"""
Browser management and page control for Facebook scraping
"""
from .accounts_pool import AccountsPool
from .response import ResponseInterceptor
from .account import Account
from .logger import logger
from .models import ScrapeOutcome
from .utils import (
    recursively_get_dict_value,
    get_device_os,
    generate_fingerprint,
    serialize_fingerprint,
    deserialize_fingerprint,
    fingerprint_os,
)
from .exceptions import (
    FailedLoginError, CheckpointError, AccountDisabledError, TransientLoginError,
    AccountBannedError, RateLimitError,
)

import asyncio
import json
import os
import random
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlencode
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright, Browser, Locator
from camoufox.async_api import AsyncNewBrowser
from typing import Optional
import re


# Hybrid-mode constants — used by user_timeline_hybrid() to drive
# pagination via page.request.post() instead of scroll-driven rendering.
GRAPHQL_API_URL = "https://www.facebook.com/api/graphql/"
# Headers managed by Playwright / TLS layer or by the BrowserContext; never
# pass them through to page.request.post() from a captured template.
HYBRID_HEADER_DROP = frozenset({
    "host", "content-length", "connection", "accept-encoding", "cookie",
})
# GraphQL friendly name we look for in the live network capture to
# extract the request template.
HYBRID_TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"


class BrowserSession:
    """Manages browser session and page navigation"""

    # Max attempts of the form-fill + submit + verify inner block within a single
    # login() call. Gives us one internal retry on transient playwright flakes
    # before escalating to the worker via TransientLoginError.
    LOGIN_FORM_MAX_ATTEMPTS = 2

    # NOTE: per-call wall-clock cap on page-DOM ops (scroll, error checks) used
    # to live here as the class constant `OPERATION_TIMEOUT_SECONDS = 900`. It
    # is now `operation_timeout_seconds`, a per-mode param in
    # Query.ENDPOINT_REGISTRY (see docs/hybrid/overview.md and CLAUDE.md →
    # "External watchdog task for hang detection" for the long-term plan).

    # ==================== Initialization & Lifecycle ====================

    def __init__(
            self,
            account: Account,
            pool: AccountsPool,
            headless: bool = False,
            mobile: bool = False,
    ):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.mobile = mobile

        # the endpoint we're scrolling (set by scraping methods like user_timeline)
        self.endpoint: str = ""

        # browser-related objects
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[ResponseInterceptor] = None

    @classmethod
    async def create(cls, account: Account, pool: AccountsPool, headless=False, mobile: bool = False):
        logger.debug(f"BrowserSession.create() for {account.display_name}, headless={headless}")
        instance = cls(account=account, pool=pool, headless=headless, mobile=mobile)
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
            # See docs/hybrid/overview.md. Remove this block when done.
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

        Outcomes table: (URL-path-suffix regex, outcome kind). Order matters —
        most-specific first (so /checkpoint/disabled/ wins over /checkpoint/,
        and the `/?home` / end-of-host / query-only patterns are kept narrow
        so they don't swallow /login/, /zuck, /recover/, etc.). Adding a new
        FB login flow is one row. The wait regex is the union of all rows;
        the dispatch iterates the same table in order and returns the first
        per-row pattern that matches.

        On a known terminal failure (`disabled`, `checkpoint`, `two_factor`)
        we persist `error_msg` + mark the account inactive *before* raising,
        so higher layers don't need a second DB write.
        """
        outcomes: list[tuple[str, str]] = [
            (r"/checkpoint/disabled/",   "disabled"),
            (r"/checkpoint/",            "checkpoint"),
            (r"/two_step_verification/", "two_factor"),
            (r"/two_factor/",            "two_factor"),
            (r"/?home",                  "logged_in"),  # /home, /home.php
            (r"/?$",                     "logged_in"),  # bare root (host or host/)
            (r"/?\?",                    "logged_in"),  # query-only (host?... / host/?...)
        ]
        host_re = r"https://(?:www|m|web|mbasic)\.facebook\.com"
        wait_re = re.compile(
            rf"^{host_re}(?:{'|'.join(f'(?:{p})' for p, _ in outcomes)})"
        )
        dispatch: list[tuple[re.Pattern, str]] = [
            (re.compile(rf"^{host_re}{p}"), kind) for p, kind in outcomes
        ]

        try:
            await self.page.wait_for_url(wait_re, timeout=5000)
        except Exception as e:
            logger.debug(
                f"No known login-outcome URL after 5s: {e} "
                f"(last url={self.page.url})"
            )
            return False

        url = self.page.url
        for pattern, kind in dispatch:
            if pattern.match(url):
                return await self._dispatch_login_outcome(kind, url)

        # Unreachable as long as the wait regex and the dispatch list are
        # built from the same table — log and bail just in case.
        logger.warning(f"URL matched login-outcome wait regex but no handler: {url}")
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

    async def user_timeline_manual(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        scroll_window_height_coefficient: float = 3.0,
        post_nav_sleep_seconds: float = 5.0,
        inter_scroll_sleep_range: tuple[float, float] = (2.0, 4.5),
        breather_every_n_scrolls: int = 50,
        breather_duration_seconds: float = 30,
        max_no_new_posts_streak: int = 30,
        stall_timeout_seconds: float = 300,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """
        Scrape a Facebook user's homepage by driving scroll and intercepting
        GraphQL responses (the "manual" mode of the UserTimeline endpoint).

        Direct-call caveat: when invoked outside the Worker pipeline, callers
        are responsible for pre-validating inputs (date format / range / future
        end_date clamping). This method does not run Query.__post_init__.
        Going through `FacebookScraper.user_timeline` does run validation.

        Args:
            handle: Facebook username/handle.
            start_date / end_date: YYYY-MM-DD.
            (... see Query.ENDPOINT_REGISTRY[("UserTimeline","manual")] for the
             full list of param defaults and meanings.)

        Returns:
            ScrapeOutcome — Worker composes the final ScrapingResult by
            attaching the canonical Query via ScrapingResult.from_outcome.
        """

        self.endpoint = "UserTimeline"
        logger.debug(f"user_timeline_manual() starting for @{handle}, date range: {start_date} to {end_date}")

        base_url = "https://www.facebook.com/"
        target_url = f"{base_url}{handle}/"

        scrape_start_time = datetime.now(timezone.utc)

        # Inputs are assumed pre-validated (see direct-call caveat above).
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d")

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
                    await asyncio.sleep(post_nav_sleep_seconds)

                    # press escape key
                    await self.page.keyboard.press('Escape')

                    # Check if we got logged out
                    if not self.is_on_page(target_url):
                        return ScrapeOutcome(
                            result='logged out while scraping',
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

                    # Check for error conditions after navigation
                    try:
                        error = await asyncio.wait_for(
                            self.check_error_conditions(),
                            timeout=operation_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"@{handle}: check_error_conditions() (post-nav) hung > "
                            f"{operation_timeout_seconds}s — returning partial results"
                        )
                        return ScrapeOutcome(
                            result=f'hang: post-nav error check timed out after {operation_timeout_seconds}s',
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )
                    if error:
                        return ScrapeOutcome(
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
                    last_resp_str = f"{last_resp_dbg.strftime('%H:%M:%S')} ({silence_dbg:.1f}s ago, threshold={stall_timeout_seconds}s)"
                else:
                    last_resp_str = f"never (threshold={stall_timeout_seconds}s)"
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
                                timeout=operation_timeout_seconds,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"@{handle}: check_error_conditions() (stalled) hung > "
                                f"{operation_timeout_seconds}s — returning partial results"
                            )
                            return ScrapeOutcome(
                                result=f'hang: stalled error check timed out after {operation_timeout_seconds}s',
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        logger.debug(
                            f"@{handle} iter {total_scrolls}: after check_error_conditions() "
                            f"({(datetime.now(timezone.utc) - t_err).total_seconds():.2f}s), error={error!r}"
                        )
                        if error:
                            return ScrapeOutcome(
                                result=error,
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                    if no_new_posts_count > max_no_new_posts_streak:
                        if current_post_count == 0:
                            return ScrapeOutcome(
                                result='no posts',
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        else:
                            return ScrapeOutcome(
                                result='scraped until first ever post was reached',
                                posts=self.response_interceptor.get_posts(),
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
                            logger.info(
                                f"Reached target start date {start_date} for @{handle} "
                                f"scraping {len(self.response_interceptor.get_posts())} posts"
                            )
                            return ScrapeOutcome(
                                result='scraped until user-specified starting date was reached',
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                # Watchdog: bail out if Facebook has stopped responding to GraphQL
                last_resp = self.response_interceptor.last_response_time or scrape_start_time
                silence_seconds = (datetime.now(timezone.utc) - last_resp).total_seconds()
                if silence_seconds > stall_timeout_seconds:
                    logger.warning(
                        f"@{handle}: no GraphQL response for {silence_seconds:.0f}s "
                        f"(threshold={stall_timeout_seconds}s) — returning partial results"
                    )
                    return ScrapeOutcome(
                        result=f'stalled: no graphql response for {int(silence_seconds)}s',
                        posts=self.response_interceptor.get_posts(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time
                    )

                # Scroll to trigger loading more posts (also records scroll in database)
                t_scroll = datetime.now(timezone.utc)
                logger.debug(f"@{handle} iter {total_scrolls}: before scroll()")
                try:
                    await asyncio.wait_for(
                        self.scroll(window_height_coefficient=scroll_window_height_coefficient),
                        timeout=operation_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"@{handle}: scroll() hung > {operation_timeout_seconds}s "
                        f"— renderer likely wedged, returning partial results"
                    )
                    return ScrapeOutcome(
                        result=f'hang: scroll timed out after {operation_timeout_seconds}s',
                        posts=self.response_interceptor.get_posts(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time
                    )
                logger.debug(
                    f"@{handle} iter {total_scrolls}: after scroll() "
                    f"({(datetime.now(timezone.utc) - t_scroll).total_seconds():.2f}s)"
                )
                total_scrolls += 1

                # Periodic breather to look less bot-like
                if total_scrolls % breather_every_n_scrolls == 0:
                    logger.info(
                        f"@{handle}: {current_post_count} posts after {total_scrolls} scrolls - "
                        f"pausing {breather_duration_seconds}s"
                    )
                    await asyncio.sleep(breather_duration_seconds)

                sleep_s = random.uniform(*inter_scroll_sleep_range)
                logger.debug(f"@{handle} iter {total_scrolls-1}: sleeping {sleep_s:.2f}s for GraphQL responses")
                await asyncio.sleep(sleep_s)
                logger.debug(
                    f"@{handle} iter {total_scrolls-1}: iter total "
                    f"{(datetime.now(timezone.utc) - iter_start).total_seconds():.2f}s"
                )

            except Exception as e:
                logger.error(f"Error scraping @{handle}: {e}")
                return ScrapeOutcome(
                    result=f'error: {str(e)}',
                    posts=self.response_interceptor.get_posts(),
                    time_started=scrape_start_time,
                    time_taken=datetime.now(timezone.utc) - scrape_start_time
                )

    # ==================== Hybrid mode (UserTimeline) ====================
    #
    # Boundary between hybrid-private and shared utilities (for the future
    # manual+hybrid dedup pass — search for `manual+hybrid dedup`):
    #   Shared utilities used by hybrid:
    #     - self.goto()                         — navigation wrapper
    #     - self.scroll()                       — bootstrap + organic bursts
    #     - self.check_error_conditions()       — DOM error detection
    #     - self.record_scroll()                — DB write for rotation policy
    #     - self.response_interceptor.parser.parse_timeline_response()
    #     - self.response_interceptor.add_posts()
    #     - self.response_interceptor.latest_csr / latest_dyn (token freshness)
    #   Hybrid-private (search `_hybrid_*` to find them all):
    #     phase methods: _hybrid_navigate, _hybrid_bootstrap,
    #                    _hybrid_capture_template, _hybrid_pagination_loop
    #     helpers:       _hybrid_wait_for_template, _hybrid_parse_form_data,
    #                    _hybrid_clean_headers, _hybrid_build_body,
    #                    _hybrid_walk_response_for, _hybrid_extract_end_cursor,
    #                    _hybrid_extract_oldest_creation_time,
    #                    _hybrid_extract_graphql_error,
    #                    _hybrid_organic_scroll_burst
    #     constants:     GRAPHQL_API_URL, HYBRID_HEADER_DROP,
    #                    HYBRID_TARGET_FRIENDLY_NAME

    async def user_timeline_hybrid(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        pagination_count: int = 3,
        scroll_burst_every: int = 10,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 20.0,
        max_paginations: int = 10000,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """
        Scrape a Facebook user's timeline via the "hybrid" mode.

        Bypasses scroll-driven pagination: navigates to the profile, provokes
        one bootstrap scroll to fire the first ProfileCometTimelineFeedRefetchQuery
        (so we can capture its shape), then drives all subsequent paginations
        via `page.request.post()` directly. `beforeTime` is set on every
        replay (matching FB's UI date-filter behavior) so FB caps the
        upper bound server-side; `afterTime` is left `null` (FB's UI never
        sets it — overriding would be a fingerprint). The lower bound is
        enforced client-side by terminating the loop when a batch's oldest
        post is older than `start_date`.

        Every successful pagination request increments the account's scroll
        count via `record_scroll()` — paginations count as scrolls for
        rotation-policy purposes (see docs/hybrid/overview.md
        "Bookkeeping rules").

        Stop conditions:
          1. `end_cursor` missing/null in the response — FB has no more
             posts in our filter range.
          2. (defensive) Oldest post in response is older than `start_date`
             — in case `afterTime` returns posts past the boundary.

        Direct-call caveat: when invoked outside the Worker pipeline, callers
        are responsible for pre-validating inputs (date format / range / future
        end_date clamping). This method does not run Query.__post_init__.
        Going through `FacebookScraper.user_timeline` does run validation.

        Args:
            handle: Facebook username/handle.
            start_date: Lower-bound date (YYYY-MM-DD). Sent as `afterTime`.
            end_date: Upper-bound date (YYYY-MM-DD). Sent as `beforeTime`.
            (... see Query.ENDPOINT_REGISTRY[("UserTimeline","hybrid")] for the
             full list of param defaults and meanings.)

        Returns:
            ScrapeOutcome — Worker composes the final ScrapingResult by
            attaching the canonical Query via ScrapingResult.from_outcome.
        """
        self.endpoint = "UserTimeline"
        logger.info(
            f"[hybrid] @{handle}: starting hybrid scrape "
            f"({start_date} → {end_date}, count={pagination_count})"
        )

        target_url = f"https://www.facebook.com/{handle}/"
        scrape_start_time = datetime.now(timezone.utc)

        # Inputs are assumed pre-validated (see direct-call caveat above).
        # Compute the date bounds the pagination loop needs.
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        # afterTime: start-of-day UTC of start_date (inclusive — catches every
        # post on start_date since their creation_time ≥ start-of-day).
        start_unix = int(start_datetime.timestamp())

        # beforeTime: end-of-day UTC of end_date, capped at "now" so we capture
        # all posts up to this exact second when end_date is today. Mirrors
        # FB's frontend behavior: when "today" is selected as a date filter,
        # FB sets beforeTime to int(datetime.now(UTC).timestamp()) (verified
        # empirically — see docs/hybrid/overview.md). A non-null
        # beforeTime is also a precondition for FB honoring `cursor=null` on
        # the first replay (which is how we walk a date-filtered range from
        # the most-recent end), so this guarantee matters.
        end_of_day = end_datetime + timedelta(days=1) - timedelta(seconds=1)
        now_utc = datetime.now(timezone.utc)
        end_unix = int(min(end_of_day, now_utc).timestamp())

        # Bundle pagination-loop tunables into a local params dict so the
        # phase method can pull what it needs by key (and adding a new tunable
        # doesn't need a signature change in _hybrid_pagination_loop).
        loop_params = {
            "pagination_count": pagination_count,
            "scroll_burst_every": scroll_burst_every,
            "scroll_burst_size_range": scroll_burst_size_range,
            "pagination_sleep_mean": pagination_sleep_mean,
            "pagination_sleep_std": pagination_sleep_std,
            "max_paginations": max_paginations,
            "max_no_progress_streak": max_no_progress_streak,
            "request_timeout_ms": request_timeout_ms,
            "operation_timeout_seconds": operation_timeout_seconds,
        }

        self.response_interceptor.flush()
        # Hybrid sources every post from a replay request whose body carries
        # the user's date filters. The natural PCTFRQ fired by the bootstrap
        # scroll (and any organic-burst PCTFRQ during the loop) has no
        # beforeTime/afterTime set — auto-extracting its posts would drop
        # off-range posts into the result. Token tracking, viewer detection,
        # and network_capture all keep working with this off.
        self.response_interceptor.extract_posts = False

        # Phase 1 — navigate
        error = await self._hybrid_navigate(
            target_url=target_url,
            post_nav_sleep_seconds=post_nav_sleep_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if error:
            return ScrapeOutcome(
                result=error,
                posts=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )

        # Phase 2 — bootstrap scroll
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                posts=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )

        # Phase 3 — capture pagination template
        error, template = await self._hybrid_capture_template(
            template_capture_timeout=template_capture_timeout,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if error:
            return ScrapeOutcome(
                result=error,
                posts=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )
        logger.info(
            f"[hybrid] @{handle}: template captured "
            f"(doc_id={template['doc_id']}, profile_id={template['profile_id']})"
        )

        # Phase 4 — pagination loop
        result_str = await self._hybrid_pagination_loop(
            handle=handle,
            template=template,
            params=loop_params,
            start_unix=start_unix,
            end_unix=end_unix,
        )
        return ScrapeOutcome(
            result=result_str,
            posts=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time
        )

    # ---------------- Hybrid mode phases ----------------

    async def _hybrid_navigate(
        self,
        target_url: str,
        post_nav_sleep_seconds: float,
        operation_timeout_seconds: float,
    ) -> str | None:
        """Navigate to the profile and run the post-nav error check.
        Returns an error result string, or None on success."""
        logger.info(f"[hybrid] navigating to {target_url}")
        try:
            await self.goto(target_url, wait_until="domcontentloaded")
        except Exception as e:
            return f'navigation_error: {e}'
        await asyncio.sleep(post_nav_sleep_seconds)

        await self.page.keyboard.press('Escape')
        await asyncio.sleep(abs(random.gauss(1, 0.2)))

        try:
            error = await asyncio.wait_for(
                self.check_error_conditions(),
                timeout=operation_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return f'hang: post-nav error check timed out after {operation_timeout_seconds}s'
        return error  # may be None (success) or an FB-surfaced error code

    async def _hybrid_bootstrap(self, operation_timeout_seconds: float) -> str | None:
        """Provoke the first ProfileCometTimelineFeedRefetchQuery via a real
        scroll. Required because the profile page does not fire pagination
        GraphQL on initial load alone (see memory: pagination_needs_scroll)."""
        logger.debug("[hybrid] bootstrap scroll to provoke first pagination")
        try:
            await asyncio.wait_for(
                self.scroll(window_height_coefficient=1.0),
                timeout=operation_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return f'hang: bootstrap scroll timed out after {operation_timeout_seconds}s'
        return None

    async def _hybrid_capture_template(
        self,
        template_capture_timeout: float,
        operation_timeout_seconds: float,
    ) -> tuple[str | None, dict | None]:
        """Wait for the natural pagination request to land and extract a
        replay template from it.

        We copy the *form* (static tokens: fb_dtsg, lsd, doc_id, the
        __relay_internal__pv__* flags, etc.) and *headers* — but NOT the
        cursor. With a non-null `beforeTime` set on every replay, FB honors
        `cursor=null` on the first request and returns the most-recent batch
        within [afterTime, beforeTime] (matches FB's own frontend behavior
        when a date filter is active). Reading the natural cursor would skip
        the SSR-rendered batch; using null instead picks it up.

        Returns (error_or_None, template_or_None). Template dict shape:
            { 'form':       <full url-decoded form dict>,
              'headers':    <cleaned header dict ready for page.request.post>,
              'cursor':     None — first replay always uses cursor=null,
              'doc_id':     <doc_id, for logging>,
              'profile_id': <profile id, for logging> }
        On capture failure, re-checks DOM error conditions to surface a
        clean reason (private profile, etc.) instead of a generic timeout.
        """
        template = await self._hybrid_wait_for_template(template_capture_timeout)
        if not template:
            try:
                error = await asyncio.wait_for(
                    self.check_error_conditions(),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                error = None
            return (error or 'template_capture_timeout', None)

        form = self._hybrid_parse_form_data(template["post_data"])
        headers = self._hybrid_clean_headers(template["headers"])
        try:
            initial_variables = json.loads(form.get("variables", "{}"))
        except json.JSONDecodeError:
            initial_variables = {}

        return (None, {
            "form": form,
            "headers": headers,
            "cursor": None,  # see docstring — cursor=null is the right start
            "doc_id": form.get("doc_id"),
            "profile_id": initial_variables.get("id"),
        })

    async def _hybrid_pagination_loop(
        self,
        handle: str,
        template: dict,
        params: dict,
        start_unix: int,
        end_unix: int,
    ) -> str:
        """Drive paginations via page.request.post() until one of the stop
        conditions fires. Returns the result string for the ScrapingResult."""
        template_form = template["form"]
        template_headers = template["headers"]
        cursor = template["cursor"]

        # Pull tunables from the validated params dict so callers can't
        # accidentally bypass registry validation.
        pagination_count = params["pagination_count"]
        scroll_burst_every = params["scroll_burst_every"]
        scroll_burst_size_range = params["scroll_burst_size_range"]
        pagination_sleep_mean = params["pagination_sleep_mean"]
        pagination_sleep_std = params["pagination_sleep_std"]
        max_paginations = params["max_paginations"]
        max_no_progress_streak = params["max_no_progress_streak"]
        request_timeout_ms = params["request_timeout_ms"]
        operation_timeout_seconds = params["operation_timeout_seconds"]

        total_paginations = 0
        no_progress_streak = 0
        previous_post_count = len(self.response_interceptor.get_posts())

        # max_paginations == -1 means "no cap"; any positive int caps the loop.
        while max_paginations < 0 or total_paginations < max_paginations:
            # No `afterTime` override — FB's UI never sets afterTime (only
            # beforeTime), so sending it would be a unique fingerprint. The
            # captured template has `afterTime: null` baked in; not
            # overriding keeps our replay shape identical to the UI.
            # Lower-bound enforcement happens client-side: terminate when a
            # batch's oldest post is older than start_unix (further down).
            overrides = {
                "cursor": cursor,
                "beforeTime": end_unix,
                "count": pagination_count,
            }
            body = self._hybrid_build_body(template_form, overrides)

            iter_start = datetime.now(timezone.utc)
            response, text, error_str = await self._hybrid_send_replay(
                handle=handle,
                body=body,
                template_headers=template_headers,
                request_timeout_ms=request_timeout_ms,
                operation_timeout_seconds=operation_timeout_seconds,
            )
            if error_str is not None:
                return error_str

            await self.record_scroll(endpoint=self.endpoint, count=1)
            total_paginations += 1

            # Extract posts via the shared parser; push into the interceptor's
            # accumulator so get_posts() returns a unified view of everything
            # (template-capture pagination + hybrid replays).
            try:
                parsed = self.response_interceptor.parser.parse_timeline_response(
                    text.encode("utf-8"), GRAPHQL_API_URL
                )
            except Exception as e:
                logger.warning(f"[hybrid] @{handle}: parser raised: {e}")
                parsed = None
            if parsed and parsed.get("posts"):
                self.response_interceptor.add_posts(parsed["posts"])

            # 200 + errors[] populated → drain posts above, then classify.
            # Auth-ish errors mean the session is invalid → raise so Worker
            # rotates the account. Other errors → bail with a result string.
            graphql_error = self._hybrid_extract_graphql_error(text)
            if graphql_error:
                logger.warning(f"[hybrid] @{handle}: GraphQL error: {graphql_error}")
                if self._hybrid_is_auth_error(graphql_error):
                    raise FailedLoginError(
                        f"Session invalid mid-scrape (graphql error: {graphql_error})"
                    )
                return f'graphql_error: {graphql_error}'

            current_post_count = len(self.response_interceptor.get_posts())
            new_posts_in_iter = current_post_count - previous_post_count
            no_progress_streak = 0 if new_posts_in_iter else no_progress_streak + 1
            previous_post_count = current_post_count

            # Stop 1: end_cursor missing → no more posts in the filter range.
            end_cursor = self._hybrid_extract_end_cursor(text)
            if not end_cursor:
                elapsed_iter = (datetime.now(timezone.utc) - iter_start).total_seconds()
                logger.info(
                    f"[hybrid] @{handle}: end_cursor null after "
                    f"{total_paginations} paginations (last iter {elapsed_iter:.2f}s) — "
                    f"end of feed within filter range"
                )
                return 'scraped until user-specified starting date was reached'

            # Stop 2 (defensive): oldest post in this response < start_unix.
            oldest_in_batch = self._hybrid_extract_oldest_creation_time(text)
            if oldest_in_batch is not None and oldest_in_batch < start_unix:
                logger.info(
                    f"[hybrid] @{handle}: oldest post in batch "
                    f"({datetime.fromtimestamp(oldest_in_batch, tz=timezone.utc).isoformat()}) "
                    f"is older than start; done"
                )
                return 'scraped until user-specified starting date was reached'

            # No-progress backstop: bail rather than spin if FB's filter is
            # returning empty page after empty page.
            if no_progress_streak >= max_no_progress_streak:
                logger.warning(
                    f"[hybrid] @{handle}: {no_progress_streak} paginations "
                    f"with no new posts — bailing"
                )
                return 'no_new_posts_streak'

            cursor = end_cursor

            elapsed_iter = (datetime.now(timezone.utc) - iter_start).total_seconds()
            logger.debug(
                f"[hybrid] @{handle} pagination {total_paginations}: "
                f"{response.status} in {elapsed_iter:.2f}s, "
                f"posts now={current_post_count} (+{new_posts_in_iter})"
            )

            if (total_paginations % scroll_burst_every) == 0:
                await self._hybrid_organic_scroll_burst(
                    *scroll_burst_size_range,
                    operation_timeout_seconds=operation_timeout_seconds,
                )

            sleep_s = abs(random.gauss(pagination_sleep_mean, pagination_sleep_std))
            await asyncio.sleep(sleep_s)

        logger.warning(
            f"[hybrid] @{handle}: hit max_paginations cap ({max_paginations})"
        )
        return f'hit max_paginations cap ({max_paginations})'

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

    # ---------------- Hybrid mode helpers ----------------
    # Used by user_timeline_hybrid() to drive pagination via
    # page.request.post() instead of scroll-driven rendering.

    @staticmethod
    def _hybrid_parse_form_data(post_data: str | None) -> dict[str, str]:
        """Parse a urlencoded form body into a dict (last-value-wins on
        duplicates). Returns empty dict for None/empty input."""
        if not post_data:
            return {}
        try:
            parsed = parse_qs(post_data, keep_blank_values=True)
            return {k: v[-1] for k, v in parsed.items()}
        except Exception:
            return {}

    @staticmethod
    def _hybrid_clean_headers(raw: dict[str, str]) -> dict[str, str]:
        """Drop HTTP/2 pseudo-headers and headers managed by Playwright /
        BrowserContext (cookie, host, content-length, etc.) so they aren't
        double-set when we hand them to page.request.post()."""
        out = {}
        for k, v in raw.items():
            if k.startswith(":"):
                continue
            if k.lower() in HYBRID_HEADER_DROP:
                continue
            out[k] = v
        return out

    def _hybrid_build_body(
        self,
        template_form: dict[str, str],
        variable_overrides: dict,
    ) -> str:
        """Build the form body for a hybrid replay POST.

        Steps:
          1. Apply `variable_overrides` (cursor, beforeTime, afterTime, count)
             to the JSON-encoded `variables` field of the template.
          2. Splice in the freshest `__csr` and `__dyn` seen on any natural
             browser-issued GraphQL POST (tracked by ResponseInterceptor).
             FB rotates these per-session — `__csr` every ~3-4 paginations,
             `__dyn` every ~10-25 — so replaying the cached template values
             eventually drifts and gets rejected. Live-splicing keeps replays
             aligned with the most recent organic traffic.
          3. URL-encode the whole body for sending.
        """
        body = dict(template_form)
        try:
            variables = json.loads(body.get("variables", "{}"))
        except json.JSONDecodeError:
            variables = {}
        variables.update(variable_overrides)
        body["variables"] = json.dumps(variables, separators=(",", ":"))

        # Token splicing: prefer freshness if any natural GraphQL traffic has
        # been seen since the template was captured.
        if self.response_interceptor.latest_csr:
            body["__csr"] = self.response_interceptor.latest_csr
        if self.response_interceptor.latest_dyn:
            body["__dyn"] = self.response_interceptor.latest_dyn

        return urlencode(body)

    async def _hybrid_wait_for_template(self, timeout_seconds: float) -> dict | None:
        """Poll until a natural ProfileCometTimelineFeedRefetchQuery request
        has been observed (saved to `interceptor.latest_pctfrq_request` by
        `_track_pctfrq_template`), or until the timeout elapses.

        Returns a dict shaped {"post_data": str|None, "headers": dict} — the
        narrower template hook we promote to production. Falls back to
        `network_capture` if it's been populated (i.e., FB_NETWORK_CAPTURE_ALL=1)
        and the dedicated hook hasn't fired yet.
        """
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout_seconds:
            tpl = self.response_interceptor.latest_pctfrq_request
            if tpl is not None:
                return tpl
            # Fallback: scan the full capture if it happens to be enabled.
            for rec in self.response_interceptor.network_capture:
                req = rec.get("request") or {}
                headers = req.get("headers") or {}
                if headers.get("x-fb-friendly-name") == HYBRID_TARGET_FRIENDLY_NAME:
                    return {"post_data": req.get("post_data"), "headers": headers}
                form = self._hybrid_parse_form_data(req.get("post_data"))
                if form.get("fb_api_req_friendly_name") == HYBRID_TARGET_FRIENDLY_NAME:
                    return {"post_data": req.get("post_data"), "headers": headers}
            await asyncio.sleep(interval)
            elapsed += interval
        return None

    @staticmethod
    def _hybrid_walk_response_for(text: str, target_keys: set[str]):
        """Walk a JSON or JSONL response body, yielding (key, value) pairs
        for every nested dict key matching target_keys.

        FB GraphQL can be a single JSON doc OR JSONL when @stream/@defer
        is in play, so try JSONL first then fall back to single-doc.
        """
        if not text:
            return
        docs: list = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                docs = []
                break
        if not docs:
            try:
                docs = [json.loads(text)]
            except json.JSONDecodeError:
                return

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in target_keys:
                        yield (k, v)
                    yield from walk(v)
            elif isinstance(o, list):
                for item in o:
                    yield from walk(item)

        for d in docs:
            yield from walk(d)

    @classmethod
    def _hybrid_extract_end_cursor(cls, text: str) -> str | None:
        """Find the first non-empty `end_cursor` (or `endCursor`) in a
        GraphQL response body. Returns None if no cursor is present —
        which we interpret as end-of-feed."""
        for k, v in cls._hybrid_walk_response_for(text, {"end_cursor", "endCursor"}):
            if v:
                return v
        return None

    @classmethod
    def _hybrid_extract_oldest_creation_time(cls, text: str) -> int | None:
        """Find the smallest `creation_time` (unix seconds) in a GraphQL
        response body. Returns None if no creation_time was present."""
        oldest: int | None = None
        for _, v in cls._hybrid_walk_response_for(text, {"creation_time"}):
            if isinstance(v, (int, float)):
                t = int(v)
                if oldest is None or t < oldest:
                    oldest = t
        return oldest

    @classmethod
    def _hybrid_extract_graphql_error(cls, text: str) -> str | None:
        """Find the first GraphQL error message in a response body. Returns
        None if no errors are surfaced. FB returns 200 even on errors, so
        this is how we detect them."""
        for _, v in cls._hybrid_walk_response_for(text, {"errors"}):
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    if msg:
                        return str(msg)[:200]
                elif isinstance(first, str):
                    return first[:200]
        return None

    # Substrings that indicate the GraphQL error is auth-related (session/token
    # invalid). FB historically uses 200 + errors[] for these instead of HTTP
    # 401, so we have to pattern-match on the message. Empirical mapping is
    # incomplete (see CLAUDE.md TODO "HTTP error classification"); update as
    # we observe new ones in the wild.
    _HYBRID_AUTH_ERROR_MARKERS = (
        "lsddataerror",       # historical FB auth-token error
        "useridiszero",       # session lost, viewer is anonymous
        "not logged in",
        "must be logged in",
        "invalid session",
        "session has expired",
    )

    @classmethod
    def _hybrid_is_auth_error(cls, message: str) -> bool:
        """True if a GraphQL error message looks auth-related (session lost,
        token invalid, etc.). Pattern-matched because FB returns these as
        200+errors[] rather than HTTP 401."""
        if not message:
            return False
        m = message.lower()
        return any(marker in m for marker in cls._HYBRID_AUTH_ERROR_MARKERS)

    @staticmethod
    def _hybrid_body_looks_like_html(text: str) -> bool:
        """True if a response body is HTML (FB redirected our POST to a
        login / interstitial page). page.request.post does not auto-follow
        the redirect; the body lands here as HTML and JSON parsing would
        fail. Detect early so we can raise a clean logged-out signal."""
        head = text.lstrip()[:64].lower()
        return head.startswith("<!doctype") or head.startswith("<html")

    async def _hybrid_send_replay(
        self,
        handle: str,
        body: str,
        template_headers: dict,
        request_timeout_ms: int,
        operation_timeout_seconds: float,
    ) -> tuple[object | None, str, str | None]:
        """Send one replay request with built-in 5xx-retry and HTTP-status
        classification. Returns `(response, body_text, error_string)`:

        - On success: `(response, body_text, None)` and the loop continues.
        - On terminal failure with no rotation: `(None, "", error_string)`
          and the loop returns that string as the ScrapeOutcome result.
        - On terminal failure that warrants rotation: raises a typed
          exception (`FailedLoginError` / `AccountBannedError` /
          `RateLimitError`) so `Worker.execute_task`'s existing handlers
          activate.

        Status mapping (see CLAUDE.md TODO "HTTP error classification" — this
        is the working hypothesis until empirical data refines it):
          - 200 + HTML body         → FailedLoginError (session bounced to login)
          - 200 + auth errors[]     → FailedLoginError (handled in caller, not here)
          - 200 + GraphQL data      → success
          - 401                     → FailedLoginError
          - 403                     → AccountBannedError
          - 429                     → RateLimitError
          - 500/502/503/504         → retry up to 3x with 5s/15s/45s backoff
          - other 4xx/5xx           → error string (no retry, no rotation)
        """
        retry_delays = [5, 15, 45]
        retryable_5xx = {500, 502, 503, 504}
        attempt = 0

        while True:
            try:
                response = await asyncio.wait_for(
                    self.page.request.post(
                        GRAPHQL_API_URL,
                        headers=template_headers,
                        data=body,
                        timeout=request_timeout_ms,
                    ),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[hybrid] @{handle}: page.request.post hung > "
                    f"{operation_timeout_seconds}s — returning partial"
                )
                return None, "", f'hang: page.request.post timed out after {operation_timeout_seconds}s'
            except Exception as e:
                logger.warning(f"[hybrid] @{handle}: page.request.post failed: {e}")
                return None, "", f'pagination_error: {e}'

            status = response.status

            # 5xx — retry with backoff up to len(retry_delays) times.
            if status in retryable_5xx:
                if attempt < len(retry_delays):
                    delay = retry_delays[attempt]
                    attempt += 1
                    logger.warning(
                        f"[hybrid] @{handle}: HTTP {status}, retry {attempt}/"
                        f"{len(retry_delays)} after {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    f"[hybrid] @{handle}: HTTP {status} after "
                    f"{len(retry_delays) + 1} attempts — bailing"
                )
                return None, "", f'pagination_error: HTTP {status} after retries'

            # 401 / 403 / 429 — typed exceptions so Worker's handlers fire.
            if status == 401:
                raise FailedLoginError(
                    f"Hybrid replay returned HTTP 401 — session invalid"
                )
            if status == 403:
                raise AccountBannedError(
                    f"Hybrid replay returned HTTP 403 — account likely banned"
                )
            if status == 429:
                raise RateLimitError(
                    f"Hybrid replay returned HTTP 429 — rate limited"
                )

            # Other non-200 (400, 404, 5xx-non-retryable, etc.) — bail with
            # a result string but no rotation.
            if status != 200:
                logger.warning(
                    f"[hybrid] @{handle}: HTTP {status} — bailing (no retry)"
                )
                return None, "", f'pagination_error: HTTP {status}'

            # 200 — read body. FB sometimes returns HTML (redirect to login)
            # in our POST response; treat as logged out.
            try:
                text = await response.text()
            except Exception as e:
                return None, "", f'pagination_error: read body: {e}'

            if self._hybrid_body_looks_like_html(text):
                raise FailedLoginError(
                    "Hybrid replay returned HTML body on 200 — session bounced to login"
                )

            return response, text, None

    async def _hybrid_organic_scroll_burst(
        self,
        min_scrolls: int = 2,
        max_scrolls: int = 5,
        operation_timeout_seconds: float = 900,
    ):
        """Fire a small burst of real scrolls to mimic an intermittent reader.

        Pure page.request traffic without any scroll events would be a
        unique pattern. Sprinkling a few scrolls between page.request bursts
        makes the session pattern look like a normal scroller. Each scroll
        is wrapped in `asyncio.wait_for(operation_timeout_seconds)` so a
        wedged renderer doesn't stall the whole scrape.
        """
        n_scrolls = random.randint(min_scrolls, max_scrolls)
        logger.debug(f"[hybrid] organic-scroll burst: {n_scrolls} scrolls")
        for j in range(n_scrolls):
            try:
                await asyncio.wait_for(
                    self.scroll(window_height_coefficient=1.0),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[hybrid] burst scroll {j+1}/{n_scrolls} timed out — "
                    f"aborting burst"
                )
                break
            except Exception as e:
                logger.warning(f"[hybrid] burst scroll {j+1} failed: {e}")
                break
            await asyncio.sleep(abs(random.gauss(2.5, 0.5)))
