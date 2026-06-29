"""Browser management and page control for Facebook scraping."""
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
from .stop_conditions import (
    StopCondition,
    StopState,
    assemble_default_stop_conditions,
    HYBRID_CURSOR_RESET_WINDOW,
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


GRAPHQL_API_URL = "https://www.facebook.com/api/graphql/"
# Headers managed by Playwright / BrowserContext; stripped from captured templates.
HYBRID_HEADER_DROP = frozenset({
    "host", "content-length", "connection", "accept-encoding", "cookie",
})

HYBRID_TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"
SEARCH_HYBRID_TARGET_FRIENDLY_NAME = "SearchCometResultsPaginatedResultsQuery"
GROUP_TIMELINE_HYBRID_TARGET_FRIENDLY_NAME = "GroupsCometFeedRegularStoriesPaginationQuery"
COMMENTS_LIST_HYBRID_TARGET_FRIENDLY_NAME = "CommentsListComponentsPaginationQuery"
PAGE_TRANSPARENCY_FRIENDLY_NAME = "ProfileTransparencyDialogQuery"
PROFILE_AUTHENTICITY_FRIENDLY_NAME = "ProfileCometDirectoryAuthenticityModalQuery"

# Empirically-validated enum values for the GroupsCometFeed `sortingSetting`
# variable. Default lives in Query.ENDPOINT_REGISTRY; this tuple just
# documents what's known to be accepted. Kept as a module-level constant
# so the CLI help text stays in sync. Other values may also be accepted
# silently by FB — the param is passed through as-is, no validation.
GROUP_TIMELINE_SORTING_SETTINGS = ("CHRONOLOGICAL", "RECENT_ACTIVITY", "TOP_POSTS")

# Registry of known Search URL filters. Maps user-facing name → FB outer-key
# prefix, inner "name" field, and an arg encoder. Unknown keys passed to
# _build_search_url are treated as raw passthrough entries.
_SEARCH_FILTER_REGISTRY: dict[str, dict] = {
    # Sort
    "recent_posts": {
        "fb_key": "recent_posts",
        "name":   "recent_posts",
        "encode": lambda **_: "",
    },
    # Date range
    "creation_time": {
        "fb_key": "rp_creation_time",
        "name":   "creation_time",
        "encode": lambda start=None, end=None: _encode_creation_time_args(start, end),
    },
    # Author / source — "Posts from" filter. Pass source= (mutually exclusive),
    # see _POSTS_FROM_SOURCES for the accepted values.
    "posts_from": {
        "fb_key": "rp_author",
        "name": lambda source="public": _posts_from_name(source),
        "encode": lambda **_: "",
    },
}


# "Posts from" filter: user-facing source → FB inner `name`.
_POSTS_FROM_SOURCES = {
    "public":           "merged_public_posts",
    "me":               "author_me",
    "friends":          "author_friends_feed",
    "groups_and_pages": "my_groups_and_pages_posts",
}


def _posts_from_name(source: str = "public") -> str:
    try:
        return _POSTS_FROM_SOURCES[source]
    except KeyError:
        raise ValueError(
            f"posts_from source {source!r} is not valid; "
            f"choose one of {sorted(_POSTS_FROM_SOURCES)}"
        )


def _encode_creation_time_args(start: str | None = None, end: str | None = None) -> str:
    """Encode start/end YYYY-MM-DD strings into the FB creation_time args blob.

    Date components are not zero-padded (FB UI format: "2025-1-1").
    One-sided bounds are supported — pass only start or only end.
    """
    args: dict[str, str] = {}
    if start:
        dt = datetime.strptime(start, "%Y-%m-%d")
        args.update({
            "start_year":  str(dt.year),
            "start_month": f"{dt.year}-{dt.month}",
            "start_day":   f"{dt.year}-{dt.month}-{dt.day}",
        })
    if end:
        dt = datetime.strptime(end, "%Y-%m-%d")
        args.update({
            "end_year":  str(dt.year),
            "end_month": f"{dt.year}-{dt.month}",
            "end_day":   f"{dt.year}-{dt.month}-{dt.day}",
        })
    return json.dumps(args, separators=(",", ":"))


# Bump when FB ships a schema update to the persisted query.
PAGE_TRANSPARENCY_DOC_ID = "35170702705850131"
PROFILE_AUTHENTICITY_DOC_ID = "26932128459750707"

class BrowserSession:
    """Manages browser session and page navigation."""

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
        # When False, initialize() skips cookie injection + form login so the
        # caller can drive auth itself.
        self.auto_login = auto_login

        self.endpoint: str = ""

        # Per-session scroll counter. Worker reads this after each task and
        # adds it to its own per-account-ownership total to decide rotation
        # (the DB scroll columns are cumulative-lifetime and aren't suitable
        # as a rotation signal — see Worker.execute_task).
        self.scrolls_recorded: int = 0

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[ResponseInterceptor] = None

    async def __aenter__(self):
        logger.debug(f"BrowserSession.__aenter__() for {self.account.display_name}")
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug(f"BrowserSession.__aexit__() for {self.account.display_name}, exc_type={exc_type}")
        await self.close()
        return False

    async def initialize(self):
        """Open the browser, inject cookies (if any), and log in."""
        logger.debug(f"BrowserSession.initialize() starting for {self.account.display_name}")
        self._pw = await async_playwright().start()

        try:
            proxy_settings = self._get_proxy_dict()
            logger.debug(f"Proxy settings: {'configured' if proxy_settings else 'none'}")

            fingerprint = await self._resolve_fingerprint()
            device_os = get_device_os()
            device_headless = self._resolve_headless(os=device_os, headless=self.headless)
            logger.debug(
                f"Fingerprint: {fingerprint}, "
                f"OS: {device_os}, "
                f"Headless: {device_headless}"
            )

            self._browser: Browser = await AsyncNewBrowser(
                playwright=self._pw,
                humanize=True,
                headless=device_headless,
                proxy=proxy_settings,
                geoip=True if proxy_settings else False,
                os=device_os,
                fingerprint=fingerprint,
                i_know_what_im_doing=True,
                firefox_user_prefs={
                    "browser.aboutwelcome.enabled": False,
                    "browser.startup.firstrunSkipsHomepage": True,
                    "browser.shell.checkDefaultBrowser": False,
                    "datareporting.policy.dataSubmissionEnabled": False,
                    "browser.cache.disk.enable": False,
                    "browser.cache.memory.capacity": 0,
                    "browser.sessionhistory.max_entries": 2,
                    "browser.sessionhistory.max_total_viewers": 0,
                    "dom.ipc.processCount.webIsolated": 1,
                }
            )

            self._context: BrowserContext = await self._browser.new_context()
            self.page = await self._context.new_page()

            # Workaround for camoufox issue #473: br/zstd decompression broken.
            await self.page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

            self.response_interceptor = ResponseInterceptor()
            self.response_interceptor.setup_interception(self.page)

            if not self.auto_login:
                logger.info(f"Browser session initialized for {self.account.display_name} (auto_login=False)")
                return

            # Production login orchestrator: cookies → automatic → manual
            # (non-headless only). Raises typed exceptions on terminal failure;
            # the worker catches them to drive account rotation.
            await _login.login(self)
            logger.info(f"Browser session initialized for {self.account.display_name}")
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
        """Close browser session and clean up resources."""
        logger.debug(f"BrowserSession.close() for {self.account.display_name}")
        if self.response_interceptor:
            # When FB_NETWORK_CAPTURE_DIR is set, dump captured XHR to JSONL before teardown.
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
        """Return cookies from the current browser context."""
        storage_state = await self._context.storage_state()
        return storage_state['cookies']

    async def save_cookies(self):
        """Persist current cookies to the accounts database."""
        cookies = await self.get_cookies()
        await self.pool.update_cookies(self.account.identifier, cookies)
        logger.info(f"Saved cookies for {self.account.display_name}")


    # ==================== Scraping ====================

    async def user_timeline_manual(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
        scroll_window_height_coefficient: float = 3.0,
        post_nav_sleep_seconds: float = 5.0,
        inter_scroll_sleep_range: tuple[float, float] = (2.0, 4.5),
        breather_every_n_scrolls: int = 50,
        breather_duration_seconds: float = 30,
        max_no_new_posts_streak: int = 30,
        stall_timeout_seconds: float = 300,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """Scrape a user's timeline by driving scroll and intercepting GraphQL responses.

        Args:
            handle: Facebook username/handle.
            start_date / end_date: YYYY-MM-DD or None (open lower / upper
                bound). When start_date is None, the "oldest post < start"
                stop is disabled — the scrape relies on no_new_posts_streak
                and the GraphQL-silence watchdog for termination.

        Returns:
            ScrapeOutcome.
        """
        self.endpoint = "UserTimeline"
        logger.debug(f"user_timeline_manual() starting for @{handle}, date range: {start_date} to {end_date}")

        base_url = "https://www.facebook.com/"
        target_url = f"{base_url}{handle}/"

        scrape_start_time = datetime.now(timezone.utc)

        start_datetime = (
            datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        )
        end_datetime = (
            datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
        )

        total_scrolls = 0
        no_new_posts_count = 0
        previous_post_count = 0

        logger.info(f"Scraping @{handle}'s homepage from {start_date} to {end_date}")

        self.response_interceptor.flush()

        while True:
            try:
                iter_start = datetime.now(timezone.utc)
                logger.debug(f"@{handle} loop iter {total_scrolls}: start")

                if not self.is_on_page(target_url):
                    logger.info(f"Navigating to {target_url}")
                    await self.goto(target_url)
                    await asyncio.sleep(post_nav_sleep_seconds)

                    await self.page.keyboard.press('Escape')

                    if not self.is_on_page(target_url):
                        return ScrapeOutcome(
                            result='logged out while scraping',
                            data=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

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

                if current_post_count == previous_post_count:
                    no_new_posts_count += 1
                    logger.debug(f"@{handle} iter {total_scrolls}: no new posts (streak={no_new_posts_count})")

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

                if current_post_count > 0 and start_datetime is not None:
                    oldest_timestamp = self._find_oldest_post_timestamp(posts)

                    if oldest_timestamp:
                        logger.debug(f"Oldest post: {oldest_timestamp}, target: {start_datetime}")

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

                # Bail if Facebook has stopped responding to GraphQL.
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

    async def user_timeline_hybrid(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
        pagination_count: int = 3,
        scroll_burst_every: int = 10,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 45.0,
        max_paginations: int = 10000,
        max_posts: int = -1,
        initial_cursor: str = "",
        seen_post_ids_to_skip: list | None = None,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """Scrape a user's timeline by replaying ProfileCometTimelineFeedRefetchQuery via page.request.post().

        Args:
            handle: Facebook username/handle.
            start_date / end_date: YYYY-MM-DD or None. When end_date is None,
                no `beforeTime` is injected into replay bodies (the CLI layer
                normally auto-fills today to mirror FB's UI; passing None
                directly is an opt-out). When start_date is None, the
                date-bounded stops (`OldestInBatchBelowStartDate`,
                `ConsecutiveOutOfRange`) no-op via their existing guards.

        Returns:
            ScrapeOutcome.
        """
        self.endpoint = "UserTimeline"
        logger.info(
            f"[hybrid] @{handle}: starting hybrid scrape "
            f"({start_date} → {end_date}, count={pagination_count})"
        )

        target_url = f"https://www.facebook.com/{handle}/"
        scrape_start_time = datetime.now(timezone.utc)

        # afterTime: start-of-day UTC, inclusive. None when start_date omitted.
        start_unix: int | None = None
        if start_date:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start_unix = int(start_datetime.timestamp())

        # beforeTime: end-of-day UTC capped at "now". None when end_date omitted
        # (loop will skip `beforeTime` injection — see `inject_before_time`).
        end_unix: int | None = None
        if end_date:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_of_day = end_datetime + timedelta(days=1) - timedelta(seconds=1)
            now_utc = datetime.now(timezone.utc)
            end_unix = int(min(end_of_day, now_utc).timestamp())

        loop_params = {
            "pagination_count": pagination_count,
            "scroll_burst_every": scroll_burst_every,
            "scroll_burst_size_range": scroll_burst_size_range,
            "pagination_sleep_mean": pagination_sleep_mean,
            "pagination_sleep_std": pagination_sleep_std,
            "max_paginations": max_paginations,
            "max_posts": max_posts,
            "max_no_progress_streak": max_no_progress_streak,
            "request_timeout_ms": request_timeout_ms,
            "operation_timeout_seconds": operation_timeout_seconds,
        }

        self.response_interceptor.flush()
        # All posts come from explicit replays; ignore natural PCTFRQ bodies which carry no date filters.
        self.response_interceptor.extract_posts = False

        # Resume seed (mirrors group_timeline_hybrid). Must run AFTER flush()
        # (which clears seen_post_ids) and BEFORE the loop.
        if seen_post_ids_to_skip:
            self.response_interceptor.seen_post_ids.update(seen_post_ids_to_skip)
            logger.info(
                f"[hybrid] @{handle}: seeded {len(seen_post_ids_to_skip)} "
                f"post_ids into dedup set for resume"
            )
        if initial_cursor:
            logger.info(
                f"[hybrid] @{handle}: resuming from cursor "
                f"{self._hybrid_cursor_fp(initial_cursor)}"
            )

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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )

        # Phase 2 — bootstrap scroll
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )
        logger.info(
            f"[hybrid] @{handle}: template captured "
            f"(doc_id={template['doc_id']}, profile_id={template['profile_id']})"
        )

        # Phase 4 — pagination loop
        result_str, next_cursor = await self._hybrid_pagination_loop(
            label=handle,
            template=template,
            params=loop_params,
            start_unix=start_unix,
            end_unix=end_unix,
            initial_cursor=initial_cursor or None,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time,
            last_cursor=next_cursor,
        )

    async def search_hybrid(
        self,
        query_text: str,
        filters: dict | None = None,
        pagination_count: int = 5,
        scroll_burst_every: int = 50,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 45.0,
        max_paginations: int = -1,
        max_posts: int = -1,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """Scrape Facebook search results for `query_text`.

        Search filters (sort order, date range, etc.) are applied via the
        URL &filters= blob (see `_build_search_url` and `_SEARCH_FILTER_REGISTRY`).
        Replay bodies carry no `beforeTime`/`afterTime`. When a "creation_time"
        filter is present, `start_unix`/`end_unix` are extracted from it so
        stop conditions have their anchors even though they don't flow into
        the request body.

        The first cursor is extracted from the captured SCRQ template form
        (SCRQ uses `{"page_number":0,...}` on its first request, not null).

        Args:
            query_text: Free-form search term.
            filters: Optional dict of search filters. Known keys: "recent_posts",
                "creation_time" (with "start"/"end" YYYY-MM-DD sub-keys).
                Unknown keys are passed as raw blob entries. None = no filters.

        Returns:
            ScrapeOutcome.
        """
        self.endpoint = "Search"
        logger.info(
            f"[hybrid] search {query_text!r}: starting hybrid scrape "
            f"(filters={filters!r}, count={pagination_count})"
        )

        target_url = self._build_search_url(query_text, filters)
        scrape_start_time = datetime.now(timezone.utc)

        # Extract date anchors from creation_time filter for stop conditions.
        # They don't flow into the replay body (inject_before_time=False).
        creation_time_kwargs = (filters or {}).get("creation_time") or {}
        start_date = creation_time_kwargs.get("start") if isinstance(creation_time_kwargs, dict) else None
        end_date = creation_time_kwargs.get("end") if isinstance(creation_time_kwargs, dict) else None
        start_unix: int | None = None
        if start_date:
            start_unix = int(
                datetime.strptime(start_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc).timestamp()
            )
        end_unix: int | None = None
        if end_date:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_of_day = end_datetime + timedelta(days=1) - timedelta(seconds=1)
            end_unix = int(min(end_of_day, datetime.now(timezone.utc)).timestamp())

        loop_params = {
            "pagination_count": pagination_count,
            "scroll_burst_every": scroll_burst_every,
            "scroll_burst_size_range": scroll_burst_size_range,
            "pagination_sleep_mean": pagination_sleep_mean,
            "pagination_sleep_std": pagination_sleep_std,
            "max_paginations": max_paginations,
            "max_posts": max_posts,
            "max_no_progress_streak": max_no_progress_streak,
            "request_timeout_ms": request_timeout_ms,
            "operation_timeout_seconds": operation_timeout_seconds,
        }

        self.response_interceptor.flush()
        self.response_interceptor.extract_posts = False

        # Phase 1 — navigate to filtered search URL
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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        # Phase 2 — bootstrap scroll (SCRQ does not fire on raw navigation)
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )

        # Phase 3 — capture SCRQ template
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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
            )
        logger.info(
            f"[hybrid] search {query_text!r}: template captured "
            f"(doc_id={template['doc_id']})"
        )

        # SCRQ's first request uses cursor="{page_number:0,...}", NOT null.
        # _hybrid_capture_template always sets template["cursor"]=None, so we
        # extract the real initial cursor from the form variables here.
        try:
            initial_vars = json.loads(template["form"].get("variables", "{}"))
            initial_cursor = initial_vars.get("cursor") or ""
        except json.JSONDecodeError:
            initial_cursor = ""

        # Phase 4 — pagination loop.
        # inject_before_time=False: date bounds live in the URL, not the body.
        # parse_response: SCRQ responses use serpResponse.results.edges[], not data.node.
        result_str, next_cursor = await self._hybrid_pagination_loop(
            label=query_text,
            template=template,
            params=loop_params,
            start_unix=start_unix,
            end_unix=end_unix,
            inject_before_time=False,
            initial_cursor=initial_cursor or None,
            parse_response=self.response_interceptor.parser.parse_search_response,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time,
            last_cursor=next_cursor,
        )

    async def group_timeline_hybrid(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
        pagination_count: int = 3,
        sorting_setting: str = "TOP_POSTS",
        scroll_burst_every: int = 10,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 45.0,
        max_paginations: int = -1,
        max_posts: int = -1,
        initial_cursor: str = "",
        seen_post_ids_to_skip: list | None = None,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        max_consecutive_out_of_range: int = 20,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """Scrape a group's feed by replaying GroupsCometFeedRegularStoriesPaginationQuery.

        Differences from `user_timeline_hybrid`:
          - Navigates to `/groups/<handle>/` (accepts vanity or numeric id;
            FB redirects either to the canonical group URL).
          - The GraphQL variables carry no `beforeTime` / `afterTime` filter —
            FB has no server-side date filter for group feeds. Termination
            is purely client-side: oldest in-batch creation_time vs.
            `start_unix`. `end_date` is therefore advisory (it only bounds
            the loop's stop check; FB always returns from the current head
            of feed when cursor is null).
          - `sortingSetting` is overridden on every replay (default
            `TOP_POSTS`, matching FB's UI). Empirically, `CHRONOLOGICAL`
            scrapes correlate with account suspensions on this endpoint;
            TOP_POSTS is the lowest-fingerprint choice. Termination under
            TOP_POSTS relies on `ConsecutiveOutOfRange` (default 20) since
            posts arrive non-monotonically — see `assemble_default_stop_conditions`.
          - The captured `variables.id` (numeric group id) is inherited from
            the natural request, so callers may pass either the vanity
            handle or the numeric id as `handle`.

        Args:
            handle: Vanity group handle (e.g. "albertaseparatism") or the
                numeric group id. Used for the `/groups/<handle>/` URL.
            start_date / end_date: YYYY-MM-DD (inputs are not re-validated here).
            sorting_setting: Value to send as `variables.sortingSetting`.
                Known-valid (default `"TOP_POSTS"`):
                  - `"TOP_POSTS"` — FB UI default; algorithmic ranking.
                    Termination via `ConsecutiveOutOfRange`. Lowest-fingerprint
                    and the empirically safer choice for sustained scraping.
                  - `"CHRONOLOGICAL"` — stream-line tail is descending by
                    post creation_time. Closest to true creation-time
                    ordering but empirically associated with bans on this
                    endpoint — opt-in only. Termination via both
                    `OldestInBatchBelowStartDate` and `ConsecutiveOutOfRange`.
                  - `"RECENT_ACTIVITY"` — sorts by most recent comment /
                    reaction; treated as non-chronological.
                Other values may be accepted by FB silently.
            max_consecutive_out_of_range: N posts in a row outside
                `[start_unix, end_unix]` → bail with `consecutive_out_of_range`.
                Primary date-tail stop under non-chronological sorts; -1
                disables.
            initial_cursor: When non-empty, the loop starts from this
                cursor instead of `null` (iter 1 is skipped) — used by the
                `--continue` resume path. Per FB's own docs, cursors are
                ephemeral; if FB rejects it the existing stop conditions
                handle the fallout (cursor_reset, no_new_posts_streak,
                etc.).
            seen_post_ids_to_skip: Optional iterable of `post_id` strings
                to seed `ResponseInterceptor.seen_post_ids` with before the
                loop starts. Prevents previously-collected posts from
                being re-added when a resume run re-encounters them in
                the bootstrap edge.

        Returns:
            ScrapeOutcome.
        """
        self.endpoint = "GroupTimeline"
        logger.info(
            f"[hybrid] group @{handle}: starting hybrid scrape "
            f"({start_date} → {end_date}, count={pagination_count}, "
            f"sort={sorting_setting})"
        )

        target_url = f"https://www.facebook.com/groups/{handle}/"
        scrape_start_time = datetime.now(timezone.utc)

        # Date bounds are purely client-side here (GCFRSPQ has no server-side
        # date filter). Either / both may be None — stop conditions are
        # None-safe via their existing guards, and the loop will not inject
        # `beforeTime` either way (`inject_before_time=False` below).
        start_unix: int | None = None
        if start_date:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            start_unix = int(start_datetime.timestamp())
        end_unix: int | None = None
        if end_date:
            end_datetime = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_of_day = end_datetime + timedelta(days=1) - timedelta(seconds=1)
            now_utc = datetime.now(timezone.utc)
            end_unix = int(min(end_of_day, now_utc).timestamp())

        loop_params = {
            "pagination_count": pagination_count,
            "scroll_burst_every": scroll_burst_every,
            "scroll_burst_size_range": scroll_burst_size_range,
            "pagination_sleep_mean": pagination_sleep_mean,
            "pagination_sleep_std": pagination_sleep_std,
            "max_paginations": max_paginations,
            "max_posts": max_posts,
            "max_no_progress_streak": max_no_progress_streak,
            "max_consecutive_out_of_range": max_consecutive_out_of_range,
            "request_timeout_ms": request_timeout_ms,
            "operation_timeout_seconds": operation_timeout_seconds,
        }

        self.response_interceptor.flush()
        # All posts come from explicit replays. Natural bootstrap GCFRSPQ
        # carries the user's UI sort (likely TOP_POSTS), so letting it
        # auto-populate self.posts would mix algorithmic ordering into the
        # output. Mirrors UserTimeline / Search hybrid.
        self.response_interceptor.extract_posts = False

        # Resume seed: prime the interceptor's dedup set with IDs from a
        # prior run so bootstrap-edge highlights we already collected don't
        # come back as new posts. Must happen AFTER flush() (which clears
        # seen_post_ids) and BEFORE the loop runs.
        if seen_post_ids_to_skip:
            self.response_interceptor.seen_post_ids.update(seen_post_ids_to_skip)
            logger.info(
                f"[hybrid] group @{handle}: seeded {len(seen_post_ids_to_skip)} "
                f"post_ids into dedup set for resume"
            )
        if initial_cursor:
            logger.info(
                f"[hybrid] group @{handle}: resuming from cursor "
                f"{self._hybrid_cursor_fp(initial_cursor)}"
            )

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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )

        # Phase 2 — bootstrap scroll (GCFRSPQ doesn't fire on raw navigation,
        # same as PCTFRQ — see feedback memory).
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )

        # Phase 3 — capture pagination template (GCFRSPQ instead of PCTFRQ/SCRQ)
        error, template = await self._hybrid_capture_template(
            template_capture_timeout=template_capture_timeout,
            operation_timeout_seconds=operation_timeout_seconds,
            friendly_name=GROUP_TIMELINE_HYBRID_TARGET_FRIENDLY_NAME,
            interceptor_attr="latest_gcfrspq_request",
        )
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )
        logger.info(
            f"[hybrid] group @{handle}: template captured "
            f"(doc_id={template['doc_id']}, group_id={template['profile_id']})"
        )

        # Phase 4 — pagination loop. `end_unix` flows into the stop framework
        # (ConsecutiveOutOfRange upper bound) but is NOT injected into the
        # request body — GroupsCometFeedRegularStoriesPaginationQuery has no
        # server-side date filter (`inject_before_time=False`). Static
        # override forces a non-default sort on every replay.
        result_str, next_cursor = await self._hybrid_pagination_loop(
            label=handle,
            template=template,
            params=loop_params,
            start_unix=start_unix,
            end_unix=end_unix,
            inject_before_time=False,
            static_variable_overrides={
                "sortingSetting": sorting_setting,
            },
            initial_cursor=initial_cursor or None,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time,
            last_cursor=next_cursor,
        )

    async def comments_list_hybrid(
        self,
        handle: str,
        post_id: str,
        comments_after_count: int = -1,
        feed_location: str = "POST_PERMALINK_DIALOG",
        scroll_burst_every: int = 50,
        scroll_burst_size_range: tuple[int, int] = (2, 5),
        pagination_sleep_mean: float = 2.5,
        pagination_sleep_std: float = 0.5,
        template_capture_timeout: float = 45.0,
        max_paginations: int = -1,
        max_results: int = -1,
        initial_cursor: str = "",
        seen_comment_ids_to_skip: list | None = None,
        post_nav_sleep_seconds: float = 3.0,
        request_timeout_ms: int = 30000,
        max_no_progress_streak: int = 5,
        operation_timeout_seconds: float = 900,
    ) -> ScrapeOutcome:
        """Scrape top-level comments on a post by replaying
        CommentsListComponentsPaginationQuery.

        Differences from `user_timeline_hybrid` / `group_timeline_hybrid`:
          - Navigates to `/<handle>/posts/<post_id>/` (post_id accepts the
            numeric form OR the pfbid form — both work in FB's permalink URL).
          - GraphQL response is single-chunk JSON (not JSONL / @defer), so the
            pagination loop uses a comment-specific parser
            (`parse_comments_response`) instead of `parse_timeline_response`.
          - Pagination variable is `commentsAfterCursor` (not `cursor`); page
            size variable is `commentsAfterCount`; FB has no server-side date
            filter and comments are non-chronological, so the loop runs no
            date-bound stop conditions.
          - The captured `variables.id` (base64 `feedback:<numeric_post_id>`)
            is inherited from the natural request; callers don't supply it.

        Args:
            handle: Vanity handle (or numeric id) of the post's author / page;
                used in the navigation URL.
            post_id: Numeric post id OR pfbid form — passed verbatim into the
                URL path.
            comments_after_count: `variables.commentsAfterCount`. `-1` mirrors
                FB's UI (server picks ~10 per page). Positive int caps the
                per-page count.
            feed_location: `variables.feedLocation`. Default
                `"POST_PERMALINK_DIALOG"` matches the surface our nav URL hits.
            max_results: -1 disables; cap on total accumulated comments.
                Batch-boundary, may overshoot by up to ~one page.
            initial_cursor: When non-empty, the loop starts from this
                cursor instead of `null` — used by the `--continue` resume
                path. Cursors are ephemeral per FB's own docs.
            seen_comment_ids_to_skip: Optional iterable of comment_id strings
                to seed `ResponseInterceptor.seen_post_ids` with (the same
                set is reused for comment dedup across resume runs).

        Returns:
            ScrapeOutcome with `data` = list of Comment-shaped records (one
            entry per top-level comment).
        """
        self.endpoint = "CommentsList"
        logger.info(
            f"[hybrid] post {handle}/{post_id}: starting comments scrape "
            f"(feed_location={feed_location})"
        )

        target_url = f"https://www.facebook.com/{handle}/posts/{post_id}/"
        scrape_start_time = datetime.now(timezone.utc)

        loop_params = {
            "comments_after_count": comments_after_count,
            "feed_location": feed_location,
            "scroll_burst_every": scroll_burst_every,
            "scroll_burst_size_range": scroll_burst_size_range,
            "pagination_sleep_mean": pagination_sleep_mean,
            "pagination_sleep_std": pagination_sleep_std,
            "max_paginations": max_paginations,
            # The MaxPostsReached stop condition counts `all_posts_count`;
            # we re-use it as the comment-count cap by aliasing the param.
            "max_posts": max_results,
            "max_no_progress_streak": max_no_progress_streak,
            "request_timeout_ms": request_timeout_ms,
            "operation_timeout_seconds": operation_timeout_seconds,
        }

        self.response_interceptor.flush()
        # Comments aren't returned by `parse_timeline_response` (the
        # interceptor's auto-extract hook), so leaving `extract_posts` on
        # would have no effect either way — we collect manually inside the
        # loop. Turn it off for clarity / symmetry with other hybrid paths.
        self.response_interceptor.extract_posts = False

        # Resume seed: comments dedup uses the same `seen_post_ids` set
        # (it's name-only — the set is really "seen record ids" for whatever
        # endpoint the session is currently scraping).
        if seen_comment_ids_to_skip:
            self.response_interceptor.seen_post_ids.update(seen_comment_ids_to_skip)
            logger.info(
                f"[hybrid] post {handle}/{post_id}: seeded "
                f"{len(seen_comment_ids_to_skip)} comment_ids into dedup set "
                f"for resume"
            )
        if initial_cursor:
            logger.info(
                f"[hybrid] post {handle}/{post_id}: resuming from cursor "
                f"{self._hybrid_cursor_fp(initial_cursor)}"
            )

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
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )

        # Phase 2 — bootstrap scroll. CommentsListComponentsPaginationQuery
        # is the "load more comments" query; an initial page-level scroll
        # gets the comments panel rendered + the first paginated request
        # firing. Same mechanism as PCTFRQ / GCFRSPQ.
        error = await self._hybrid_bootstrap(operation_timeout_seconds)
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )

        # Phase 3 — capture template
        error, template = await self._hybrid_capture_template(
            template_capture_timeout=template_capture_timeout,
            operation_timeout_seconds=operation_timeout_seconds,
            friendly_name=COMMENTS_LIST_HYBRID_TARGET_FRIENDLY_NAME,
            interceptor_attr="latest_clcpq_request",
        )
        if error:
            return ScrapeOutcome(
                result=error,
                data=self.response_interceptor.get_posts(),
                time_started=scrape_start_time,
                time_taken=datetime.now(timezone.utc) - scrape_start_time,
                last_cursor=initial_cursor or None,
            )
        logger.info(
            f"[hybrid] post {handle}/{post_id}: template captured "
            f"(doc_id={template['doc_id']}, feedback_id={template['profile_id']})"
        )

        # Phase 4 — comment pagination loop. No date filter, no JSONL
        # parsing, no Story shape. Comment parser handles dedup +
        # accumulation directly.
        result_str, next_cursor = await self._hybrid_comments_pagination_loop(
            label=f"{handle}/{post_id}",
            template=template,
            params=loop_params,
            initial_cursor=initial_cursor or None,
        )
        return ScrapeOutcome(
            result=result_str,
            data=self.response_interceptor.get_posts(),
            time_started=scrape_start_time,
            time_taken=datetime.now(timezone.utc) - scrape_start_time,
            last_cursor=next_cursor,
        )

    async def _hybrid_comments_pagination_loop(
        self,
        label: str,
        template: dict,
        params: dict,
        initial_cursor: str | None = None,
        stop_conditions: list[StopCondition] | None = None,
    ) -> tuple[str, str | None]:
        """Drive comment paginations via page.request.post() until a stop fires.

        Mirrors the structure of `_hybrid_pagination_loop` but tailored for
        CommentsListComponentsPaginationQuery:
          - Replays use `commentsAfterCursor` / `commentsAfterCount` (not
            `cursor` / `count`).
          - The response is single-chunk JSON; `end_cursor` lives at
            `data.node.comment_rendering_instance_for_feed_location.comments.page_info.end_cursor`.
          - Comments are parsed via `parse_comments_response` and appended
            via the interceptor's `add_posts` (which dedups by `id`).
          - No date semantics; date-bound stop conditions are not assembled.

        Returns `(result_string, next_cursor)`.
        """
        template_form = template["form"]
        template_headers = template["headers"]
        cursor = initial_cursor or template["cursor"]
        next_cursor: str | None = None

        comments_after_count = params["comments_after_count"]
        feed_location = params["feed_location"]
        scroll_burst_every = params["scroll_burst_every"]
        scroll_burst_size_range = params["scroll_burst_size_range"]
        pagination_sleep_mean = params["pagination_sleep_mean"]
        pagination_sleep_std = params["pagination_sleep_std"]
        request_timeout_ms = params["request_timeout_ms"]
        operation_timeout_seconds = params["operation_timeout_seconds"]

        if stop_conditions is None:
            stop_conditions = []
        default_stop_conditions = assemble_default_stop_conditions(
            endpoint=self.endpoint,
            mode="hybrid",
            sorting_setting=None,
            params=params,
        )
        for d in default_stop_conditions:
            stop_conditions.append(d)

        total_paginations = 0
        no_progress_streak = 0
        previous_post_count = len(self.response_interceptor.get_posts())
        iter_window: deque = deque(maxlen=HYBRID_CURSOR_RESET_WINDOW)

        while True:
            overrides = {
                "commentsAfterCursor": cursor,
                "commentsAfterCount": comments_after_count,
                "feedLocation": feed_location,
            }
            body = self._hybrid_build_body(template_form, overrides)

            cursor_sent_fp = self._hybrid_cursor_fp(cursor)
            csr_len = len(self.response_interceptor.latest_csr or "")
            dyn_len = len(self.response_interceptor.latest_dyn or "")
            cursor_sent_this_iter = cursor

            iter_start = datetime.now(timezone.utc)
            response, text, error_str = await self._hybrid_send_replay(
                handle=label,
                body=body,
                template_headers=template_headers,
                request_timeout_ms=request_timeout_ms,
                operation_timeout_seconds=operation_timeout_seconds,
            )
            if error_str is not None:
                return error_str, next_cursor

            await self.record_scroll(endpoint=self.endpoint, count=1)
            total_paginations += 1

            try:
                parsed = self.response_interceptor.parser.parse_comments_response(
                    text.encode("utf-8"), GRAPHQL_API_URL
                )
            except Exception as e:
                logger.warning(f"[hybrid] @{label}: comments parser raised: {e}")
                parsed = None
            comments = (parsed or {}).get("comments") or []
            if comments:
                self.response_interceptor.add_posts(comments)
            end_cursor = (parsed or {}).get("end_cursor")
            has_next_page = bool((parsed or {}).get("has_next_page"))

            # Auth-ish errors → raise so Worker rotates the account.
            # In-body rate-limits → return 'rate_limit'.
            graphql_error_detail = self._hybrid_extract_graphql_error_detail(text)
            if graphql_error_detail:
                gql_msg = graphql_error_detail.get("message", "")
                gql_code = graphql_error_detail.get("code")
                logger.warning(
                    f"[hybrid] @{label}: GraphQL error: {gql_msg} "
                    f"(code={gql_code}, severity={graphql_error_detail.get('severity')})"
                )
                if self._hybrid_is_auth_error(gql_msg):
                    raise FailedLoginError(
                        f"Session invalid mid-scrape (graphql error: {gql_msg})"
                    )
                if self._hybrid_is_rate_limit_error(graphql_error_detail):
                    return 'rate_limit', next_cursor

            current_post_count = len(self.response_interceptor.get_posts())
            new_posts_in_iter = current_post_count - previous_post_count
            no_progress_streak = 0 if new_posts_in_iter else no_progress_streak + 1
            previous_post_count = current_post_count

            if end_cursor and has_next_page:
                # When FB reports `has_next_page: false`, the cursor it
                # echoes back is stale — drop it so the EndOfFeed stop
                # condition fires cleanly. Mirrors how `_hybrid_extract_end_cursor`
                # treats a falsy end_cursor.
                next_cursor = end_cursor

            elapsed_iter = (datetime.now(timezone.utc) - iter_start).total_seconds()
            cursor_recv_fp = self._hybrid_cursor_fp(end_cursor)
            posts_in_resp = len(comments)
            logger.debug(
                f"[hybrid] @{label} pagination {total_paginations}: "
                f"{response.status} in {elapsed_iter:.2f}s, "
                f"comments now={current_post_count} (+{new_posts_in_iter}, "
                f"in_resp={posts_in_resp}), "
                f"has_next_page={has_next_page}, "
                f"cursor_sent={cursor_sent_fp}, "
                f"cursor_recv={cursor_recv_fp}, "
                f"__csr_len={csr_len}, __dyn_len={dyn_len}, "
                f"resp_bytes={len(text or '')}"
            )

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
                    "cursor_recv_fp": cursor_recv_fp,
                    "has_next_page": has_next_page,
                    "posts_in_resp": posts_in_resp,
                },
                "session": {
                    "csr_len": csr_len,
                    "dyn_len": dyn_len,
                },
            })

            # Effective end_cursor for stop conditions: treat
            # `has_next_page: false` as end-of-feed regardless of what
            # FB echoes back in `end_cursor`. EndOfFeed.evaluate bails on
            # a falsy end_cursor.
            effective_end_cursor = end_cursor if has_next_page else None

            state = StopState(
                label=label,
                endpoint=self.endpoint,
                sorting_setting=None,
                iter_index=total_paginations,
                cursor_sent=cursor_sent_this_iter,
                end_cursor=effective_end_cursor,
                start_unix=None,
                end_unix=None,
                batch_creation_times=[],
                oldest_in_batch=None,
                newest_in_batch=None,
                second_oldest_in_batch=None,
                posts_in_resp=posts_in_resp,
                new_posts_in_iter=new_posts_in_iter,
                all_posts_count=current_post_count,
                no_progress_streak=no_progress_streak,
                response_text=text,
                request_body=body,
                template_headers=template_headers,
                response_status=response.status,
                iter_start_iso=iter_start.isoformat(),
                elapsed_s=elapsed_iter,
                cursor_sent_fp=cursor_sent_fp,
                cursor_recv_fp=cursor_recv_fp,
                csr_len=csr_len,
                dyn_len=dyn_len,
                iter_window=iter_window,
                graphql_error_detail=graphql_error_detail,
            )

            for cond in stop_conditions:
                result_string = cond.evaluate(state)
                if result_string is not None:
                    return result_string, next_cursor

            cursor = end_cursor

            if (total_paginations % scroll_burst_every) == 0:
                await self._hybrid_organic_scroll_burst(
                    *scroll_burst_size_range,
                    operation_timeout_seconds=operation_timeout_seconds,
                )

            sleep_s = abs(random.gauss(pagination_sleep_mean, pagination_sleep_std))
            await asyncio.sleep(sleep_s)

    async def page_transparency_hybrid(
        self,
        page_id: str,
        handle: str | None = None,
        post_nav_sleep_seconds: float = 3.0,
        template_capture_timeout: float = 45.0,
        request_timeout_ms: int = 30000,
        operation_timeout_seconds: float = 120,
    ) -> ScrapeOutcome:
        """Scrape ProfileTransparencyDialogQuery for one page (single-shot, no pagination).

        Args:
            page_id: Numeric page id; sent as `variables.pageID` and used for
                the bootstrap navigation URL when no `handle` is supplied.
            handle: Optional vanity handle. When given, drives the bootstrap
                navigation (matches a real user typing the vanity URL).
                Defaults to using `page_id` for navigation.

        Returns:
            ScrapeOutcome with `data` as a 1-element list `[transparency_dict]`
            on success, or `[]` on failure.
        """
        self.endpoint = "PageTransparency"
        label = handle or page_id
        logger.info(
            f"[hybrid] page transparency for @{label} (page_id={page_id})"
        )

        target_url = f"https://www.facebook.com/{label}/"
        scrape_start_time = datetime.now(timezone.utc)

        self.response_interceptor.flush()
        self.response_interceptor.extract_posts = False

        # Phase 1 — navigate. Drives organic GraphQL traffic that supplies auth-bearing fields.
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
        # FB cross-checks the friendly-name header against the form field; mismatch → 400.
        headers["x-fb-friendly-name"] = PAGE_TRANSPARENCY_FRIENDLY_NAME

        response, text, error_str = await self._hybrid_send_replay(
            handle=label,
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

    async def profile_authenticity_hybrid(
        self,
        user_id: str,
        scale: int = 3,
        post_nav_sleep_seconds: float = 3.0,
        template_capture_timeout: float = 45.0,
        request_timeout_ms: int = 30000,
        operation_timeout_seconds: float = 120,
    ) -> ScrapeOutcome:
        """Scrape ProfileCometDirectoryAuthenticityModalQuery for one profile (single-shot, no pagination).

        Args:
            user_id: Numeric user id; sent as `variables.userID` and used as
                the bootstrap navigation URL (FB redirects `/<user_id>/` to
                the canonical profile page).
            scale: Image scale (FB's UI uses 3).

        Returns:
            ScrapeOutcome with `data` as a 1-element list `[authenticity_dict]`
            on success, or `[]` on failure.
        """
        self.endpoint = "ProfileAuthenticity"
        logger.info(f"[hybrid] profile authenticity for user_id={user_id}")

        target_url = f"https://www.facebook.com/{user_id}/"
        scrape_start_time = datetime.now(timezone.utc)

        self.response_interceptor.flush()
        self.response_interceptor.extract_posts = False

        # Phase 1 — navigate. Drives organic GraphQL traffic that supplies auth-bearing fields.
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

        # Phase 3 — synthesize the authenticity request and send it.
        body = self._profile_authenticity_build_body(
            template["post_data"], user_id, scale,
        )
        headers = self._hybrid_clean_headers(template["headers"])
        # FB cross-checks the friendly-name header against the form field; mismatch → 400.
        headers["x-fb-friendly-name"] = PROFILE_AUTHENTICITY_FRIENDLY_NAME

        response, text, error_str = await self._hybrid_send_replay(
            handle=str(user_id),
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
        record = self._parse_profile_authenticity_response(text)
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
        """Navigate to the profile and run a post-nav error check. Returns an error string or None."""
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
        return error

    async def _hybrid_bootstrap(self, operation_timeout_seconds: float) -> str | None:
        """Provoke the first pagination GraphQL request via a real scroll.

        The profile page does not fire ProfileCometTimelineFeedRefetchQuery on
        initial load — a small scroll is required to bootstrap it.
        """
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
        """Wait for the natural pagination request and extract a replay template.

        Returns (error_or_None, template_or_None). Template dict carries
        `form`, `headers`, `cursor` (always None — first replay uses
        cursor=null), `doc_id`, and `profile_id`. On capture failure, re-runs
        the DOM error check to surface a clean reason (private profile, etc.).
        """
        template = await self._hybrid_wait_for_template(
            template_capture_timeout,
            friendly_name=friendly_name,
            interceptor_attr=interceptor_attr,
            rescroll=True,
            operation_timeout_seconds=operation_timeout_seconds,
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
            "cursor": None,
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
        static_variable_overrides: dict | None = None,
        initial_cursor: str | None = None,
        stop_conditions: list[StopCondition] | None = None,
        inject_before_time: bool = True,
        parse_response=None,
    ) -> tuple[str, str | None]:
        """Drive paginations via page.request.post() until a stop condition fires.

        When `end_unix` is set, date-bound stop conditions use it as the
        upper edge of the window. If `inject_before_time` is also True
        (default), the replay body carries it as `beforeTime` — leave True
        for endpoints with server-side date filters (UserTimeline), False
        for endpoints without one (GroupTimeline) so we don't send an
        unrecognized variable to FB.
        When `start_unix` is set, date-bound stop conditions use it as the
        lower edge of the window.
        When `static_variable_overrides` is set, those keys are merged into
        the body's `variables` on every replay (e.g. GroupTimeline injects
        `sortingSetting=TOP_POSTS` to match FB's UI default).
        When `initial_cursor` is set (non-empty), the loop starts from that
        cursor instead of `null` — used by the `--continue` resume path.
        When `stop_conditions` is None, the canonical list is assembled by
        `assemble_default_stop_conditions(endpoint, mode, sorting_setting, params)`.

        Termination: walks `stop_conditions` each iter; the first condition
        to return a result-string terminates the loop. Auth GraphQL errors
        (raise `FailedLoginError`) and in-body rate-limit errors (return
        `('rate_limit', next_cursor)` for Worker's account-lock branch) are
        short-circuited inline BEFORE the framework walk — they map to
        non-result-string side effects that don't compose with the rest.

        Returns `(result_string, next_cursor)`. `next_cursor` is the cursor
        the *next* replay would have used had the loop continued — i.e. the
        latest `end_cursor` observed on the wire. `None` when the loop never
        completed a successful batch, or when the loop exited via the
        "end_cursor null = end of feed" path.
        """
        template_form = template["form"]
        template_headers = template["headers"]
        cursor = initial_cursor or template["cursor"]
        # Tracks the cursor the *next* iter would have sent, so early-return
        # stop conditions don't lose it. Updated immediately after each
        # successful end_cursor extraction below.
        next_cursor: str | None = None

        pagination_count = params["pagination_count"]
        scroll_burst_every = params["scroll_burst_every"]
        scroll_burst_size_range = params["scroll_burst_size_range"]
        pagination_sleep_mean = params["pagination_sleep_mean"]
        pagination_sleep_std = params["pagination_sleep_std"]
        request_timeout_ms = params["request_timeout_ms"]
        operation_timeout_seconds = params["operation_timeout_seconds"]

        sorting_setting = (static_variable_overrides or {}).get("sortingSetting")

        if stop_conditions is None:
            # then add the stop condition at the start
            stop_conditions = []
        default_stop_conditions = assemble_default_stop_conditions(
            endpoint=self.endpoint,
            mode="hybrid",
            sorting_setting=sorting_setting,
            params=params,
        )
        for d in default_stop_conditions:
            stop_conditions.append(d)


        total_paginations = 0
        no_progress_streak = 0
        previous_post_count = len(self.response_interceptor.get_posts())

        # Rolling per-iter window for the diagnostic dump path (cursor_reset,
        # graphql_error). Loop is the sole writer; conditions read it.
        iter_window: deque = deque(maxlen=HYBRID_CURSOR_RESET_WINDOW)

        while True:
            # No `afterTime` override — FB's UI never sets it, so sending one would be a fingerprint.
            overrides = {
                "cursor": cursor,
                "count": pagination_count,
            }
            if end_unix is not None and inject_before_time:
                overrides["beforeTime"] = end_unix
            if static_variable_overrides:
                # Per-iter overrides (cursor/count/beforeTime) take precedence;
                # static keys back-fill anything the loop doesn't already set.
                merged = dict(static_variable_overrides)
                merged.update(overrides)
                overrides = merged
            body = self._hybrid_build_body(template_form, overrides)

            cursor_sent_fp = self._hybrid_cursor_fp(cursor)
            csr_len = len(self.response_interceptor.latest_csr or "")
            dyn_len = len(self.response_interceptor.latest_dyn or "")
            cursor_sent_this_iter = cursor

            iter_start = datetime.now(timezone.utc)
            response, text, error_str = await self._hybrid_send_replay(
                handle=label,
                body=body,
                template_headers=template_headers,
                request_timeout_ms=request_timeout_ms,
                operation_timeout_seconds=operation_timeout_seconds,
            )
            if error_str is not None:
                return error_str, next_cursor

            await self.record_scroll(endpoint=self.endpoint, count=1)
            total_paginations += 1

            _parse = parse_response or self.response_interceptor.parser.parse_timeline_response
            try:
                parsed = _parse(text.encode("utf-8"), GRAPHQL_API_URL)
            except Exception as e:
                logger.warning(f"[hybrid] @{label}: parser raised: {e}")
                parsed = None
            if parsed and parsed.get("posts"):
                self.response_interceptor.add_posts(parsed["posts"])

            # Auth-ish errors → raise so Worker rotates the account.
            # In-body rate-limits → return 'rate_limit' so Worker locks 24h + rotates.
            # Other GraphQL errors → handled by GraphQLError stop condition below.
            graphql_error_detail = self._hybrid_extract_graphql_error_detail(text)
            if graphql_error_detail:
                gql_msg = graphql_error_detail.get("message", "")
                gql_code = graphql_error_detail.get("code")
                logger.warning(
                    f"[hybrid] @{label}: GraphQL error: {gql_msg} "
                    f"(code={gql_code}, severity={graphql_error_detail.get('severity')})"
                )
                if self._hybrid_is_auth_error(gql_msg):
                    raise FailedLoginError(
                        f"Session invalid mid-scrape (graphql error: {gql_msg})"
                    )
                if self._hybrid_is_rate_limit_error(graphql_error_detail):
                    # Partial data preserved on the ScrapeOutcome — Worker
                    # special-cases this result string to lock the account
                    # for 24h + rotate.
                    return 'rate_limit', next_cursor

            current_post_count = len(self.response_interceptor.get_posts())
            new_posts_in_iter = current_post_count - previous_post_count
            no_progress_streak = 0 if new_posts_in_iter else no_progress_streak + 1
            previous_post_count = current_post_count

            end_cursor = self._hybrid_extract_end_cursor(text)
            if end_cursor:
                # End-of-feed (end_cursor null) propagates `None` through
                # `next_cursor` so the multi-leg resume path treats it as
                # terminal. Update only when we got a fresh non-null one.
                next_cursor = end_cursor

            # Collapse the three per-post extractor calls into one pass; the
            # per-post list is also needed for ConsecutiveOutOfRange.
            # Use the already-parsed response dict to avoid re-parsing — this
            # also makes Search work (parse_search_response returns Story-shaped
            # posts that _hybrid_iter_wrapping_creation_times would miss because
            # it calls parse_timeline_response internally).
            batch_times = list(self._hybrid_iter_batch_creation_times_from_parsed(parsed))
            oldest_in_batch = min(batch_times) if batch_times else None
            newest_in_batch = max(batch_times) if batch_times else None
            second_oldest_in_batch = (
                sorted(batch_times)[1] if len(batch_times) >= 2 else None
            )

            elapsed_iter = (datetime.now(timezone.utc) - iter_start).total_seconds()
            oldest_iso = (
                datetime.fromtimestamp(oldest_in_batch, tz=timezone.utc).isoformat()
                if oldest_in_batch is not None else "n/a"
            )
            newest_iso = (
                datetime.fromtimestamp(newest_in_batch, tz=timezone.utc).isoformat()
                if newest_in_batch is not None else "n/a"
            )
            posts_in_resp = len(parsed.get("posts") or []) if parsed else 0
            cursor_recv_fp = self._hybrid_cursor_fp(end_cursor)
            logger.debug(
                f"[hybrid] @{label} pagination {total_paginations}: "
                f"{response.status} in {elapsed_iter:.2f}s, "
                f"posts now={current_post_count} (+{new_posts_in_iter}, "
                f"in_resp={posts_in_resp}), "
                f"batch=[{newest_iso} .. {oldest_iso}], "
                f"cursor_sent={cursor_sent_fp}, "
                f"cursor_recv={cursor_recv_fp}, "
                f"__csr_len={csr_len}, __dyn_len={dyn_len}, "
                f"resp_bytes={len(text or '')}"
            )

            # Append the current iter to the rolling window BEFORE walking
            # conditions — both cursor_reset and graphql_error dumps read
            # the window when they fire.
            anchor_label = "2nd_oldest" if self.endpoint == "GroupTimeline" else "oldest"
            detector_anchor = (
                second_oldest_in_batch or oldest_in_batch
                if self.endpoint == "GroupTimeline"
                else oldest_in_batch
            )
            anchor_iso = (
                datetime.fromtimestamp(detector_anchor, tz=timezone.utc).isoformat()
                if detector_anchor is not None else "n/a"
            )
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
                    "cursor_recv_fp": cursor_recv_fp,
                    "oldest_iso": oldest_iso,
                    "newest_iso": newest_iso,
                    "oldest_unix": oldest_in_batch,
                    "newest_unix": newest_in_batch,
                    "detector_anchor_unix": detector_anchor,
                    "detector_anchor_iso": anchor_iso,
                    "detector_anchor_label": anchor_label,
                    "posts_in_resp": posts_in_resp,
                },
                "session": {
                    "csr_len": csr_len,
                    "dyn_len": dyn_len,
                },
            })

            state = StopState(
                label=label,
                endpoint=self.endpoint,
                sorting_setting=sorting_setting,
                iter_index=total_paginations,
                cursor_sent=cursor_sent_this_iter,
                end_cursor=end_cursor,
                start_unix=start_unix,
                end_unix=end_unix,
                batch_creation_times=batch_times,
                oldest_in_batch=oldest_in_batch,
                newest_in_batch=newest_in_batch,
                second_oldest_in_batch=second_oldest_in_batch,
                posts_in_resp=posts_in_resp,
                new_posts_in_iter=new_posts_in_iter,
                all_posts_count=current_post_count,
                no_progress_streak=no_progress_streak,
                response_text=text,
                request_body=body,
                template_headers=template_headers,
                response_status=response.status,
                iter_start_iso=iter_start.isoformat(),
                elapsed_s=elapsed_iter,
                cursor_sent_fp=cursor_sent_fp,
                cursor_recv_fp=cursor_recv_fp,
                csr_len=csr_len,
                dyn_len=dyn_len,
                iter_window=iter_window,
                graphql_error_detail=graphql_error_detail,
            )

            for cond in stop_conditions:
                result_string = cond.evaluate(state)
                if result_string is not None:
                    # EndOfFeed semantics: `end_cursor` was null, so
                    # `next_cursor` is still the prior iter's value (or
                    # None on iter 1). The caller's multi-leg resume path
                    # treats `None` as "no resume", which matches the
                    # original behavior.
                    return result_string, next_cursor

            cursor = end_cursor

            if (total_paginations % scroll_burst_every) == 0:
                await self._hybrid_organic_scroll_burst(
                    *scroll_burst_size_range,
                    operation_timeout_seconds=operation_timeout_seconds,
                )

            sleep_s = abs(random.gauss(pagination_sleep_mean, pagination_sleep_std))
            await asyncio.sleep(sleep_s)

    async def check_error_conditions(self) -> str | None:
        """Return a short error code if the current page surfaces an FB error, else None."""
        logger.debug("check_error_conditions()")

        # Fast path: one query to check if ANY error indicator exists.
        error_indicators = (
            self.page.get_by_role("button", name="Retry")
            .or_(self.page.get_by_role("button", name="Reload page"))
            .or_(self.page.get_by_text("Profile isn't available"))
            .or_(self.page.get_by_text("This content isn't available"))
            .or_(self.page.get_by_text("Sorry, this page isn't available"))
            .or_(self.page.get_by_text("No Posts Yet"))
            .or_(self.page.get_by_text("This account is private"))
            .or_(self.page.get_by_text("Only members can see who's in the group and what they post"))
        )
        if await error_indicators.count() == 0:
            return None

        logger.debug("Error indicator detected, checking specifics...")

        retry_button = self.page.get_by_role("button", name="Retry")
        if await retry_button.count() > 0:
            if await self.page.get_by_text("account is private").count() > 0:
                return 'account is private'
            if await self.page.get_by_text("Failed to Load").count() > 0:
                return 'failed to load'

        reload_button = self.page.get_by_role('button', name='Reload page')
        if await reload_button.count() > 0:
            if await self.page.get_by_text("Something went wrong").count() > 0:
                return 'something went wrong - reload'

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

        if await self.page.get_by_text("Only members can see who's in the group and what they post").count() > 0:
            return 'group is private'

        return None

    async def record_scroll(self, endpoint: str, count: int = 1):
        """Record `count` scrolls against the current account."""
        self.scrolls_recorded += count
        await self.pool.update_scroll_count(self.account.identifier, endpoint, count)

    async def get_scroll_count(self, endpoint: str | None = None) -> int:
        """Return the scroll count (per-endpoint, or overall 24h if `endpoint` is None)."""
        return await self.pool.get_scroll_count(self.account.identifier, endpoint)

    # ==================== Navigation ====================

    async def goto(self, url: str, timeout: int = 30000, wait_until: str = "domcontentloaded"):
        logger.debug(f"goto({url})")
        await self.page.goto(url, timeout=timeout, wait_until=wait_until)

    def is_on_page(self, url: str) -> bool:
        return self.page.url == url

    async def scroll_to_element(self, element):
        await element.scroll_into_view_if_needed()

    def find_elements(self, selector: str):
        return self.page.locator(selector)

    async def scroll(self, window_height_coefficient: float = 3):
        """Scroll by `window_height_coefficient * window.innerHeight` and record one scroll."""
        logger.debug(f"scroll(coeff={window_height_coefficient}) for endpoint={self.endpoint}")
        await self.page.evaluate(f"window.scrollBy(0, window.innerHeight * {window_height_coefficient})")
        await self.record_scroll(endpoint=self.endpoint, count=1)


    # ==================== Private Helpers ====================

    async def _resolve_fingerprint(self):
        """Return a stable per-(account, host_os) fingerprint, generating + persisting on first use."""
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
        if headless and (os == "linux"):
            return "virtual"
        return headless

    def _get_proxy_dict(self) -> dict | None:
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
        """Return the oldest creation_time among intercepted posts, or None."""
        oldest_timestamp = None

        for post in posts:
            ts = (
                recursively_get_dict_value(post, 'timestamp.story.creation_time') or
                recursively_get_dict_value(post, 'created_time')
            )

            if ts:
                try:
                    if isinstance(ts, dict):
                        if len(set(ts.values())) == 1:
                            ts = list(ts.values()).pop()
                        else:
                            logger.warning(f"Post has multiple timestamps, "
                                           f"taking the latest one - "
                                           f"({[datetime.fromisoformat(ts) for ts in ts.values()]})")
                            ts = max(ts.values())

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

    @staticmethod
    def _hybrid_parse_form_data(post_data: str | None) -> dict[str, str]:
        """Parse a urlencoded form body into a dict (last value wins on duplicates)."""
        if not post_data:
            return {}
        try:
            parsed = parse_qs(post_data, keep_blank_values=True)
            return {k: v[-1] for k, v in parsed.items()}
        except Exception:
            return {}

    @staticmethod
    def _build_search_url(query_text: str, filters: dict | None = None) -> str:
        """Build a Facebook search URL with an optional filters blob.

        Known filter names are looked up in `_SEARCH_FILTER_REGISTRY` and
        encoded. Unrecognised keys are treated as raw passthrough: the key
        is used verbatim as the outer dict key (e.g. ``"city:0"``) and the
        value must be ``{"name": ..., "args": ...}``.

        If `filters` is None or empty, no ``filters=`` param is added and
        FB returns results under its default ranking.
        """
        if not filters:
            return f"https://www.facebook.com/search/top?q={quote(query_text)}"

        outer: dict[str, str] = {}
        counts: dict[str, int] = {}
        for key, kwargs in filters.items():
            if key in _SEARCH_FILTER_REGISTRY:
                spec = _SEARCH_FILTER_REGISTRY[key]
                fb_key = spec["fb_key"]
                idx = counts.get(fb_key, 0)
                counts[fb_key] = idx + 1
                kw = kwargs or {}
                args = spec["encode"](**kw)
                name = spec["name"](**kw) if callable(spec["name"]) else spec["name"]
                outer[f"{fb_key}:{idx}"] = json.dumps(
                    {"name": name, "args": args},
                    separators=(",", ":"),
                )
            else:
                # Raw passthrough — key is the full outer dict key (e.g. "city:0"),
                # value must be {"name": ..., "args": ...}. We require a ':' in the
                # key as the signal that the caller *intends* a raw blob entry; a
                # bare key (no ':') is almost certainly a typo'd known-filter name,
                # which would otherwise be silently ignored by FB. Reject it loudly.
                if ":" not in key:
                    raise ValueError(
                        f"Unknown search filter {key!r}. Known filters: "
                        f"{sorted(_SEARCH_FILTER_REGISTRY)}. For a raw passthrough "
                        f"entry, use a FB outer key containing ':' (e.g. 'city:0')."
                    )
                outer[key] = json.dumps(kwargs, separators=(",", ":"))

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
        """Drop HTTP/2 pseudo-headers and headers managed by Playwright (cookie, host, content-length, etc.)."""
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
        """Build the urlencoded form body for a hybrid replay POST.

        Applies `variable_overrides` to the template's `variables` field and
        splices in the freshest `__csr` / `__dyn` from any natural GraphQL
        POST — FB rotates these per-session and stale values eventually get
        rejected.
        """
        body = dict(template_form)
        try:
            variables = json.loads(body.get("variables", "{}"))
        except json.JSONDecodeError:
            variables = {}
        variables.update(variable_overrides)
        body["variables"] = json.dumps(variables, separators=(",", ":"))

        if self.response_interceptor.latest_csr:
            body["__csr"] = self.response_interceptor.latest_csr
        if self.response_interceptor.latest_dyn:
            body["__dyn"] = self.response_interceptor.latest_dyn

        return urlencode(body)

    async def _hybrid_wait_for_any_graphql_request(
        self, timeout_seconds: float
    ) -> dict | None:
        """Poll until any natural GraphQL POST has been observed, or timeout.

        Returns `{"post_data": str|None, "headers": dict}` or None.
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
        """Build a urlencoded form body for ProfileTransparencyDialogQuery.

        Inherits all auth/telemetry fields from the captured template;
        overrides friendly-name, variables, doc_id, and splices fresh
        __csr / __dyn.
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
        """Return the `data.page` dict from a ProfileTransparencyDialogQuery body, or None."""
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

    def _profile_authenticity_build_body(
        self,
        template_post_data: str | None,
        user_id: str,
        scale: int,
    ) -> str:
        """Build a urlencoded form body for ProfileCometDirectoryAuthenticityModalQuery.

        Inherits all auth/telemetry fields from the captured template;
        overrides friendly-name, variables, doc_id, and splices fresh
        __csr / __dyn.
        """
        template_form = self._hybrid_parse_form_data(template_post_data)
        body = dict(template_form)
        body["fb_api_req_friendly_name"] = PROFILE_AUTHENTICITY_FRIENDLY_NAME
        body["variables"] = json.dumps(
            {"scale": int(scale), "userID": str(user_id)},
            separators=(",", ":"),
        )
        body["doc_id"] = PROFILE_AUTHENTICITY_DOC_ID
        if self.response_interceptor.latest_csr:
            body["__csr"] = self.response_interceptor.latest_csr
        if self.response_interceptor.latest_dyn:
            body["__dyn"] = self.response_interceptor.latest_dyn
        return urlencode(body)

    @staticmethod
    def _parse_profile_authenticity_response(text: str) -> dict | None:
        """Return the `data.user` dict from a ProfileCometDirectoryAuthenticityModalQuery body, or None."""
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
            user = (doc.get("data") or {}).get("user")
            if isinstance(user, dict):
                return user
        return None

    async def _hybrid_wait_for_template(
        self,
        timeout_seconds: float,
        friendly_name: str = HYBRID_TARGET_FRIENDLY_NAME,
        interceptor_attr: str = "latest_pctfrq_request",
        rescroll: bool = False,
        rescroll_every_seconds: float = 3.0,
        rescroll_coefficient: float = 2.0,
        operation_timeout_seconds: float = 900.0,
    ) -> dict | None:
        """Poll until a natural <friendly_name> GraphQL request has been observed, or timeout.

        Returns `{"post_data": str|None, "headers": dict}` or None. Falls back
        to `network_capture` if FB_NETWORK_CAPTURE_ALL=1 has populated it.

        When `rescroll=True` (scroll-driven endpoints) the loop keeps scrolling
        every `rescroll_every_seconds` while it waits. A single bootstrap scroll
        often fails to provoke the feed-refetch query under load — the page may
        not have rendered when it fired, or one viewport isn't past FB's
        server-rendered posts. `scrollBy` is relative, so repeated scrolls walk
        progressively deeper until the query fires (or we time out). Single-shot
        endpoints don't scroll, so they leave `rescroll=False`. Scroll hiccups
        are swallowed — they must not abort the capture.
        """
        elapsed = 0.0
        interval = 0.5
        since_scroll = 0.0
        while elapsed < timeout_seconds:
            tpl = getattr(self.response_interceptor, interceptor_attr, None)
            if tpl is not None:
                return tpl
            for rec in self.response_interceptor.network_capture:
                req = rec.get("request") or {}
                headers = req.get("headers") or {}
                if headers.get("x-fb-friendly-name") == friendly_name:
                    return {"post_data": req.get("post_data"), "headers": headers}
                form = self._hybrid_parse_form_data(req.get("post_data"))
                if form.get("fb_api_req_friendly_name") == friendly_name:
                    return {"post_data": req.get("post_data"), "headers": headers}
            if rescroll and since_scroll >= rescroll_every_seconds:
                since_scroll = 0.0
                try:
                    await asyncio.wait_for(
                        self.scroll(window_height_coefficient=rescroll_coefficient),
                        timeout=operation_timeout_seconds,
                    )
                except Exception as e:  # noqa: BLE001 - a scroll hiccup must not abort capture
                    logger.debug(f"[hybrid] rescroll during template wait failed: {e}")
            await asyncio.sleep(interval)
            elapsed += interval
            since_scroll += interval
        return None

    @staticmethod
    def _hybrid_walk_response_for(text: str, target_keys: set[str]):
        """Yield (key, value) pairs for every nested dict key matching target_keys.

        Handles both single-JSON and JSONL bodies (FB uses JSONL with @stream/@defer).
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

    @staticmethod
    def _hybrid_iter_chunk_end_cursors(text: str):
        """Yield `(chunk_path_len, end_cursor)` for every `end_cursor` /
        `endCursor` in the response body.

        FB's @stream/@defer pagination responses are JSONL streams of patches.
        Each patch (chunk) declares the response-tree location it plugs in at
        via a top-level `path` field; the initial non-deferred chunk has no
        `path` (we treat it as path-length 0). The page-level pagination
        cursor for the requested connection (the one FB's pagination resolver
        will accept on the next replay) lives at the shortest path:

          - GroupTimeline:  ['node', 'group_feed']                (len 2)
          - UserTimeline:   ['node', 'timeline_list_feed_units']  (len 2)
          - Search:         shallow path under the SERP root      (len ~2-3)

        Inner-attachment sub-streams (Reels mini-feed, instream-video-ad,
        nested comment threads) have their own `page_info.end_cursor` values
        living at paths 5+ elements deep. Sending one of those back as
        `variables.cursor` on the page-level pagination query triggers
        `field_exception` server-side because the cursor belongs to a
        different connection entirely.
        """
        if not text:
            return
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = doc.get("path")
            path_len = len(path) if isinstance(path, list) else 0

            def walk(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k in ("end_cursor", "endCursor"):
                            yield v
                        yield from walk(v)
                elif isinstance(o, list):
                    for item in o:
                        yield from walk(item)

            for cursor in walk(doc):
                yield path_len, cursor

    @classmethod
    def _hybrid_extract_end_cursor(cls, text: str) -> str | None:
        """Return the page-level `end_cursor`, or None (= end-of-feed).

        Picks the cursor from the chunk with the shortest `path` — that's the
        page-level paginated connection's `page_info`, never a nested
        sub-stream's cursor (Reels attachment, etc.). A falsy cursor at the
        shortest path is the legitimate end-of-feed signal and returns None.
        See `_hybrid_iter_chunk_end_cursors` for details.
        """
        candidates = list(cls._hybrid_iter_chunk_end_cursors(text))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1] or None

    @staticmethod
    def _hybrid_iter_wrapping_creation_times(text: str):
        """Yield wrapping (post-itself) creation_times for every Story in the response body.

        Routes through the flattener's `_extract_times` so shared posts yield
        only the share's own date — never the inner attached_story's.
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

    @staticmethod
    def _hybrid_iter_batch_creation_times_from_parsed(parsed: dict | None):
        """Yield wrapping creation_times from an already-parsed response dict.

        Accepts the output of `parse_timeline_response` or
        `parse_search_response` (both produce `{posts: [{node: Story, ...}]}`).
        Used by `_hybrid_pagination_loop` after the parse step to avoid
        re-parsing — and to correctly handle Search responses, which
        `_hybrid_iter_wrapping_creation_times` would miss because it calls
        `parse_timeline_response` internally (which doesn't know about the
        `serpResponse` shape).
        """
        if not parsed:
            return
        parser = FacebookGraphQLParser()
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
        """Smallest wrapping creation_time in the response, or None."""
        times = list(cls._hybrid_iter_wrapping_creation_times(text))
        return min(times) if times else None

    @classmethod
    def _hybrid_extract_newest_creation_time(cls, text: str) -> int | None:
        """Largest wrapping creation_time in the response, or None."""
        times = list(cls._hybrid_iter_wrapping_creation_times(text))
        return max(times) if times else None

    @classmethod
    def _hybrid_extract_second_oldest_creation_time(cls, text: str) -> int | None:
        """Second-smallest wrapping creation_time in the response, or None
        when fewer than 2 posts had a parseable timestamp.

        Used by GroupTimeline's cursor-reset detector to ignore the
        bootstrap-edge "highlight" outlier — FB injects one anchor post
        per batch that is often chronologically out-of-order vs. the
        cursor's real position, which would otherwise produce false-
        positive cursor resets every ~150-200 paginations.
        """
        times = sorted(cls._hybrid_iter_wrapping_creation_times(text))
        if len(times) < 2:
            return None
        return times[1]

    @staticmethod
    def _hybrid_cursor_fp(cursor: str | None) -> str:
        """Compact log fingerprint for a cursor: `null` or `len=<N> sha=<8hex>`."""
        if not cursor:
            return "null"
        h = hashlib.sha1(cursor.encode("utf-8")).hexdigest()[:8]
        return f"len={len(cursor)} sha={h}"

    @classmethod
    def _hybrid_extract_graphql_error(cls, text: str) -> str | None:
        """Return the first GraphQL error message in the body (FB returns 200 + errors[]), or None."""
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

    @classmethod
    def _hybrid_extract_graphql_error_detail(cls, text: str) -> dict | None:
        """Return the first GraphQL error object as `{message, code, severity}` or None.

        Like `_hybrid_extract_graphql_error` but preserves the structured
        fields needed to disambiguate auth vs. rate-limit vs. generic errors
        without parsing the message string twice. `code` is FB's internal
        numeric error code (e.g. 1675004 for rate-limit) — more stable than
        the human-readable `message` for programmatic matching.
        """
        for _, v in cls._hybrid_walk_response_for(text, {"errors"}):
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, dict):
                    msg = first.get("message")
                    return {
                        "message":  str(msg)[:200] if msg else "",
                        "code":     first.get("code"),
                        "severity": first.get("severity"),
                    }
                elif isinstance(first, str):
                    return {"message": first[:200], "code": None, "severity": None}
        return None

    # Substrings that mark a GraphQL error as auth-related (FB returns 200 + errors[] for these).
    _HYBRID_AUTH_ERROR_MARKERS = (
        "lsddataerror",
        "useridiszero",
        "not logged in",
        "must be logged in",
        "invalid session",
        "session has expired",
    )

    # FB's numeric error codes that map to account-level rate-limiting.
    # Confirmed manually: code=1675004, severity=CRITICAL fires on accounts
    # that hit FB's account-scoped throttle — appears identically whether
    # the request is from our scraper or a human in a browser. Match by
    # code first (more stable across FB's message localisation), fall
    # through to substring match on the message as a safety net.
    _HYBRID_RATE_LIMIT_CODES = (1675004,)
    _HYBRID_RATE_LIMIT_MARKERS = ("rate limit exceeded",)

    @classmethod
    def _hybrid_is_auth_error(cls, message: str) -> bool:
        """True if a GraphQL error message looks auth-related."""
        if not message:
            return False
        m = message.lower()
        return any(marker in m for marker in cls._HYBRID_AUTH_ERROR_MARKERS)

    @classmethod
    def _hybrid_is_rate_limit_error(cls, err: dict | None) -> bool:
        """True if a GraphQL error object signals account-level rate-limiting.

        Matches FB's internal code (1675004) when present (most reliable),
        else falls back to a case-insensitive substring scan on the message
        ("Rate limit exceeded"). Either alone is sufficient — FB has been
        observed to emit both forms.
        """
        if not err:
            return False
        code = err.get("code")
        if code is not None and code in cls._HYBRID_RATE_LIMIT_CODES:
            return True
        msg = (err.get("message") or "").lower()
        return any(marker in msg for marker in cls._HYBRID_RATE_LIMIT_MARKERS)

    @staticmethod
    def _hybrid_body_looks_like_html(text: str) -> bool:
        """True if the body is HTML (FB redirected our POST to login)."""
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
        """Send one replay POST with 5xx retry and HTTP-status classification.

        Returns `(response, body, None)` on success, `(None, "", error)` on
        terminal non-rotation failure, or raises FailedLoginError /
        AccountBannedError / RateLimitError on 401 / 403 / 429.
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

            # Other non-200 (400, 404, non-retryable 5xx) — bail with a result string, no rotation.
            if status != 200:
                logger.warning(
                    f"[hybrid] @{handle}: HTTP {status} — bailing (no retry)"
                )
                return None, "", f'pagination_error: HTTP {status}'

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
        """Fire a small burst of real scrolls between replay bursts to mimic an intermittent reader."""
        n_scrolls = random.randint(min_scrolls, max_scrolls)
        logger.debug(f"[hybrid] organic-scroll burst: {n_scrolls} scrolls")
        for j in range(n_scrolls):
            try:
                await asyncio.wait_for(
                    self.scroll(window_height_coefficient=1.0),
                    timeout=operation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Best-effort fingerprint cosmetics; abort the burst, not the scrape.
                logger.warning(
                    f"[hybrid] burst scroll {j+1}/{n_scrolls} timed out — "
                    f"aborting burst"
                )
                break
            except Exception as e:
                logger.warning(f"[hybrid] burst scroll {j+1} failed: {e}")
                break
            await asyncio.sleep(abs(random.gauss(2.5, 0.5)))
