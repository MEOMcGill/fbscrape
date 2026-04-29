"""
Browser management and page control for Facebook scraping
"""
from . import exceptions
from .accounts_pool import AccountsPool
from .response import ResponseInterceptor
from .account import Account
from .logger import logger
from .models import ScrapingResult, Query
from .utils import (
    recursively_get_dict_value,
    get_device_os,
    generate_fingerprint,
    serialize_fingerprint,
    deserialize_fingerprint,
    fingerprint_os,
)
from .exceptions import FailedLoginError, CheckpointError, AccountDisabledError, TransientLoginError

import asyncio
import os
import random
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright, Browser, Locator
from camoufox.async_api import AsyncNewBrowser
from typing import Optional
import re


# (URL-path-suffix regex, outcome kind). Order matters: most-specific first
# (so /checkpoint/disabled/ wins over /checkpoint/, and the `/?home` /
# end-of-host / query-only patterns are kept narrow so they don't swallow
# /login/, /zuck, /recover/, etc). Adding a new FB login flow = one row.
_LOGIN_OUTCOMES: list[tuple[str, str]] = [
    (r"/checkpoint/disabled/",   "disabled"),
    (r"/checkpoint/",            "checkpoint"),
    (r"/two_step_verification/", "two_factor"),
    (r"/two_factor/",            "two_factor"),
    (r"/?home",                  "logged_in"),  # /home, /home.php
    (r"/?$",                     "logged_in"),  # bare root (host or host/)
    (r"/?\?",                    "logged_in"),  # query-only (host?... / host/?...)
]

# Single source of truth: wait regex is the union; dispatch iterates the
# same table in order and returns the first per-row pattern that matches.
_HOST_RE = r"https://(?:www|m|web|mbasic)\.facebook\.com"
_LOGIN_OUTCOME_RE = re.compile(
    rf"^{_HOST_RE}(?:{'|'.join(f'(?:{p})' for p, _ in _LOGIN_OUTCOMES)})"
)
_LOGIN_OUTCOME_DISPATCH: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"^{_HOST_RE}{p}"), kind) for p, kind in _LOGIN_OUTCOMES
]


class BrowserSession:
    """Manages browser session and page navigation"""

    # Max attempts of the form-fill + submit + verify inner block within a single
    # login() call. Gives us one internal retry on transient playwright flakes
    # before escalating to the worker via TransientLoginError.
    LOGIN_FORM_MAX_ATTEMPTS = 2

    # Per-call wall-clock cap on page-DOM ops (scroll, error checks). Playwright's
    # `page.evaluate` and locator queries have no default timeout on the JS-engine
    # side, so if FB's renderer wedges (anti-bot kill, OOM, GC death) the await
    # blocks forever — the in-loop stall watchdog never fires because it runs
    # downstream of the hung await. This bounds the worst case at the call site.
    # TODO: replace with an external watchdog task that can cancel any hung await
    # (see CLAUDE.md → "External watchdog task for hang detection").
    OPERATION_TIMEOUT_SECONDS = 900

    # ==================== Initialization & Lifecycle ====================

    def __init__(
            self,
            account: Account,
            pool: AccountsPool,
            headless: bool = False,
            mobile: bool = False,
            stall_timeout_seconds: int = 300,
    ):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.mobile = mobile
        self.stall_timeout_seconds = stall_timeout_seconds

        # the endpoint we're scrolling (set by scraping methods like user_timeline)
        self.endpoint: str = ""

        # browser-related objects
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[ResponseInterceptor] = None

    @classmethod
    async def create(cls, account: Account, pool: AccountsPool, headless=False, mobile: bool = False, stall_timeout_seconds: int = 300):
        logger.debug(f"BrowserSession.create() for {account.display_name}, headless={headless}")
        instance = cls(account=account, pool=pool, headless=headless, mobile=mobile, stall_timeout_seconds=stall_timeout_seconds)
        await instance.initialize()
        return instance

    async def __aenter__(self):
        """Async context manager entry - initialize browser session"""
        logger.debug(f"BrowserSession.__aenter__() for {self.account.display_name}")
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - close browser session"""
        logger.debug(f"BrowserSession.__aexit__() for {self.account.display_name}, exc_type={exc_type}")
        await self.close()
        return False  # Don't suppress exceptions

    async def initialize(self):
        """Initialize browser session with playwright and camoufox.

        If any step after playwright starts raises, we tear down whatever was
        created so the caller isn't left with an orphaned browser / playwright
        handle. (Python does NOT call `__aexit__` when `__aenter__` raises, so
        the `async with BrowserSession(...) as s` pattern in Worker cannot clean
        up after an init failure by itself.)
        """
        logger.debug(f"BrowserSession.initialize() starting for {self.account.display_name}")
        self._pw = await async_playwright().start()

        try:
            # Get proxy settings from account
            proxy_settings = self._get_proxy_dict()
            logger.debug(f"Proxy settings: {'configured' if proxy_settings else 'none'}")

            # Resolve a stable per-account fingerprint (loaded or generated+persisted)
            fingerprint = await self._resolve_fingerprint()

            # Create browser context using camoufox
            self._browser: Browser = await AsyncNewBrowser(
                playwright=self._pw,
                humanize=True,
                headless="virtual" if self.headless else self.headless,
                proxy=proxy_settings,
                geoip=True if proxy_settings else False,
                os=get_device_os(),
                fingerprint=fingerprint,
                i_know_what_im_doing=True,  # custom per-account fingerprint is intentional
                firefox_user_prefs={
                    "browser.aboutwelcome.enabled": False,
                    "browser.startup.firstrunSkipsHomepage": True,
                    "browser.shell.checkDefaultBrowser": False,
                    "datareporting.policy.dataSubmissionEnabled": False,

                    # memory saving attributes - suggested by Claude
                    "browser.cache.disk.enable": False,
                    "browser.cache.memory.capacity": 0,
                    "browser.sessionhistory.max_entries": 2,
                    "browser.sessionhistory.max_total_viewers": 0,
                    "dom.ipc.processCount.webIsolated": 1,
                }
            )

            self._context: BrowserContext = await self._browser.new_context()
            logger.debug("Browser context created")

            # Create page
            self.page = await self._context.new_page()
            logger.debug("Page created")

            # To-do: Workaround for camoufox issue #473: br/zstd decompression broken
            await self.page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

            # Set up a response interceptor
            self.response_interceptor = ResponseInterceptor()
            self.response_interceptor.setup_interception(self.page)

            # Auth branch depends on whether we have cookies to try:
            #   - Cookies present: inject, verify with check_logged_in, fall back to recovery
            #     (obstacle handlers + form login) if cookies didn't yield a logged-in session.
            #   - No cookies: go straight to the form login flow. login() owns its own
            #     _on_login_success call on success, so we don't re-do the bookkeeping here.
            if self.account.cookies:
                logger.debug(f"Account has {len(self.account.cookies)} cookies, injecting...")
                try:
                    await self._context.add_cookies(self.account.cookies)
                    logger.info(f"Injected {len(self.account.cookies)} cookies for {self.account.display_name}")
                except Exception as e:
                    logger.warning(f"Failed to inject cookies for {self.account.display_name}: {e}")

                # Fast happy-path: cookies worked.
                if await self.check_logged_in(timeout=5.0):
                    await self._on_login_success()
                    logger.info(f"Browser session initialized for {self.account.display_name} (already logged in)")
                    return

                # Cookies didn't get us in — obstacle handlers + form-login fallback.
                await self._resolve_not_logged_in()
                logger.info(f"Browser session initialized for {self.account.display_name}")
            else:
                logger.debug("No cookies available, going straight to form login")
                if not await self.login():
                    raise FailedLoginError(
                        f"Failed to login for {self.account.display_name} (no login form)"
                    )
                logger.info(f"Browser session initialized for {self.account.display_name} (form login)")
        except FailedLoginError as e:
            logger.error(f"Failed to login for {self.account.display_name}: {e}")
            try:
                await self.close()
            except Exception as cleanup_err:
                logger.warning(f"Error during init-failure cleanup for {self.account.display_name}: {cleanup_err}")
            raise
        except BaseException:
            logger.debug(f"BrowserSession.initialize() failed for {self.account.display_name}, cleaning up")
            try:
                await self.close()
            except Exception as cleanup_err:
                logger.warning(f"Error during init-failure cleanup for {self.account.display_name}: {cleanup_err}")
            raise

    async def close(self):
        """Close browser session and cleanup resources"""
        logger.debug(f"BrowserSession.close() for {self.account.display_name}")
        if self.response_interceptor:
            # TEMP: Path B investigation. If FB_NETWORK_CAPTURE_DIR is set, dump the
            # captured XHR (request + response) to a JSONL file before tearing down.
            # See docs/path_b_investigation.md. Remove this block when done.
            capture_dir = os.environ.get("FB_NETWORK_CAPTURE_DIR")
            if capture_dir and self.response_interceptor.network_capture:
                try:
                    os.makedirs(capture_dir, exist_ok=True)
                    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", self.account.identifier or "unknown")
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    out_path = os.path.join(capture_dir, f"network_{ts}_{safe_id}.jsonl")
                    n = self.response_interceptor.save_network_capture_to_jsonl(out_path)
                    logger.info(f"[CAPTURE] Wrote {n} XHR records to {out_path}")
                except Exception as e:
                    logger.warning(f"[CAPTURE] Failed to dump network capture: {e}")
            logger.debug("Stopping response interceptor")
            self.response_interceptor.stop_interception()
        if self._browser:
            logger.debug("Closing browser")
            await self._browser.close()
        if self._pw:
            logger.debug("Stopping playwright")
            await self._pw.stop()

        logger.info(f"Browser session closed for {self.account.display_name}")

    # ==================== Authentication ====================
    async def _continue_to_login_is_visible(self):
        try:
            await self.page.get_by_label("Continue", exact=False).wait_for(state="visible", timeout=3000)
            logger.debug("Continue for login is visible")
            return True
        except Exception as e:
            logger.debug(f"Continue for login is not visible: {e}")
            return False

    async def pass_continue_button(self):
        # click on the 'Continue' button if it's there
        try:
            await self.page.get_by_label("Continue", exact=False).click(timeout=10000)
            logger.debug("Clicked post-login 'Continue' button")
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"Failed to click post-login 'Continue' button: {e}")
            return

        # insert the password for the login
        try:
            await self._human_type(
                self.page.get_by_role("textbox", name="Password"),
                self.account.password
            )
            logger.debug("Filled post-login 'Password' field")
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"Failed to fill post-login 'Password' field: {e}")
            return

        # press login button
        try:
            await self.page.get_by_role("button", name="Log in", exact=True).click(timeout=10000)
            logger.debug("Clicked post-login 'Log in' button")
            await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"Failed to click post-login 'Log in' button: {e}")
            return

        # now check if you've hit some issues logging in
        await self._wait_for_log_in_outcome()

    async def login(self) -> bool:
        """
        Execute Facebook login flow if needed.

        Transient errors (playwright flake, element-not-found, page timeout) get
        one internal retry with a page reload; if still failing, a
        `TransientLoginError` is raised so the worker can rotate to a different
        account WITHOUT marking the current one inactive.

        Returns:
            True if login successful or already logged in, False on known "can't
            login here" conditions (no form visible, viewer never resolved, URL
            never settled).

        Raises:
            CheckpointError / AccountDisabledError: Facebook redirected to a
                /checkpoint/ page (detector already wrote DB state).
            TransientLoginError: Both internal attempts hit unexpected errors.
        """
        logger.debug(f"BrowserSession.login() for {self.account.display_name}")
        # Check if already logged in
        if await self.check_logged_in(timeout=5.0):
            logger.debug("Already logged in")
            await self._on_login_success()
            return True

        # Decline cookies popup
        await self._clear_pre_login_popups()

        # Check if login form is visible
        if not await self._is_login_form_visible():
            logger.warning(f"Cannot login {self.account.display_name}: no login form and not logged in")
            return False

        logger.info(f"Logging in to Facebook as {self.account.display_name}")
        logger.debug("Login form is visible, proceeding with credentials")

        last_transient: Exception | None = None
        for attempt in range(1, self.LOGIN_FORM_MAX_ATTEMPTS + 1):
            logger.debug(
                f"Login attempt {attempt}/{self.LOGIN_FORM_MAX_ATTEMPTS} "
                f"for {self.account.display_name}"
            )
            try:
                # Fill username with human-like typing
                await self._human_type(
                    self.page.get_by_role('textbox', name='Email or mobile number'),
                    self.account.identifier
                )
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # Fill password with human-like typing.
                # Use role-based textbox selector: `get_by_label("Password")` also matches
                # the "Show password" button (role=button) which shares the same label.
                await self._human_type(
                    self.page.get_by_role("textbox", name="Password"),
                    self.account.password
                )
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # Click login button
                if self.mobile:
                    await self.page.get_by_role('button', name='Log in').click()
                else:
                    await self.page.get_by_role('button', name='Log in').nth(0).click()

                logger.info("Login form submitted")
                await asyncio.sleep(5)

                # Classify the post-form URL. Raises CheckpointError / AccountDisabledError
                # on a checkpoint; returns True on a logged-in URL; False if URL never settled.
                url_ok = await self._wait_for_log_in_outcome()
                if not url_ok:
                    # URL stayed on /login or some intermediate page — most commonly a
                    # slow/flaky network rather than a credential problem. Treat as
                    # transient so the worker rotates without marking the account inactive.
                    logger.warning(f"Login URL never settled for {self.account.display_name}")
                    raise TransientLoginError(
                        f"Login URL never settled after form submit for {self.account.display_name}"
                    )

                # Belt-and-suspenders: GraphQL-level confirmation
                if await self.check_logged_in(timeout=10.0):
                    await self._on_login_success()
                    logger.info(f"Login successful for {self.account.display_name}")
                    return True

                # Form submitted, URL settled on a logged-in variant, but viewer GraphQL
                # never came through — usually a GraphQL timing race or soft network issue
                # rather than a real credential failure. Treat as transient.
                logger.warning(f"Viewer never came through after form submit for {self.account.display_name}")
                raise TransientLoginError(
                    f"Viewer never came through after form submit for {self.account.display_name}"
                )

            except FailedLoginError:
                # CheckpointError / AccountDisabledError — detector already wrote DB state.
                raise
            except Exception as e:
                last_transient = e
                logger.warning(
                    f"Transient error on login attempt "
                    f"{attempt}/{self.LOGIN_FORM_MAX_ATTEMPTS} "
                    f"for {self.account.display_name}: {e}"
                )
                # Reset for next attempt — reload page and re-verify form is present
                if attempt < self.LOGIN_FORM_MAX_ATTEMPTS:
                    try:
                        await self.page.goto(
                            "https://www.facebook.com", wait_until="domcontentloaded"
                        )
                        await self._clear_pre_login_popups()
                        if not await self._is_login_form_visible():
                            # Page landed on a non-form state (e.g., already logged in
                            # via partial submission, or a checkpoint); can't retry.
                            break
                    except Exception as reset_err:
                        logger.warning(
                            f"Failed to reset page for retry "
                            f"({self.account.display_name}): {reset_err}"
                        )
                        break
                    await asyncio.sleep(random.uniform(1.5, 3.0))

        # Exhausted internal retries — escalate to worker without marking account inactive.
        raise TransientLoginError(
            f"Login failed after {self.LOGIN_FORM_MAX_ATTEMPTS} transient attempts "
            f"for {self.account.display_name}: {last_transient}"
        )

    async def check_logged_in(self, timeout: float = 10.0) -> bool:
        """
        Check if logged in by navigating to facebook.com and waiting for a GraphQL
        response whose body carries a non-null `data.viewer` object. `viewer` is
        Facebook's authenticated-user context — queries referencing it only resolve
        when a live session exists; the Continue/login pages don't answer them.
        This is DOM-independent and, unlike post-bearing responses, doesn't require
        the home feed to render (which may not fire without a scroll).

        Args:
            timeout: Max seconds to wait for a viewer-bearing GraphQL response

        Returns:
            True if a viewer-bearing response was intercepted (logged in), False otherwise
        """
        logger.debug(f"check_logged_in() with timeout={timeout}s")
        # Create a temporary interceptor for this check
        temp_interceptor = ResponseInterceptor()
        temp_interceptor.setup_interception(self.page)

        try:
            await self.page.goto("https://www.facebook.com", wait_until="domcontentloaded")

            # Wait for a viewer-bearing GraphQL response (authenticated user context)
            elapsed = 0.0
            interval = 0.5
            while elapsed < timeout:
                if temp_interceptor.has_viewer_response():
                    logger.info(
                        f"Logged in: viewer-bearing response intercepted "
                        f"({temp_interceptor.get_graphql_request_count()} GraphQL responses seen)"
                    )
                    return True
                await asyncio.sleep(interval)
                elapsed += interval

            logger.warning(
                f"Not logged in: no viewer-bearing GraphQL response after {timeout}s "
                f"({temp_interceptor.get_graphql_request_count()} generic GraphQL responses seen)"
            )
            return False
        finally:
            temp_interceptor.stop_interception()

    async def get_cookies(self) -> list[dict]:
        """Get cookies from current browser context"""
        storage_state = await self._context.storage_state()
        return storage_state['cookies']

    async def save_cookies(self):
        """Save current cookies to the database"""
        cookies = await self.get_cookies()
        await self.pool.update_cookies(self.account.identifier, cookies)
        logger.info(f"Saved cookies for {self.account.display_name}")

    async def _on_login_success(self):
        """Post-successful-login bookkeeping: persist cookies, mark account active,
        reset stale scroll counts, update last_used, and clear post-login popups."""
        # Save cookies after successful login
        await self.save_cookies()
        # Mark account as active and clear any previous error message
        await self.pool.set_active(self.account.identifier, True, None)
        # Reset scroll_count_overall_24h if last_used was over 24h ago
        if self.account.last_used:
            time_since_last_used = datetime.now(timezone.utc) - self.account.last_used.replace(tzinfo=timezone.utc)
            if time_since_last_used > timedelta(hours=24):
                await self.pool.reset_scroll_counts(self.account.identifier)
                logger.info(f"Reset scroll counts for {self.account.display_name} (last used {time_since_last_used} ago)")
        # Update last_used timestamp
        await self.pool.update_last_used(self.account.identifier)
        # Clear any post-login popups
        await self._clear_post_login_popups()

    async def _resolve_not_logged_in(self):
        """Handle all known 'not-yet-logged-in' states after cookie injection.

        Tries each registered obstacle handler in order. Each handler self-detects
        its case and, if matched, performs the recovery action. After any match we
        re-verify with `check_logged_in` (GraphQL-based, DOM-independent). If no
        handler succeeds in getting us logged in, we fall back to the last resort:
        wipe cookies and run the full `login()` form flow.

        To add a new obstacle, define `_handle_<case>(self) -> bool` that returns
        True iff it matched, and register it in `obstacle_handlers` below.

        Raises:
            FailedLoginError: if no handler and no fallback resulted in a login.
        """
        obstacle_handlers = [
            self._handle_continue_interstitial,
            # future: self._handle_2fa_challenge,
            # future: self._handle_checkpoint,
        ]

        for handler in obstacle_handlers:
            if not await handler():
                continue
            logger.info(f"Login obstacle matched by {handler.__name__}")
            if await self.check_logged_in(timeout=10.0):
                await self._on_login_success()
                return
            logger.warning(f"{handler.__name__} ran but login still not confirmed")

        # Last resort: wipe cookies and run the full login form flow.
        logger.warning(f"No obstacle handler succeeded — falling back to full login for {self.account.display_name}")
        await self._context.clear_cookies()
        if not await self.login():
            raise FailedLoginError(f"Failed to login for {self.account.display_name}")

    async def _handle_continue_interstitial(self) -> bool:
        """Facebook 'Continue' re-auth screen: click Continue, submit password, click Log in."""
        if not await self._continue_to_login_is_visible():
            return False
        await self.pass_continue_button()
        return True

    async def _wait_for_log_in_outcome(self) -> bool:
        """Wait for the post-login-form URL to settle and classify the outcome.

        Outcomes are enumerated in the module-level `_LOGIN_OUTCOMES` table —
        regex (`_LOGIN_OUTCOME_RE`) and dispatch share that single source of
        truth, so adding a new FB login flow is one row.

        On a known terminal failure (`disabled`, `checkpoint`, `two_factor`)
        we persist `error_msg` + mark the account inactive *before* raising,
        so higher layers don't need a second DB write.
        """
        try:
            await self.page.wait_for_url(_LOGIN_OUTCOME_RE, timeout=5000)
        except Exception as e:
            logger.debug(
                f"No known login-outcome URL after 5s: {e} "
                f"(last url={self.page.url})"
            )
            return False

        url = self.page.url
        for pattern, kind in _LOGIN_OUTCOME_DISPATCH:
            if pattern.match(url):
                return await self._dispatch_login_outcome(kind, url)

        # Unreachable as long as the wait regex and the dispatch list are
        # built from the same table — log and bail just in case.
        logger.warning(f"URL matched _LOGIN_OUTCOME_RE but no handler: {url}")
        return False

    async def _dispatch_login_outcome(self, kind: str, url: str) -> bool:
        """Route a classified login outcome to its handler.

        `logged_in` returns True. Failure kinds mark the account inactive and
        raise the corresponding exception — all info needed by the worker is
        on the exception (`url` attr) and in the DB (`error_msg`).
        """
        if kind == "logged_in":
            return True

        msg_by_kind = {
            "disabled":   f"Account disabled by Facebook ({url})",
            "checkpoint": f"Checkpoint challenge — manual intervention required ({url})",
            "two_factor": f"2FA challenge — manual intervention required ({url})",
        }
        exc_by_kind = {
            "disabled":   AccountDisabledError,
            "checkpoint": CheckpointError,
            "two_factor": CheckpointError,
        }
        msg = msg_by_kind[kind]
        logger.warning(f"{self.account.display_name}: {msg}")
        await self.pool.set_active(self.account.identifier, False, msg)
        raise exc_by_kind[kind](msg, url=url)

    # ==================== Scraping ====================

    async def user_timeline(
        self,
        handle: str,
        start_date: str,
        end_date: str,
    ) -> ScrapingResult:
        """
        Scrape a Facebook user's homepage using GraphQL response interception.

        Args:
            handle: Facebook username/handle
            start_date: Start date for scraping (YYYY-MM-DD)
            end_date: End date for scraping (YYYY-MM-DD)

        Returns:
            ScrapingResult with outcome and collected data
        """

        self.endpoint = "UserTimeline"
        logger.debug(f"user_timeline() starting for @{handle}, date range: {start_date} to {end_date}")

        base_url = "https://www.facebook.com/"
        target_url = f"{base_url}{handle}/"

        scrape_start_time = datetime.now(timezone.utc)
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d")

        # Create Query object for this scrape
        query = Query(
            endpoint=self.endpoint,
            query={
                "handle": handle,
                "start_date": start_date,
                "end_date": end_date,
            },
            params={},
            start_date=start_datetime,
            end_date=end_datetime
        )

        total_scrolls = 0
        no_new_posts_count = 0
        previous_post_count = 0

        logger.info(f"Scraping @{handle}'s homepage from {start_date} to {end_date}")

        # Clear any existing intercepted data
        self.response_interceptor.flush()

        while True:
            try:
                iter_start = datetime.now(timezone.utc)
                logger.debug(f"@{handle} loop iter {total_scrolls}: start")

                # Navigate to target page if needed
                if not self.is_on_page(target_url):
                    logger.info(f"Navigating to {target_url}")
                    await self.goto(target_url)
                    await asyncio.sleep(5)

                    # press escape key
                    await self.page.keyboard.press('Escape')

                    # Check if we got logged out
                    if not self.is_on_page(target_url):
                        return ScrapingResult(
                            query=query,
                            result='logged out while scraping',
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

                    # Check for error conditions after navigation
                    try:
                        error = await asyncio.wait_for(
                            self.check_error_conditions(),
                            timeout=self.OPERATION_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"@{handle}: check_error_conditions() (post-nav) hung > "
                            f"{self.OPERATION_TIMEOUT_SECONDS}s — returning partial results"
                        )
                        return ScrapingResult(
                            query=query,
                            result=f'hang: post-nav error check timed out after {self.OPERATION_TIMEOUT_SECONDS}s',
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time,
                        )
                    if error:
                        return ScrapingResult(
                            query=query,
                            result=error,
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

                # Get currently intercepted posts
                t_get_posts = datetime.now(timezone.utc)
                logger.debug(f"@{handle} iter {total_scrolls}: before get_posts()")
                posts = self.response_interceptor.get_posts()
                current_post_count = len(posts)
                last_resp_dbg = self.response_interceptor.last_response_time
                if last_resp_dbg is not None:
                    silence_dbg = (datetime.now(timezone.utc) - last_resp_dbg).total_seconds()
                    last_resp_str = f"{last_resp_dbg.strftime('%H:%M:%S')} ({silence_dbg:.1f}s ago, threshold={self.stall_timeout_seconds}s)"
                else:
                    last_resp_str = f"never (threshold={self.stall_timeout_seconds}s)"
                logger.debug(
                    f"@{handle} iter {total_scrolls}: after get_posts() "
                    f"({(datetime.now(timezone.utc) - t_get_posts).total_seconds():.2f}s), "
                    f"count={current_post_count} (prev={previous_post_count}), "
                    f"graphql_responses={self.response_interceptor.get_graphql_request_count()}, "
                    f"last_response={last_resp_str}"
                )

                logger.debug(f"Scrolled {total_scrolls} times, intercepted {current_post_count} posts")

                # Check if we're making progress
                if current_post_count == previous_post_count:
                    no_new_posts_count += 1
                    logger.debug(f"@{handle} iter {total_scrolls}: no new posts (streak={no_new_posts_count})")

                    # Check for errors when stalled
                    if no_new_posts_count == 3:
                        t_err = datetime.now(timezone.utc)
                        logger.debug(f"@{handle} iter {total_scrolls}: before check_error_conditions()")
                        try:
                            error = await asyncio.wait_for(
                                self.check_error_conditions(),
                                timeout=self.OPERATION_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"@{handle}: check_error_conditions() (stalled) hung > "
                                f"{self.OPERATION_TIMEOUT_SECONDS}s — returning partial results"
                            )
                            return ScrapingResult(
                                query=query,
                                result=f'hang: stalled error check timed out after {self.OPERATION_TIMEOUT_SECONDS}s',
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                            )
                        logger.debug(
                            f"@{handle} iter {total_scrolls}: after check_error_conditions() "
                            f"({(datetime.now(timezone.utc) - t_err).total_seconds():.2f}s), error={error!r}"
                        )
                        if error:
                            return ScrapingResult(
                                query=query,
                                result=error,
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                    if no_new_posts_count > 30:
                        if current_post_count == 0:
                            return ScrapingResult(
                                query=query,
                                result='no posts',
                                posts=[],
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        else:
                            return ScrapingResult(
                                query=query,
                                result='scraped until first ever post was reached',
                                posts=posts,
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                else:
                    no_new_posts_count = 0
                    previous_post_count = current_post_count
                    logger.debug(f"@{handle} iter {total_scrolls}: progress! new count={current_post_count}")

                # Check timestamps if we have posts
                if current_post_count > 0:
                    oldest_timestamp = self._find_oldest_post_timestamp(posts)

                    if oldest_timestamp:
                        logger.debug(f"Oldest post: {oldest_timestamp}, target: {start_datetime}")

                        # Check if we've reached the target date
                        if oldest_timestamp.replace(tzinfo=None) < start_datetime:
                            response_interceptor_posts = self.response_interceptor.get_posts()
                            logger.info(f"Reached target start date {start_date} for @{handle} scraping {len(response_interceptor_posts)} posts")
                            return ScrapingResult(
                                query=query,
                                result='scraped until user-specified starting date was reached',
                                posts=response_interceptor_posts,
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                # Watchdog: bail out if Facebook has stopped responding to GraphQL
                last_resp = self.response_interceptor.last_response_time or scrape_start_time
                silence_seconds = (datetime.now(timezone.utc) - last_resp).total_seconds()
                if silence_seconds > self.stall_timeout_seconds:
                    logger.warning(
                        f"@{handle}: no GraphQL response for {silence_seconds:.0f}s "
                        f"(threshold={self.stall_timeout_seconds}s) — returning partial results"
                    )
                    return ScrapingResult(
                        query=query,
                        result=f'stalled: no graphql response for {int(silence_seconds)}s',
                        posts=self.response_interceptor.get_posts(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time,
                    )

                # Scroll to trigger loading more posts (also records scroll in database)
                t_scroll = datetime.now(timezone.utc)
                logger.debug(f"@{handle} iter {total_scrolls}: before scroll()")
                try:
                    await asyncio.wait_for(self.scroll(), timeout=self.OPERATION_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"@{handle}: scroll() hung > {self.OPERATION_TIMEOUT_SECONDS}s "
                        f"— renderer likely wedged, returning partial results"
                    )
                    return ScrapingResult(
                        query=query,
                        result=f'hang: scroll timed out after {self.OPERATION_TIMEOUT_SECONDS}s',
                        posts=self.response_interceptor.get_posts(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time,
                    )
                logger.debug(
                    f"@{handle} iter {total_scrolls}: after scroll() "
                    f"({(datetime.now(timezone.utc) - t_scroll).total_seconds():.2f}s)"
                )
                total_scrolls += 1

                # Rate limiting
                if total_scrolls % 50 == 0:
                    logger.info(f"@{handle}: {current_post_count} posts after {total_scrolls} scrolls - pausing 30s")
                    await asyncio.sleep(30)

                sleep_s = random.uniform(2, 4.5)
                logger.debug(f"@{handle} iter {total_scrolls-1}: sleeping {sleep_s:.2f}s for GraphQL responses")
                await asyncio.sleep(sleep_s)
                logger.debug(
                    f"@{handle} iter {total_scrolls-1}: iter total "
                    f"{(datetime.now(timezone.utc) - iter_start).total_seconds():.2f}s"
                )

            except Exception as e:
                logger.error(f"Error scraping @{handle}: {e}")
                return ScrapingResult(
                    query=query,
                    result=f'error: {str(e)}',
                    posts=self.response_interceptor.get_posts(),
                    time_started=scrape_start_time,
                    time_taken=datetime.now(timezone.utc) - scrape_start_time
                )

    async def check_error_conditions(self) -> str | None:
        """
        Check for Facebook error conditions on current page.

        Uses a combined locator for fast-path detection, then detailed checks
        only if an error indicator is found.

        Returns:
            Error code string if error detected, None otherwise
        """
        logger.debug("check_error_conditions()")

        # Fast path: single query to check if ANY error indicator exists
        error_indicators = (
            self.page.get_by_role("button", name="Retry")
            .or_(self.page.get_by_role("button", name="Reload page"))
            .or_(self.page.get_by_text("Profile isn't available"))
            .or_(self.page.get_by_text("This content isn't available"))
            .or_(self.page.get_by_text("Sorry, this page isn't available"))
            .or_(self.page.get_by_text("No Posts Yet"))
            .or_(self.page.get_by_text("This account is private"))
        )
        if await error_indicators.count() == 0:
            return None  # No errors - fast exit

        # Slow path: determine which specific error
        logger.debug("Error indicator detected, checking specifics...")

        # Check for "Retry" button (multiple error cases)
        retry_button = self.page.get_by_role("button", name="Retry")
        if await retry_button.count() > 0:
            if await self.page.get_by_text("account is private").count() > 0:
                return 'account is private'
            if await self.page.get_by_text("Failed to Load").count() > 0:
                return 'failed to load'

        # Check for "Reload page" button
        reload_button = self.page.get_by_role('button', name='Reload page')
        if await reload_button.count() > 0:
            if await self.page.get_by_text("Something went wrong").count() > 0:
                return 'something went wrong - reload'

        # Direct text checks
        if await self.page.get_by_text("Profile isn't available").count() > 0:
            return 'profile is not available'

        if await self.page.get_by_text("Sorry, this page isn't available").count() > 0:
            return 'page not available'

        if await self.page.get_by_text("No Posts Yet").count() > 0:
            return 'no posts'

        if await self.page.get_by_text("This account is private").count() > 0:
            return 'account is private'

        if await self.page.get_by_text("This content isn't available right now").count() > 0:
            return 'content not available'

        return None

    async def record_scroll(self, endpoint: str, count: int = 1):
        """
        Record scroll(s) for the current account and update the database.

        Args:
            endpoint: The endpoint being scrolled (e.g., 'user_page', 'search')
            count: Number of scrolls to record (default 1)
        """
        await self.pool.update_scroll_count(self.account.identifier, endpoint, count)

    async def get_scroll_count(self, endpoint: str | None = None) -> int:
        """
        Get scroll count for the current account.

        Args:
            endpoint: If provided, get count for specific endpoint; otherwise get overall 24h count

        Returns:
            Scroll count
        """
        return await self.pool.get_scroll_count(self.account.identifier, endpoint)

    # ==================== Navigation ====================

    async def goto(self, url: str, timeout: int = 30000, wait_until: str = "domcontentloaded"):
        """Navigate to URL"""
        logger.debug(f"goto({url})")
        await self.page.goto(url, timeout=timeout, wait_until=wait_until)

    def is_on_page(self, url: str) -> bool:
        """Check if currently on a specific URL"""
        return self.page.url == url

    async def scroll_to_element(self, element):
        """Scroll element into view"""
        await element.scroll_into_view_if_needed()

    def find_elements(self, selector: str):
        """Query elements by selector"""
        return self.page.locator(selector)

    async def scroll(self, window_height_coefficient: float = 3):
        """Scroll window by window_height_coefficient * window.innerHeight"""
        logger.debug(f"scroll(coeff={window_height_coefficient}) for endpoint={self.endpoint}")
        await self.page.evaluate(f"window.scrollBy(0, window.innerHeight * {window_height_coefficient})")
        await self.record_scroll(endpoint=self.endpoint, count=1)


    # ==================== Private Helpers ====================

    async def _resolve_fingerprint(self):
        """Return a browserforge Fingerprint for this session.

        Prefers the persisted fingerprint on the account, but regenerates if:
          - no fingerprint is stored yet,
          - the stored JSON is corrupt / fails to deserialize,
          - the stored fingerprint's OS differs from the current host OS.

        Camoufox cannot reliably mask the underlying host OS (canvas / WebGL /
        fonts / media APIs leak through the Firefox sandbox regardless of the
        fingerprint overrides), so a macOS fingerprint run on a Linux host is
        a stronger anti-bot signal than a fresh consistent one. We regenerate
        on host-OS drift.
        """
        host_os = get_device_os()
        fp_json = self.account.fingerprint
        if fp_json:
            try:
                fp = deserialize_fingerprint(fp_json)
                stored_os = fingerprint_os(fp)
                if stored_os == host_os:
                    logger.debug(
                        f"Loaded persisted fingerprint for {self.account.display_name} (os={stored_os})"
                    )
                    return fp
                logger.info(
                    f"Host OS changed for {self.account.display_name} "
                    f"(stored={stored_os}, current={host_os}); regenerating fingerprint"
                )
            except Exception as e:
                logger.warning(
                    f"Corrupt fingerprint for {self.account.display_name}: {e}; regenerating"
                )

        fp = generate_fingerprint(host_os)
        fp_json = serialize_fingerprint(fp)
        await self.pool.update_fingerprint(self.account.identifier, fp_json)
        self.account.fingerprint = fp_json
        logger.info(
            f"Generated + persisted new fingerprint for {self.account.display_name} (os={host_os})"
        )
        return fp

    def _get_proxy_dict(self) -> dict | None:
        """Build proxy configuration dict from account settings"""
        if self.account.proxy_server:
            if self.account.proxy_username and self.account.proxy_password:
                return {
                    "server": self.account.proxy_server,
                    "username": self.account.proxy_username,
                    "password": self.account.proxy_password,
                }
            else:
                logger.warning(
                    f"Proxy username ({self.account.proxy_username}) and password ({self.account.proxy_password}) "
                    f"are required if proxy server is set."
                )
        return None

    async def _is_login_form_visible(self) -> bool:
        """Check if Facebook login form is visible"""
        try:
            await self.page.get_by_label("Email or phone number").or_(
                self.page.get_by_label("Password")
            ).or_(
                self.page.get_by_role("button", name="Log in", exact=True)
            ).first.wait_for(state="visible", timeout=5000)
            return True
        except Exception:
            return False

    async def _decline_optional_cookies(self) -> bool:
        """Decline optional cookies popup if present"""
        try:
            await self.page.get_by_role('button', name='Decline optional cookies').nth(0).click(timeout=5000)
            logger.info("Declined optional cookies")
            return True
        except Exception:
            return False

    async def _close_firefox_startup_overlay(self):
        """Close Firefox startup overlay if present"""
        try:
            await self.page.get_by_role('button', name='Close').nth(0).click(timeout=5000)
            logger.info("Closed Firefox startup overlay")
        except Exception:
            pass

    async def _close_not_now_pop(self):
        """Close Not Now popup if present"""
        try:
            label = 'Not now' if self.mobile else 'Not Now'
            await self.page.get_by_role('button', name=label).nth(0).click(timeout=5000)
            logger.info("Closed Not Now popup")
        except Exception:
            pass

    async def _clear_pre_login_popups(self):
        """Dismiss pre-login popup dialogs"""
        await self._decline_optional_cookies()

    async def _clear_post_login_popups(self):
        """Dismiss post-login popup dialogs"""
        await self._close_firefox_startup_overlay()
        await self._close_not_now_pop()

    async def _human_type(self, locator: Locator, text: str, mean_delay: float = 0.1, std_dev: float = 0.03):
        """
        Type text with human-like timing using a normal distribution for delays.

        Args:
            locator: Playwright locator to type into
            text: Text to type
            mean_delay: Mean delay between keystrokes in seconds (default 100ms)
            std_dev: Standard deviation of delay in seconds (default 30ms)
        """
        await locator.click()
        for char in text:
            await locator.press(char)
            # Sample delay from normal distribution, clamp to avoid negative/extreme values
            delay = max(0.02, random.gauss(mean_delay, std_dev))
            await asyncio.sleep(delay)

    def _find_oldest_post_timestamp(self, posts: list[dict]) -> datetime | None:
        """Find the oldest timestamp among intercepted posts"""
        oldest_timestamp = None

        for post in posts:
            # Try multiple possible timestamp fields
            ts = (
                recursively_get_dict_value(post, 'timestamp.story.creation_time') or
                recursively_get_dict_value(post, 'created_time')
            )

            if ts:
                try:
                    # Extract from dict if multiple values
                    if isinstance(ts, dict):
                        if len(set(ts.values())) == 1:
                            ts = list(ts.values()).pop()
                        else:
                            logger.warning(f"Post has multiple timestamps, "
                                           f"taking the latest one - "
                                           f"({[datetime.fromisoformat(ts) for ts in ts.values()]})")
                            ts = max(ts.values())

                    # Handle both Unix timestamp and datetime string
                    if isinstance(ts, (int, float)):
                        post_datetime = datetime.fromtimestamp(ts, tz=timezone.utc)
                    elif isinstance(ts, str):
                        post_datetime = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        continue

                    if oldest_timestamp is None or post_datetime < oldest_timestamp:
                        oldest_timestamp = post_datetime
                except Exception:
                    continue

        return oldest_timestamp
