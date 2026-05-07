"""
Core Facebook scraping API - delegates to WorkerPool for orchestration.

Provides a high-level interface for scraping Facebook that manages
browser sessions and account rotation automatically.
"""

import asyncio
from datetime import datetime, timezone, timedelta

from .accounts_pool import AccountsPool
from .logger import logger
from .models import Query, ScrapingResult
from .response import FacebookGraphQLParser
from .worker_pool import WorkerPool

# Cap on how many times a single high-level user_timeline call will resume
# after a `cursor_reset` leg. Each resume submits a fresh task with end_date
# advanced backward to the oldest collected post's day, gets a fresh browser
# session, and (because Worker locks the prior account 30 min on cursor_reset)
# typically a fresh account too. Bounds wall-clock + account spend on a
# stubborn target. Promote to a Query.params field if per-scrape tuning is
# ever needed.
MAX_CURSOR_RESET_RESUMES = 5


class FacebookScraper:
    """
    High-level API for scraping Facebook.

    Manages browser sessions and account rotation through WorkerPool.
    Supports both direct async/await usage and async context manager.

    Usage:
        # Context manager (recommended)
        async with FacebookScraper(db="accounts.db") as scraper:
            result = await scraper.user_timeline("zuck", "2024-01-01", "2025-01-01")

        # Direct usage
        scraper = FacebookScraper(db="accounts.db")
        result = await scraper.user_timeline("zuck", "2024-01-01", "2025-01-01")
        await scraper.close()

        # With gather for multiple handles
        async for result in gather(
            scraper.user_timeline(h, "2024-01-01", "2025-01-01")
            for h in handles
        ):
            print(result)
    """

    def __init__(
        self,
        db: str | AccountsPool = "accounts.db",
        max_browser_sessions: int = 5,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        raise_when_no_account: bool = True,
    ):
        """
        Initialize Facebook scraper.

        Args:
            db: Path to accounts database or AccountsPool instance
            max_browser_sessions: Maximum concurrent browser sessions
            scroll_threshold: Scrolls before rotating account
            headless: Run browsers in headless mode
            mobile: Use mobile browser emulation
            raise_when_no_account: If True (default), raise NoAccountError when
                no account is available. If False, block (polling every 5s)
                until an account frees up — useful for long-running scrapes
                where you'd rather idle than abort. Threaded down to Worker.

        Note: per-call knobs like `stall_timeout_seconds` are passed to
        `user_timeline()` (see Query.ENDPOINT_REGISTRY), not here.
        """
        self.pool = db if isinstance(db, AccountsPool) else AccountsPool(db)
        self.max_browser_sessions = max_browser_sessions
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.raise_when_no_account = raise_when_no_account
        self.worker_pool: WorkerPool | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self):
        """Lazy initialization of WorkerPool with lock to prevent race conditions."""
        async with self._init_lock:
            if self.worker_pool is None:
                logger.debug(f"Initializing WorkerPool with max_workers={self.max_browser_sessions}")
                self.worker_pool = WorkerPool(
                    pool=self.pool,
                    max_workers=self.max_browser_sessions,
                    scroll_threshold=self.scroll_threshold,
                    headless=self.headless,
                    mobile=self.mobile,
                    raise_when_no_account=self.raise_when_no_account,
                )

    async def user_timeline(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        mode: str = "hybrid",
        # Mode-specific tuning knobs. All optional. Anything left as None gets
        # the registry default from Query.ENDPOINT_REGISTRY at Query construction.
        **params,
    ) -> ScrapingResult:
        """
        Scrape a Facebook user's homepage/timeline.

        Args:
            handle: Facebook username/handle (e.g., "zuck")
            start_date: Start date for scraping (YYYY-MM-DD format)
            end_date: End date for scraping (YYYY-MM-DD format)
            mode: "manual" (scroll-driven) or "hybrid" (page.request POST-driven,
                  no scroll-induced DOM growth — default).
            **params: mode-specific tuning knobs. Allowed keys and defaults
                  live in Query.ENDPOINT_REGISTRY[("UserTimeline", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult with outcome and collected records (one per post).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        # Drop None entries so registry defaults win in Query.__post_init__.
        cleaned_params = {k: v for k, v in params.items() if v is not None}

        # Multi-leg resume loop. Each leg is a single submit→await; if a leg
        # comes back with `result='cursor_reset'` we adjust `end_date` back
        # to the oldest collected post's day and submit again. Worker locked
        # the prior account on the cursor_reset return so we naturally get a
        # different account on the next leg (if one is available).
        #
        # Posts arrive in raw `{node: ...}` GraphQL shape — we flatten through
        # the parser to get the canonical wrapping `post_id` and `created_at`.
        # Walking raw nested `creation_time` keys would mis-attribute inner
        # `attached_story` timestamps (e.g. a recent share of a 2018 post)
        # and could send the resume `end_date` years before the real frontier.
        parser = FacebookGraphQLParser()
        # Cache flattened summary alongside accumulated_posts so we don't
        # re-flatten on every iteration of the frontier computation.
        accumulated_meta: list[dict] = []  # [{'post_id': str|None, 'created_at': int|None}, ...]

        current_end_date = end_date
        seen_post_ids: set[str] = set()
        accumulated_posts: list[dict] = []
        legs: list[ScrapingResult] = []
        final_result: str | None = None

        for leg_idx in range(MAX_CURSOR_RESET_RESUMES + 1):
            query = Query(
                endpoint="UserTimeline",
                mode=mode,
                query={
                    "handle": handle,
                    "start_date": start_date,
                    "end_date": current_end_date,
                },
                params=dict(cleaned_params),  # fresh dict per leg; Query mutates
            )

            logger.debug(
                f"Submitting UserTimeline task (mode={mode}, leg={leg_idx}) for "
                f"handle={handle}, date_range={start_date} to {current_end_date}"
            )
            future = await self.worker_pool.submit_task(query)
            leg = await future
            legs.append(leg)

            # Cross-leg dedup on the wrapping (flattened) post_id. A resume's
            # beforeTime is end_of_day(prior leg's oldest), so the new leg's
            # first batches naturally overlap with what the prior leg got.
            new_count = 0
            for post in leg.data:
                flat = parser.flatten(post, endpoint=query.endpoint)
                pid = flat.get("post_id") if flat else None
                if pid:
                    if pid in seen_post_ids:
                        continue
                    seen_post_ids.add(pid)
                accumulated_posts.append(post)
                accumulated_meta.append({
                    "post_id": pid,
                    "created_at": flat.get("created_at") if flat else None,
                })
                new_count += 1

            logger.debug(
                f"UserTimeline leg {leg_idx} for handle={handle}: "
                f"result={leg.result}, leg_posts={len(leg.data)} "
                f"(new={new_count}), accumulated={len(accumulated_posts)}"
            )

            if leg.result != "cursor_reset":
                final_result = leg.result
                break

            # Compute next leg's end_date from the oldest accumulated post's
            # WRAPPING creation_time (the share's own timestamp, not any
            # inner attached_story timestamp).
            oldest_unix = min(
                (m["created_at"] for m in accumulated_meta
                 if isinstance(m.get("created_at"), (int, float))),
                default=None,
            )
            if oldest_unix is None:
                final_result = "cursor_reset_no_posts"
                logger.warning(
                    f"UserTimeline for handle={handle}: cursor_reset on leg "
                    f"{leg_idx} with no posts collected — cannot resume"
                )
                break

            new_end_date = datetime.fromtimestamp(
                oldest_unix, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            if new_end_date >= current_end_date:
                final_result = "cursor_reset_no_progress"
                logger.warning(
                    f"UserTimeline for handle={handle}: cursor_reset on leg "
                    f"{leg_idx} but oldest post date ({new_end_date}) did not "
                    f"advance past current end_date ({current_end_date}) — bailing"
                )
                break

            logger.info(
                f"UserTimeline for handle={handle}: cursor_reset on leg "
                f"{leg_idx}; resuming with end_date={new_end_date} "
                f"(was {current_end_date})"
            )
            current_end_date = new_end_date
        else:
            final_result = "cursor_reset_max_retries"
            logger.warning(
                f"UserTimeline for handle={handle}: hit "
                f"MAX_CURSOR_RESET_RESUMES={MAX_CURSOR_RESET_RESUMES} — bailing"
            )

        combined = ScrapingResult(
            query=legs[0].query,
            result=final_result if final_result is not None else legs[-1].result,
            data=accumulated_posts,
            time_started=legs[0].time_started,
            time_taken=sum(
                (leg.time_taken for leg in legs),
                start=timedelta(),
            ),
        )
        logger.debug(
            f"Completed UserTimeline (mode={mode}) for handle={handle}, "
            f"result={combined.result}, posts={len(combined.data)}, "
            f"legs={len(legs)}"
        )
        return combined

    async def search(
        self,
        query_text: str,
        start_date: str,
        end_date: str,
        mode: str = "hybrid",
        **params,
    ) -> ScrapingResult:
        """
        Scrape Facebook search results for `query_text` between two dates.

        Targets `SearchCometResultsPaginatedResultsQuery`. Date bounds are
        applied via the search URL's filter blob (Latest posts +
        creation_time), not as GraphQL variables.

        Args:
            query_text: Free-form search term.
            start_date: Start date (YYYY-MM-DD), inclusive.
            end_date:   End date (YYYY-MM-DD), inclusive.
            mode: Currently only "hybrid" is supported.
            **params: mode-specific tuning knobs. Allowed keys and defaults
                  live in Query.ENDPOINT_REGISTRY[("Search", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult with outcome and collected records (one per post).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        cleaned_params = {k: v for k, v in params.items() if v is not None}

        query = Query(
            endpoint="Search",
            mode=mode,
            query={
                "query_text": query_text,
                "start_date": start_date,
                "end_date": end_date,
            },
            params=cleaned_params,
        )

        logger.debug(
            f"Submitting Search task (mode={mode}) for query_text={query_text!r}, "
            f"date_range={start_date} to {end_date}"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.debug(
            f"Completed Search (mode={mode}) for query_text={query_text!r}, "
            f"result={result.result}, posts={len(result.data)}"
        )
        return result

    async def page_transparency(
        self,
        handle: str,
        page_id: str,
        mode: str = "hybrid",
        **params,
    ) -> ScrapingResult:
        """
        Scrape Facebook page transparency info for a given page.

        Targets `ProfileTransparencyDialogQuery` — the data behind FB's
        "Page transparency" dialog. Single-shot, no pagination, no date
        filter. Returns a `ScrapingResult` whose `data` is a 1-element
        list containing the transparency record (page name, profile pic,
        creation date in `history_items[item_type==CREATION]`, name-change
        history, admin country breakdown, ad activity flags, verification).

        Caller supplies both `handle` and `page_id`:
          - `handle` drives the bootstrap navigation. Keeps the replay POST
            firing alongside organic GraphQL traffic on the same profile
            page rather than as a cold request.
          - `page_id` is the numeric page id (e.g. `"899800046546098"`),
            sent as `variables.pageID`. We don't resolve handle → page_id.

        Args:
            handle: Facebook page handle (e.g. "habsfanhub").
            page_id: Numeric page id as a string.
            mode: Currently only "hybrid" is supported.
            **params: mode-specific tuning knobs. Allowed keys and defaults
                  live in Query.ENDPOINT_REGISTRY[("PageTransparency", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult — `data` is `[transparency_dict]` on success or
            `[]` on failure (with `result` carrying the reason).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        cleaned_params = {k: v for k, v in params.items() if v is not None}

        query = Query(
            endpoint="PageTransparency",
            mode=mode,
            query={
                "handle": handle,
                "page_id": page_id,
            },
            params=cleaned_params,
        )

        logger.debug(
            f"Submitting PageTransparency task (mode={mode}) for "
            f"handle={handle}, page_id={page_id}"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.debug(
            f"Completed PageTransparency (mode={mode}) for handle={handle}, "
            f"result={result.result}, records={len(result.data)}"
        )
        return result

    async def close(self):
        """Cleanup all browser sessions and release accounts."""
        logger.debug("Closing FacebookScraper")
        if self.worker_pool:
            await self.worker_pool.close()
            self.worker_pool = None
        logger.debug("FacebookScraper closed")

    async def __aenter__(self) -> "FacebookScraper":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Async context manager exit - cleanup resources."""
        await self.close()
        return False
