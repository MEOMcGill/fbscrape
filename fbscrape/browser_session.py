"""
Browser management and page control for Facebook scraping
"""
from .accounts_pool import AccountsPool
from .response import ResponseInterceptor, FacebookGraphQLParser
from .account import Account
from .logger import logger
from .models import ScrapeOutcome
from .utils import (
    recursively_get_dict_value,
    get_device_os,
    generate_fingerprint,
    serialize_fingerprint,
    deserialize_fingerprint,
)
from .exceptions import (
    FailedLoginError, AccountBannedError, RateLimitError, RendererHangError,
)
from . import login as _login

import asyncio
import base64
import hashlib
import json
import os
import random
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, quote, urlencode
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright, Browser
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
# Same role for the Search endpoint's hybrid mode.
SEARCH_HYBRID_TARGET_FRIENDLY_NAME = "SearchCometResultsPaginatedResultsQuery"
# Single-shot ProfileTransparencyDialogQuery — fires from a UI click in the
# natural flow, so we synthesize the body from any captured natural GraphQL
# POST and override the friendly-name + variables + doc_id. doc_id is stable
# (same value across every observed capture).
PAGE_TRANSPARENCY_FRIENDLY_NAME = "ProfileTransparencyDialogQuery"
PAGE_TRANSPARENCY_DOC_ID = "25844809825201975"

# Cursor-reset diagnostic dump knobs. When the hybrid pagination loop sees
# `oldest_in_batch` jump *newer* by more than HYBRID_CURSOR_RESET_JUMP_SECONDS
# vs. the previous iteration, we dump the last HYBRID_CURSOR_RESET_WINDOW
# (request, response) tuples to HYBRID_CURSOR_RESET_DUMP_ROOT. Diagnostic only
# — does not change loop behaviour. One dump per scrape (re-arming would
# flood disk on a session that's stuck in a recycle loop).
HYBRID_CURSOR_RESET_WINDOW = 20
HYBRID_CURSOR_RESET_JUMP_SECONDS = 7 * 86400
HYBRID_CURSOR_RESET_DUMP_ROOT = "tmp/hybrid/cursor_reset"


class BrowserSession:
    """Manages browser session and page navigation"""

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
            auto_login: bool = True,
    ):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.mobile = mobile
        # If False, initialize() opens the browser/context/page but skips the
        # cookie-injection + automatic form-login orchestration. Used by the
        # `fbscrape login` CLI command, which needs a clean-slate browser to
        # drive login itself (manual or automatic). The default `True`
        # preserves the worker's existing behavior.
        self.auto_login = auto_login

        # the endpoint we're scrolling (set by scraping methods like user_timeline)
        self.endpoint: str = ""

        # browser-related objects
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[ResponseInterceptor] = None

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
            device_os = get_device_os()
            device_headless = self._resolve_headless(os=device_os, headless=self.headless)
            logger.debug(
                f"Fingerprint: {fingerprint}, "
                f"OS: {device_os}, "
                f"Headless: {device_headless}"
            )

            # Create browser context using camoufox
            self._browser: Browser = await AsyncNewBrowser(
                playwright=self._pw,
                humanize=True,
                headless=device_headless,
                proxy=proxy_settings,
                geoip=True if proxy_settings else False,
                os=device_os,
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

            if not self.auto_login:
                # Caller opts out of the embedded login orchestration; they're
                # responsible for driving login themselves (e.g. `fbscrape login`).
                logger.info(f"Browser session initialized for {self.account.display_name} (auto_login=False)")
                return

            # Auth branch depends on whether we have cookies to try:
            #   - Cookies present: inject, verify with check_logged_in, fall back to recovery
            #     (obstacle handlers + form login) if cookies didn't yield a logged-in session.
            #   - No cookies: go straight to the form login flow. login_automatic() owns its
            #     own _on_login_success call on success, so we don't re-do bookkeeping here.
            if self.account.cookies:
                logger.debug(f"Account has {len(self.account.cookies)} cookies, injecting...")
                try:
                    await self._context.add_cookies(self.account.cookies)
                    logger.info(f"Injected {len(self.account.cookies)} cookies for {self.account.display_name}")
                except Exception as e:
                    logger.warning(f"Failed to inject cookies for {self.account.display_name}: {e}")

                # Fast happy-path: cookies worked.
                if await _login.check_logged_in(self, timeout=5.0):
                    await _login._on_login_success(self)
                    logger.info(f"Browser session initialized for {self.account.display_name} (already logged in)")
                    return

                # Cookies didn't get us in — obstacle handlers + form-login fallback.
                await _login.resolve_not_logged_in(self)
                logger.info(f"Browser session initialized for {self.account.display_name}")
            else:
                logger.debug("No cookies available, going straight to form login")
                if not await _login.login_automatic(self):
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

    async def get_cookies(self) -> list[dict]:
        """Get cookies from current browser context"""
        storage_state = await self._context.storage_state()
        return storage_state['cookies']

    async def save_cookies(self):
        """Save current cookies to the database"""
        cookies = await self.get_cookies()
        await self.pool.update_cookies(self.account.identifier, cookies)
        logger.info(f"Saved cookies for {self.account.display_name}")


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
                            data=self.response_interceptor.get_posts(),
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
                        raise RendererHangError(
                            f"post-nav error check timed out after {operation_timeout_seconds}s"
                        )
                    if error:
                        return ScrapeOutcome(
                            result=error,
                            data=self.response_interceptor.get_posts(),
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
                            raise RendererHangError(
                                f"stalled error check timed out after {operation_timeout_seconds}s"
                            )
                        logger.debug(
                            f"@{handle} iter {total_scrolls}: after check_error_conditions() "
                            f"({(datetime.now(timezone.utc) - t_err).total_seconds():.2f}s), error={error!r}"
                        )
                        if error:
                            return ScrapeOutcome(
                                result=error,
                                data=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                    if no_new_posts_count > max_no_new_posts_streak:
                        if current_post_count == 0:
                            return ScrapeOutcome(
                                result='no posts',
                                data=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        else:
                            return ScrapeOutcome(
                                result='scraped until first ever post was reached',
                                data=self.response_interceptor.get_posts(),
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
                                data=self.response_interceptor.get_posts(),
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
                        data=self.response_interceptor.get_posts(),
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
                    raise RendererHangError(
                        f"scroll timed out after {operation_timeout_seconds}s"
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

            except (FailedLoginError, AccountBannedError, RateLimitError,
                    RendererHangError):
                raise
            except Exception as e:
                logger.error(f"Error scraping @{handle}: {e}")
                return ScrapeOutcome(
                    result=f'error: {str(e)}',
                    data=self.response_interceptor.get_posts(),
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
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )

        # Phase 2 — bootstrap scroll
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
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
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )
        logger.info(
            f"[hybrid] @{handle}: template captured "
            f"(doc_id={template['doc_id']}, profile_id={template['profile_id']})"
        )

        # Phase 4 — pagination loop
        result_str = await self._hybrid_pagination_loop(
            label=handle,
            template=template,
            params=loop_params,
            start_unix=start_unix,
            end_unix=end_unix,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time
        )

    async def search_hybrid(
        self,
        query_text: str,
        start_date: str,
        end_date: str,
        pagination_count: int = 5,
        scroll_burst_every: int = 10,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 20.0,
        max_paginations: int = -1,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """
        Scrape Facebook search results for `query_text` between two dates.

        Targets the SearchCometResultsPaginatedResultsQuery GraphQL endpoint
        and applies a "Latest posts" + creation_time date filter via the URL
        filter blob (see `_build_search_url`) — date bounds are server-enforced,
        so the GraphQL replay variables only override `cursor` and `count`.
        Otherwise mirrors `user_timeline_hybrid` step-for-step (navigate,
        bootstrap scroll, capture template, replay loop).

        Stop conditions:
          1. `end_cursor` missing/null in the response — no more results.
          2. `max_no_progress_streak` consecutive paginations with zero new
             posts — backstop against silent zero-batch loops.

        Direct-call caveat: when invoked outside the Worker pipeline, callers
        are responsible for pre-validating inputs. Going through
        `FacebookScraper.search` runs full Query validation.

        Args:
            query_text: Free-form search term.
            start_date: Lower-bound date (YYYY-MM-DD) — encoded into the URL
                filter; not sent as a GraphQL variable.
            end_date:   Upper-bound date (YYYY-MM-DD) — same as start_date.
            (... see Query.ENDPOINT_REGISTRY[("Search","hybrid")] for the full
             list of param defaults and meanings.)

        Returns:
            ScrapeOutcome — Worker composes the final ScrapingResult by
            attaching the canonical Query via ScrapingResult.from_outcome.
        """
        self.endpoint = "Search"
        logger.info(
            f"[hybrid] search {query_text!r}: starting hybrid scrape "
            f"({start_date} → {end_date}, count={pagination_count})"
        )

        target_url = self._build_search_url(query_text, start_date, end_date)
        scrape_start_time = datetime.now(timezone.utc)

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
        # Same reasoning as user_timeline_hybrid: posts come exclusively from
        # the explicit replay loop. The natural bootstrap SCRQ is fired against
        # the URL filter so it would in fact be in-range, but keeping
        # extract_posts off makes the source-of-posts identical across hybrid
        # endpoints and avoids any double-extraction surprises.
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
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )

        # Phase 2 — bootstrap scroll
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )

        # Phase 3 — capture pagination template (SCRQ instead of PCTFRQ)
        error, template = await self._hybrid_capture_template(
            template_capture_timeout=template_capture_timeout,
            operation_timeout_seconds=operation_timeout_seconds,
            friendly_name=SEARCH_HYBRID_TARGET_FRIENDLY_NAME,
            interceptor_attr="latest_scrq_request",
        )
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time
            )
        logger.info(
            f"[hybrid] search {query_text!r}: template captured "
            f"(doc_id={template['doc_id']})"
        )

        # Phase 4 — pagination loop. start_unix / end_unix left as defaults
        # (None) since the URL filter is the date authority for Search.
        result_str = await self._hybrid_pagination_loop(
            label=query_text,
            template=template,
            params=loop_params,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time
        )

    async def page_transparency_hybrid(
        self,
        handle: str,
        page_id: str,
        post_nav_sleep_seconds: float = 3.0,
        template_capture_timeout: float = 20.0,
        request_timeout_ms: int = 30000,
        operation_timeout_seconds: float = 120,
    ) -> ScrapeOutcome:
        """Scrape Facebook page transparency info (single-shot, no pagination).

        Targets `ProfileTransparencyDialogQuery` — the GraphQL query that
        backs the "Page transparency" dialog (page name, profile picture,
        creation date in `history_items[item_type==CREATION]`, name-change
        history, admin country breakdown, ad activity flags, verification).

        The natural query only fires from a UI click on the "Page transparency"
        button, so the hybrid pattern of "wait for natural fire, capture,
        replay" doesn't apply. Instead we capture auth-bearing fields
        (fb_dtsg, lsd, __user, __csr, __dyn, etc.) from any natural GraphQL
        POST that fires during navigation, then synthesize the transparency
        body by overriding `fb_api_req_friendly_name`, `variables`, and
        `doc_id`.

        Caller supplies both `handle` and `page_id`:
          - `handle` drives the bootstrap navigation (warm-up + natural
            traffic pattern). The transparency replay POST then fires
            alongside organic GraphQL traffic rather than as a cold request.
          - `page_id` is the numeric page id, sent as `variables.pageID`.
            We don't resolve handle → page_id ourselves.

        Direct-call caveat: when invoked outside the Worker pipeline, callers
        are responsible for pre-validating inputs. Going through
        `FacebookScraper.page_transparency` runs full Query validation.

        Returns:
            ScrapeOutcome — `data` is a 1-element list `[transparency_dict]`
            on success, or `[]` on failure (with `result` carrying the reason).
        """
        self.endpoint = "PageTransparency"
        logger.info(
            f"[hybrid] page transparency for @{handle} (page_id={page_id})"
        )

        target_url = f"https://www.facebook.com/{handle}/"
        scrape_start_time = datetime.now(timezone.utc)

        self.response_interceptor.flush()
        # We synthesize the transparency body from a captured natural request,
        # so auto-extraction of timeline posts has no role here. Disable to
        # avoid any natural PCTFRQ that might fire (no bootstrap scroll, but
        # FB sometimes fires PCTFRQ from initial render in edge cases) from
        # leaking posts into the interceptor's accumulator.
        self.response_interceptor.extract_posts = False

        # Phase 1 — navigate. Drives organic GraphQL traffic that the
        # template-capture phase harvests for auth-bearing fields.
        error = await self._hybrid_navigate(
            target_url=target_url,
            post_nav_sleep_seconds=post_nav_sleep_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if error:
            return ScrapeOutcome(
                result=error,
                data=[],
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        # Phase 2 — capture template from any natural GraphQL POST.
        template = await self._hybrid_wait_for_any_graphql_request(
            template_capture_timeout
        )
        if not template:
            try:
                error = await asyncio.wait_for(
                    self.check_error_conditions(),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise RendererHangError(
                    f"template-capture error check timed out after "
                    f"{operation_timeout_seconds}s"
                )
            return ScrapeOutcome(
                result=error or 'template_capture_timeout',
                data=[],
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        # Phase 3 — synthesize the transparency request and send it.
        body = self._page_transparency_build_body(
            template["post_data"], page_id
        )
        headers = self._hybrid_clean_headers(template["headers"])
        # Override friendly-name header to match the synthesized body. FB
        # cross-checks the header against the form field; mismatch → 400.
        headers["x-fb-friendly-name"] = PAGE_TRANSPARENCY_FRIENDLY_NAME

        response, text, error_str = await self._hybrid_send_replay(
            handle=handle,
            body=body,
            template_headers=headers,
            request_timeout_ms=request_timeout_ms,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        if error_str is not None:
            return ScrapeOutcome(
                result=error_str,
                data=[],
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        # Phase 4 — parse the response into a single record.
        record = self._parse_page_transparency_response(text)
        if record is None:
            return ScrapeOutcome(
                result='parse_error',
                data=[],
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        return ScrapeOutcome(
            result='success',
            data=[record],
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time,
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
            raise RendererHangError(
                f"post-nav error check timed out after {operation_timeout_seconds}s"
            )
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
            raise RendererHangError(
                f"bootstrap scroll timed out after {operation_timeout_seconds}s"
            )
        return None

    async def _hybrid_capture_template(
        self,
        template_capture_timeout: float,
        operation_timeout_seconds: float,
        friendly_name: str = HYBRID_TARGET_FRIENDLY_NAME,
        interceptor_attr: str = "latest_pctfrq_request",
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

        The PCTFRQ defaults preserve the user_timeline_hybrid call site;
        search_hybrid passes SEARCH_HYBRID_TARGET_FRIENDLY_NAME and
        "latest_scrq_request".
        """
        template = await self._hybrid_wait_for_template(
            template_capture_timeout,
            friendly_name=friendly_name,
            interceptor_attr=interceptor_attr,
        )
        if not template:
            try:
                error = await asyncio.wait_for(
                    self.check_error_conditions(),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise RendererHangError(
                    f"template-capture error check timed out after {operation_timeout_seconds}s"
                )
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
        label: str,
        template: dict,
        params: dict,
        start_unix: int | None = None,
        end_unix: int | None = None,
    ) -> str:
        """Drive paginations via page.request.post() until one of the stop
        conditions fires. Returns the result string for the ScrapingResult.

        `label` is a free-form identifier used only for logging (handle for
        UserTimeline, query_text for Search).

        `start_unix` / `end_unix` are optional. When `end_unix` is set, the
        replay body carries it as `beforeTime` (matches FB's date-filter UI
        behavior for UserTimeline). When `start_unix` is set, the loop also
        terminates if a batch's oldest post is older than it. Search leaves
        both unset because date bounds are server-enforced via the URL filter
        blob — the GraphQL variables for SCRQ have no date fields to override.
        """
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

        # Cursor-reset detector state. `iter_window` is a rolling buffer of
        # the most recent (request, response) tuples — dumped to disk on
        # detection. `prev_oldest_unix` is the prior iter's oldest
        # creation_time; if the new iter's oldest jumps newer by more than
        # HYBRID_CURSOR_RESET_JUMP_SECONDS, FB has silently degraded the
        # response stream (cursor still looks valid but content recycles to
        # head). The loop returns 'cursor_reset' so the scraper layer can
        # resume from a fresh session with end_date moved back.
        iter_window: deque = deque(maxlen=HYBRID_CURSOR_RESET_WINDOW)
        prev_oldest_unix: int | None = None

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
                "count": pagination_count,
            }
            if end_unix is not None:
                overrides["beforeTime"] = end_unix
            body = self._hybrid_build_body(template_form, overrides)

            cursor_sent_fp = self._hybrid_cursor_fp(cursor)
            csr_len = len(self.response_interceptor.latest_csr or "")
            dyn_len = len(self.response_interceptor.latest_dyn or "")

            iter_start = datetime.now(timezone.utc)
            response, text, error_str = await self._hybrid_send_replay(
                handle=label,
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
                logger.warning(f"[hybrid] @{label}: parser raised: {e}")
                parsed = None
            if parsed and parsed.get("posts"):
                self.response_interceptor.add_posts(parsed["posts"])

            # 200 + errors[] populated → drain posts above, then classify.
            # Auth-ish errors mean the session is invalid → raise so Worker
            # rotates the account. Other errors → bail with a result string.
            graphql_error = self._hybrid_extract_graphql_error(text)
            if graphql_error:
                logger.warning(f"[hybrid] @{label}: GraphQL error: {graphql_error}")
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
                    f"[hybrid] @{label}: end_cursor null after "
                    f"{total_paginations} paginations (last iter {elapsed_iter:.2f}s) — "
                    f"end of feed within filter range"
                )
                return 'scraped until user-specified starting date was reached'

            # Stop 2 (defensive, UserTimeline-only): oldest post in this
            # response < start_unix. Search has no client-side start bound —
            # the URL filter enforces it server-side, so we skip this check.
            oldest_in_batch = self._hybrid_extract_oldest_creation_time(text)
            if (
                start_unix is not None
                and oldest_in_batch is not None
                and oldest_in_batch < start_unix
            ):
                logger.info(
                    f"[hybrid] @{label}: oldest post in batch "
                    f"({datetime.fromtimestamp(oldest_in_batch, tz=timezone.utc).isoformat()}) "
                    f"is older than start; done"
                )
                return 'scraped until user-specified starting date was reached'

            # No-progress backstop: bail rather than spin if FB's filter is
            # returning empty page after empty page.
            if no_progress_streak >= max_no_progress_streak:
                logger.warning(
                    f"[hybrid] @{label}: {no_progress_streak} paginations "
                    f"with no new posts — bailing"
                )
                return 'no_new_posts_streak'

            cursor = end_cursor

            elapsed_iter = (datetime.now(timezone.utc) - iter_start).total_seconds()
            oldest_iso = (
                datetime.fromtimestamp(oldest_in_batch, tz=timezone.utc).isoformat()
                if oldest_in_batch is not None else "n/a"
            )
            newest_in_batch = self._hybrid_extract_newest_creation_time(text)
            newest_iso = (
                datetime.fromtimestamp(newest_in_batch, tz=timezone.utc).isoformat()
                if newest_in_batch is not None else "n/a"
            )
            posts_in_resp = len(parsed.get("posts") or []) if parsed else 0
            logger.debug(
                f"[hybrid] @{label} pagination {total_paginations}: "
                f"{response.status} in {elapsed_iter:.2f}s, "
                f"posts now={current_post_count} (+{new_posts_in_iter}, "
                f"in_resp={posts_in_resp}), "
                f"batch=[{newest_iso} .. {oldest_iso}], "
                f"cursor_sent={cursor_sent_fp}, "
                f"cursor_recv={self._hybrid_cursor_fp(end_cursor)}, "
                f"__csr_len={csr_len}, __dyn_len={dyn_len}, "
                f"resp_bytes={len(text or '')}"
            )

            # Cursor-reset diagnostic capture. Append a self-contained record
            # of this iteration to the rolling window, then trigger a dump if
            # `oldest_in_batch` jumped newer by more than the threshold vs.
            # the previous iter (the symptom we're trying to characterize).
            iter_window.append({
                "pagination_index": total_paginations,
                "ts": iter_start.isoformat(),
                "elapsed_s": elapsed_iter,
                "status": response.status,
                "request": {
                    "body": body,
                    "headers": template_headers,
                    "cursor_sent_fp": cursor_sent_fp,
                },
                "response": {
                    "text": text,
                    "size_bytes": len(text or ""),
                    "cursor_recv_fp": self._hybrid_cursor_fp(end_cursor),
                    "oldest_iso": oldest_iso,
                    "newest_iso": newest_iso,
                    "oldest_unix": oldest_in_batch,
                    "newest_unix": newest_in_batch,
                    "posts_in_resp": posts_in_resp,
                },
                "session": {
                    "csr_len": csr_len,
                    "dyn_len": dyn_len,
                },
            })
            if (
                prev_oldest_unix is not None
                and oldest_in_batch is not None
                and (oldest_in_batch - prev_oldest_unix) > HYBRID_CURSOR_RESET_JUMP_SECONDS
            ):
                jump_days = (oldest_in_batch - prev_oldest_unix) / 86400.0
                out_dir = self._hybrid_dump_cursor_reset_window(
                    label=label,
                    trigger_index=total_paginations,
                    prev_oldest_unix=prev_oldest_unix,
                    cur_oldest_unix=oldest_in_batch,
                    window=iter_window,
                )
                logger.warning(
                    f"[hybrid] @{label}: cursor-reset detected at "
                    f"pagination {total_paginations} (oldest jumped "
                    f"+{jump_days:.1f} days) — dumped window to "
                    f"{out_dir or '<dump_failed>'}; bailing with partial posts"
                )
                # Stop condition: FB silently returned a degraded stream.
                # has_next_page / end_cursor remain valid, so neither
                # existing stop fires. Returning here lets the high-level
                # scraper resume from a fresh session with end_date adjusted
                # to the oldest post collected so far.
                return 'cursor_reset'
            if oldest_in_batch is not None:
                prev_oldest_unix = oldest_in_batch

            if (total_paginations % scroll_burst_every) == 0:
                await self._hybrid_organic_scroll_burst(
                    *scroll_burst_size_range,
                    operation_timeout_seconds=operation_timeout_seconds,
                )

            sleep_s = abs(random.gauss(pagination_sleep_mean, pagination_sleep_std))
            await asyncio.sleep(sleep_s)

        logger.warning(
            f"[hybrid] @{label}: hit max_paginations cap ({max_paginations})"
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
        """Return a browserforge Fingerprint for this session, lazily
        generating + persisting one per host OS the first time we see it.

        Each (account, host_os) pair gets a stable fingerprint that survives
        re-runs and OS hops: account.fingerprints maps os -> serialized JSON.
        Camoufox can't reliably mask the underlying host OS (canvas / WebGL /
        fonts leak through the Firefox sandbox regardless of overrides), so
        we keep one fingerprint per host OS rather than overwriting on drift.
        """
        host_os = get_device_os()
        fp_json = self.account.fingerprints.get(host_os)
        if fp_json:
            try:
                fp = deserialize_fingerprint(fp_json)
                logger.debug(
                    f"Loaded persisted fingerprint for {self.account.display_name} (os={host_os})"
                )
                return fp
            except Exception as e:
                logger.warning(
                    f"Corrupt {host_os} fingerprint for {self.account.display_name}: {e}; regenerating"
                )

        fp = generate_fingerprint(host_os)
        fp_json = serialize_fingerprint(fp)
        self.account.fingerprints[host_os] = fp_json
        await self.pool.update_fingerprint(self.account.identifier, self.account.fingerprints)
        logger.info(
            f"Generated + persisted new fingerprint for {self.account.display_name} "
            f"(os={host_os}, slots_after={sorted(self.account.fingerprints)})"
        )
        return fp

    def _resolve_headless(self, os: str, headless: bool):
        """Determine the headless mode to use for this session."""
        if headless and (os == "linux"):
            return "virtual"
        return headless

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
    def _build_search_url(query_text: str, start_date: str, end_date: str) -> str:
        """Build a Facebook search URL with a "Latest posts" sort + creation_time
        date-range filter — the hybrid Search endpoint's target URL.

        FB's UI encodes search filters as a base64-encoded JSON dict in the
        `filters=` query param; each value is itself a JSON-encoded dict of
        `{name, args}`. The shape mirrors the user-validated example URL:

            {
              "recent_posts:0":     '{"name":"recent_posts","args":""}',
              "rp_creation_time:0": '{"name":"creation_time","args":"<inner>"}'
            }

        where `<inner>` is a JSON-encoded dict carrying year/month/day strings
        derived from `start_date` / `end_date` (with no zero-padding on month
        and day, matching the user's example: "2025", "2025-1", "2025-1-1").

        Args:
            query_text: Free-form search term.
            start_date: YYYY-MM-DD lower bound (inclusive).
            end_date:   YYYY-MM-DD upper bound (inclusive).

        Returns:
            Full URL string ready for `page.goto(...)`.
        """
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        creation_args = {
            "start_year":  f"{start_dt.year}",
            "start_month": f"{start_dt.year}-{start_dt.month}",
            "end_year":    f"{end_dt.year}",
            "end_month":   f"{end_dt.year}-{end_dt.month}",
            "start_day":   f"{start_dt.year}-{start_dt.month}-{start_dt.day}",
            "end_day":     f"{end_dt.year}-{end_dt.month}-{end_dt.day}",
        }
        outer = {
            "recent_posts:0": json.dumps(
                {"name": "recent_posts", "args": ""},
                separators=(",", ":"),
            ),
            "rp_creation_time:0": json.dumps(
                {
                    "name": "creation_time",
                    "args": json.dumps(creation_args, separators=(",", ":")),
                },
                separators=(",", ":"),
            ),
        }
        filters_b64 = base64.b64encode(
            json.dumps(outer, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        return (
            "https://www.facebook.com/search/top"
            f"?q={quote(query_text)}"
            f"&filters={quote(filters_b64)}"
        )

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

    async def _hybrid_wait_for_any_graphql_request(
        self, timeout_seconds: float
    ) -> dict | None:
        """Poll until any natural GraphQL POST has been observed (saved to
        `interceptor.latest_natural_graphql_request`), or timeout.

        Used by single-shot endpoints (PageTransparency) that synthesize
        their own request body and only need cross-cutting auth-bearing
        fields from organic traffic. Returns the same shape as
        `_hybrid_wait_for_template`: `{"post_data": str|None, "headers": dict}`.
        """
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout_seconds:
            tpl = self.response_interceptor.latest_natural_graphql_request
            if tpl is not None:
                return tpl
            await asyncio.sleep(interval)
            elapsed += interval
        return None

    def _page_transparency_build_body(
        self,
        template_post_data: str | None,
        page_id: str,
    ) -> str:
        """Build a urlencoded form body for ProfileTransparencyDialogQuery
        by overriding the friendly-name, variables, and doc_id fields on a
        captured natural GraphQL POST template.

        Auth-bearing and telemetry fields (fb_dtsg, lsd, __user, __hs,
        __spin_*, __req, __crn, __ccg, __rev, __s, __hsi, __hsdp, __hblp,
        __sjsp, jazoest, server_timestamps, av, __a, __aaid, dpr,
        __comet_req) are inherited verbatim from the template — they are
        cross-cutting per session, not per query. The freshest __csr / __dyn
        seen on any natural GraphQL POST are spliced in (same logic as
        `_hybrid_build_body`).
        """
        template_form = self._hybrid_parse_form_data(template_post_data)
        body = dict(template_form)
        body["fb_api_req_friendly_name"] = PAGE_TRANSPARENCY_FRIENDLY_NAME
        body["variables"] = json.dumps(
            {"pageID": str(page_id), "scale": 3},
            separators=(",", ":"),
        )
        body["doc_id"] = PAGE_TRANSPARENCY_DOC_ID
        if self.response_interceptor.latest_csr:
            body["__csr"] = self.response_interceptor.latest_csr
        if self.response_interceptor.latest_dyn:
            body["__dyn"] = self.response_interceptor.latest_dyn
        return urlencode(body)

    @staticmethod
    def _parse_page_transparency_response(text: str) -> dict | None:
        """Parse a ProfileTransparencyDialogQuery response body into the
        page transparency record (the `data.page` dict). Returns None on
        parse failure or if no doc carries `data.page`.

        Handles both single-doc JSON and JSONL bodies.
        """
        if not text:
            return None
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
                return None
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            page = (doc.get("data") or {}).get("page")
            if isinstance(page, dict):
                return page
        return None

    async def _hybrid_wait_for_template(
        self,
        timeout_seconds: float,
        friendly_name: str = HYBRID_TARGET_FRIENDLY_NAME,
        interceptor_attr: str = "latest_pctfrq_request",
    ) -> dict | None:
        """Poll until a natural <friendly_name> GraphQL request has been
        observed (saved to `interceptor.<interceptor_attr>` by the matching
        `_track_*_template` hook), or until the timeout elapses.

        Returns a dict shaped {"post_data": str|None, "headers": dict} — the
        narrower template hook we promote to production. Falls back to
        `network_capture` if it's been populated (i.e., FB_NETWORK_CAPTURE_ALL=1)
        and the dedicated hook hasn't fired yet.

        Defaults to the user-timeline (PCTFRQ) target so existing callers
        don't need to change.
        """
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout_seconds:
            tpl = getattr(self.response_interceptor, interceptor_attr, None)
            if tpl is not None:
                return tpl
            # Fallback: scan the full capture if it happens to be enabled.
            for rec in self.response_interceptor.network_capture:
                req = rec.get("request") or {}
                headers = req.get("headers") or {}
                if headers.get("x-fb-friendly-name") == friendly_name:
                    return {"post_data": req.get("post_data"), "headers": headers}
                form = self._hybrid_parse_form_data(req.get("post_data"))
                if form.get("fb_api_req_friendly_name") == friendly_name:
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

    @staticmethod
    def _hybrid_iter_wrapping_creation_times(text: str):
        """Yield wrapping (post-itself) creation_times for every Story in a
        GraphQL response body, in unspecified order.

        Routes through the parser's `_extract_times` (same source of truth
        as the flattener), which reads the wrapping Story's metadata
        strategy at `comet_sections.context_layout.story.comet_sections.
        metadata[*].story.creation_time` where `__typename ==
        CometFeedStoryLongerTimestampStrategy`. That path never descends
        into `attached_story`, so a recent share of an older post yields
        only the share's own wrapping date — fixing the false-positive
        cursor-reset trigger and the early-stop in
        `oldest_in_batch < start_unix`.

        Handles both response shapes: A (`User.timeline_list_feed_units.
        edges[].node`, multiple stories) and B (`node` is a Story directly).
        """
        if not text:
            return
        parser = FacebookGraphQLParser()
        try:
            body = text.encode("utf-8") if isinstance(text, str) else text
            parsed = parser.parse_timeline_response(body, GRAPHQL_API_URL)
        except Exception:
            return
        if not parsed:
            return
        for post in parsed.get("posts") or []:
            node = (post or {}).get("node") or {}
            stories: list[dict] = []
            tlfu = node.get("timeline_list_feed_units") if isinstance(node, dict) else None
            if isinstance(tlfu, dict):
                for edge in (tlfu.get("edges") or []):
                    inner = edge.get("node") if isinstance(edge, dict) else None
                    if isinstance(inner, dict):
                        stories.append(inner)
            elif isinstance(node, dict) and "post_id" in node:
                stories.append(node)
            for story in stories:
                ct = (parser._extract_times(story) or {}).get("created_at")
                if isinstance(ct, (int, float)):
                    yield int(ct)

    @classmethod
    def _hybrid_extract_oldest_creation_time(cls, text: str) -> int | None:
        """Smallest WRAPPING (post-itself) creation_time in the response.
        Skips nested `attached_story` timestamps (shares of older posts)
        and any other inner `creation_time` fields. Returns None if no
        wrapping creation_time was present."""
        times = list(cls._hybrid_iter_wrapping_creation_times(text))
        return min(times) if times else None

    @classmethod
    def _hybrid_extract_newest_creation_time(cls, text: str) -> int | None:
        """Mirror — used by debug log to show batch span so a cursor reset
        (oldest jumps newer) is visible per-iteration."""
        times = list(cls._hybrid_iter_wrapping_creation_times(text))
        return max(times) if times else None

    @staticmethod
    def _hybrid_cursor_fp(cursor: str | None) -> str:
        """Compact fingerprint for a cursor string suitable for log lines.

        Cursors are long opaque base64-ish blobs; dumping them in full would
        flood logs. We need just enough to (a) tell `null` from a real cursor
        and (b) match identical cursors across iterations to detect
        cursor-reset symptoms. Format: `null` or `len=<N> sha=<8hex>`.
        """
        if not cursor:
            return "null"
        h = hashlib.sha1(cursor.encode("utf-8")).hexdigest()[:8]
        return f"len={len(cursor)} sha={h}"

    @staticmethod
    def _hybrid_dump_cursor_reset_window(
        label: str,
        trigger_index: int,
        prev_oldest_unix: int | None,
        cur_oldest_unix: int | None,
        window: deque,
        dump_root: str = HYBRID_CURSOR_RESET_DUMP_ROOT,
    ) -> str | None:
        """Persist the rolling iteration window to disk when a cursor-reset
        symptom fires. Returns the dump directory path on success, None on
        failure — failure is logged but never raised, since this is purely
        diagnostic.

        Layout:
            <dump_root>/<label>/<UTC_ts>/window.jsonl   one record per iter
            <dump_root>/<label>/<UTC_ts>/summary.json   trigger metadata
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Strip leading "@" from a handle so the path is clean; safe no-op
        # if `label` doesn't start with one (e.g. a search query_text).
        safe_label = label.lstrip("@") or "unknown"
        out_dir = os.path.join(dump_root, safe_label, ts)
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "window.jsonl"), "w") as f:
                for rec in window:
                    f.write(json.dumps(rec) + "\n")
            summary = {
                "label": label,
                "trigger_pagination": trigger_index,
                "prev_oldest_unix": prev_oldest_unix,
                "cur_oldest_unix": cur_oldest_unix,
                "prev_oldest_iso": (
                    datetime.fromtimestamp(prev_oldest_unix, tz=timezone.utc).isoformat()
                    if prev_oldest_unix is not None else None
                ),
                "cur_oldest_iso": (
                    datetime.fromtimestamp(cur_oldest_unix, tz=timezone.utc).isoformat()
                    if cur_oldest_unix is not None else None
                ),
                "jump_seconds": (
                    cur_oldest_unix - prev_oldest_unix
                    if (prev_oldest_unix is not None and cur_oldest_unix is not None)
                    else None
                ),
                "window_size": len(window),
                "dumped_at": ts,
            }
            with open(os.path.join(out_dir, "summary.json"), "w") as f:
                json.dump(summary, f, indent=2)
            return out_dir
        except Exception as e:
            logger.warning(f"[hybrid] @{label}: cursor-reset dump failed: {e}")
            return None

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
                raise RendererHangError(
                    f"page.request.post timed out after {operation_timeout_seconds}s"
                )
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
                # Burst is best-effort fingerprint cosmetics; a hang here does
                # NOT warrant a RendererHangError + scrape restart. Just abort
                # the burst and let the pagination loop continue.
                logger.warning(
                    f"[hybrid] burst scroll {j+1}/{n_scrolls} timed out — "
                    f"aborting burst"
                )
                break
            except Exception as e:
                logger.warning(f"[hybrid] burst scroll {j+1} failed: {e}")
                break
            await asyncio.sleep(abs(random.gauss(2.5, 0.5)))
