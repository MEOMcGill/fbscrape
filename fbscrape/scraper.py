"""
Core Facebook scraping API - delegates to WorkerPool for orchestration.

Provides a high-level interface for scraping Facebook that manages
browser sessions and account rotation automatically.
"""

import asyncio
import json
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
        max_posts: int = -1,
        resume_from: str | None = None,
        # Other mode-specific tuning knobs (pagination_count, scroll_burst_every,
        # max_paginations, etc.) are accepted as kwargs; allowed keys live in
        # Query.ENDPOINT_REGISTRY. Anything left as None gets the registry default.
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
            max_posts: Hard cap on the number of accumulated posts. `-1`
                  (default) disables the cap. Hybrid-only: enforced inside
                  `_hybrid_pagination_loop` at batch boundaries, so the
                  returned count can exceed `max_posts` by up to
                  `pagination_count - 1`. When the cap fires, `result.result
                  == "max_posts_reached"`. Note: for multi-leg scrapes
                  (cursor_reset resume), enforcement is per-leg; cross-leg
                  totals can exceed in rare cases.
            resume_from: Path (`.json` or `.json.gz`) to a previously-saved
                  `ScrapingResult` to resume from. Hybrid-only — raises
                  `ValueError` if used with `mode="manual"` (scroll-driven
                  scraping has no cursor concept). The saved `last_cursor`
                  is fed into **leg 0** of the multi-leg cursor_reset loop
                  only; subsequent legs (post cursor_reset) start fresh
                  with adjusted `end_date`. Saved `post_id`s seed dedup on
                  every leg so re-served bootstrap-edge posts don't
                  duplicate. Returned `data` contains only **new** posts
                  from this run — the CLI's `--continue` flag handles the
                  merge with the existing file.
            **params: other mode-specific tuning knobs. Allowed keys and
                  defaults live in Query.ENDPOINT_REGISTRY[("UserTimeline", mode)]["params"].
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
        # max_posts is hybrid-only — the manual mode doesn't route through the
        # hybrid pagination loop, so skip injection there to avoid Query's
        # unknown-param rejection. The default `-1` (no cap) silently ignores
        # the kwarg when mode="manual" was the intent anyway.
        if mode == "hybrid":
            cleaned_params["max_posts"] = max_posts

        # Resume support (hybrid-only — manual mode has no cursor concept).
        # Cursor goes to leg 0 only (post cursor_reset legs start fresh with
        # adjusted end_date by design). Saved post_ids seed dedup on every
        # leg so re-served bootstrap-edge posts don't duplicate, AND go into
        # the cross-leg `seen_post_ids` accumulator below so flatten-based
        # dedup catches anything the interceptor misses.
        initial_cursor = ""
        saved_post_ids: list[str] = []
        if resume_from is not None:
            if mode != "hybrid":
                raise ValueError(
                    f"resume_from is only supported with mode='hybrid'; "
                    f"manual mode has no cursor concept (got mode={mode!r})"
                )
            from .cli import _open_scrape_input  # local: avoids cycle at module load
            with _open_scrape_input(resume_from) as f:
                saved = json.load(f)
            initial_cursor = saved.get("last_cursor") or ""
            saved_records = saved.get("data") or saved.get("posts") or []
            saved_post_ids = [
                pid
                for rec in saved_records
                for pid in [(rec.get("node") or {}).get("post_id")
                            or rec.get("post_id")]
                if pid
            ]
            logger.info(
                f"UserTimeline resume from {resume_from}: "
                f"cursor={'<set>' if initial_cursor else '<null>'}, "
                f"seen_post_ids={len(saved_post_ids)}"
            )

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
        # Cross-leg dedup. Seeded from the resume file so a post that's
        # already in the prior file doesn't get re-added even if FB re-serves
        # it across legs (rare but possible).
        seen_post_ids: set[str] = set(saved_post_ids)
        accumulated_posts: list[dict] = []
        legs: list[ScrapingResult] = []
        final_result: str | None = None

        for leg_idx in range(MAX_CURSOR_RESET_RESUMES + 1):
            # Leg-specific param overlay on top of cleaned_params. The cursor
            # applies only to leg 0; subsequent legs start fresh by design.
            # Saved post_ids apply to every leg (interceptor-level dedup).
            leg_params = dict(cleaned_params)
            if mode == "hybrid" and saved_post_ids:
                leg_params["seen_post_ids_to_skip"] = saved_post_ids
            if mode == "hybrid" and leg_idx == 0 and initial_cursor:
                leg_params["initial_cursor"] = initial_cursor

            query = Query(
                endpoint="UserTimeline",
                mode=mode,
                query={
                    "handle": handle,
                    "start_date": start_date,
                    "end_date": current_end_date,
                },
                params=leg_params,
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
            # Resume point for a future `--continue`: the *last* leg's
            # last_cursor. Earlier legs' cursors are dead (they triggered
            # cursor_reset; the multi-leg loop has already moved past them).
            last_cursor=legs[-1].last_cursor,
        )
        logger.info(
            f"Completed UserTimeline (mode={mode}) for handle={handle}: "
            f"{combined.result} ({len(combined.data)} posts, "
            f"{len(legs)} legs, {combined.time_taken})"
        )
        return combined

    async def search(
        self,
        query_text: str,
        start_date: str,
        end_date: str,
        mode: str = "hybrid",
        max_posts: int = -1,
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
            max_posts: Hard cap on the number of accumulated posts. `-1`
                  (default) disables the cap. Enforced at batch boundaries
                  inside `_hybrid_pagination_loop`, so the returned count can
                  exceed by up to `pagination_count - 1`. When the cap fires,
                  `result.result == "max_posts_reached"`.
            **params: other mode-specific tuning knobs. Allowed keys and
                  defaults live in Query.ENDPOINT_REGISTRY[("Search", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult with outcome and collected records (one per post).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        cleaned_params = {k: v for k, v in params.items() if v is not None}
        cleaned_params["max_posts"] = max_posts

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
        logger.info(
            f"Completed Search (mode={mode}) for query_text={query_text!r}: "
            f"{result.result} ({len(result.data)} posts, {result.time_taken})"
        )
        return result

    async def group_timeline(
        self,
        handle: str,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: str = "hybrid",
        max_posts: int = -1,
        resume_from: str | None = None,
        **params,
    ) -> ScrapingResult:
        """
        Scrape a Facebook group's feed between two dates.

        Targets `GroupsCometFeedRegularStoriesPaginationQuery` (GCFRSPQ).
        Termination is purely client-side — the GraphQL query carries no
        beforeTime/afterTime variable, so the loop relies on parser-extracted
        creation_time vs. start_date.

        Unlike `user_timeline`, there is no cursor-reset multi-leg resume: a
        cursor_reset return string is terminal for GroupTimeline (no server-
        side date filter exists to advance for a fresh leg). Partial data is
        preserved on `result`.

        Args:
            handle: Vanity group handle (e.g. "albertaseparatism") or the
                numeric group id — both forms resolve via `/groups/<handle>/`.
            start_date: Start date (YYYY-MM-DD), inclusive.
            end_date:   End date (YYYY-MM-DD), inclusive — advisory, since
                FB has no server-side filter; it only bounds the client-side
                stop check.
            mode: Currently only "hybrid" is supported.
            max_posts: Hard cap on the number of accumulated posts. `-1`
                  (default) disables the cap. Enforced at batch boundaries
                  inside `_hybrid_pagination_loop`, so the returned count can
                  exceed by up to `pagination_count - 1`. When the cap fires,
                  `result.result == "max_posts_reached"`. Useful for bounding
                  open-ended scrapes where the group is large and you only
                  want the top-N most recent posts.
            resume_from: Path (`.json` or `.json.gz`) to a previously-saved
                  `ScrapingResult` from a partially-completed scrape. When
                  given, the loop starts from that file's `last_cursor`
                  instead of `null`, and the saved posts' `post_id`s are
                  used to seed the interceptor's dedup set so the bootstrap
                  edge can't re-add previously-collected posts. The returned
                  `ScrapingResult.data` contains **only the new posts** from
                  this run — the CLI's `--continue` flag handles merging
                  with the existing file. If the saved `last_cursor` is
                  `None` (= prior scrape reached end of feed cleanly),
                  resume is a no-op and a fresh `cursor=null` scrape runs.
            **params: other mode-specific tuning knobs. Allowed keys and
                  defaults live in Query.ENDPOINT_REGISTRY[("GroupTimeline", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult with outcome and collected records (one per post).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        cleaned_params = {k: v for k, v in params.items() if v is not None}
        cleaned_params["max_posts"] = max_posts

        # Resume support: pull last_cursor + post_ids out of a prior saved
        # ScrapingResult and thread them through as Query params. Accepts
        # both .json and .json.gz transparently.
        if resume_from:
            from .cli import _open_scrape_input  # local import: avoids cycle at module load
            with _open_scrape_input(resume_from) as f:
                saved = json.load(f)
            saved_cursor = saved.get("last_cursor") or ""
            saved_records = saved.get("data") or saved.get("posts") or []
            saved_post_ids = [
                pid
                for rec in saved_records
                for pid in [(rec.get("node") or {}).get("post_id")
                            or rec.get("post_id")]
                if pid
            ]
            cleaned_params["initial_cursor"] = saved_cursor
            cleaned_params["seen_post_ids_to_skip"] = saved_post_ids
            logger.info(
                f"GroupTimeline resume from {resume_from}: "
                f"cursor={'<set>' if saved_cursor else '<null>'}, "
                f"seen_post_ids={len(saved_post_ids)}"
            )

        query = Query(
            endpoint="GroupTimeline",
            mode=mode,
            query={
                "handle": handle,
                "start_date": start_date,
                "end_date": end_date,
            },
            params=cleaned_params,
        )

        logger.debug(
            f"Submitting GroupTimeline task (mode={mode}) for handle={handle}, "
            f"date_range={start_date} to {end_date}"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.info(
            f"Completed GroupTimeline (mode={mode}) for handle={handle}: "
            f"{result.result} ({len(result.data)} posts, {result.time_taken})"
        )
        return result

    async def page_transparency(
        self,
        page_id: str,
        handle: str | None = None,
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

        `page_id` is the only required input — bootstrap navigation hits
        `https://www.facebook.com/<page_id>/` and FB redirects to the
        canonical page URL. Pass `handle` only if you want the vanity URL
        in the navigation (closer to a real user typing it).

        Args:
            page_id: Numeric page id (e.g. `"899800046546098"`), sent as
                `variables.pageID`.
            handle: Optional vanity handle (e.g. "habsfanhub"). When given,
                used for the bootstrap navigation URL instead of `page_id`.
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

        query_dict: dict = {"page_id": page_id}
        if handle is not None:
            query_dict["handle"] = handle

        query = Query(
            endpoint="PageTransparency",
            mode=mode,
            query=query_dict,
            params=cleaned_params,
        )

        label = handle or page_id
        logger.debug(
            f"Submitting PageTransparency task (mode={mode}) for "
            f"page_id={page_id} (nav={label})"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.info(
            f"Completed PageTransparency (mode={mode}) for page_id={page_id}: "
            f"{result.result} ({len(result.data)} records, {result.time_taken})"
        )
        return result

    async def profile_authenticity(
        self,
        user_id: str,
        mode: str = "hybrid",
        **params,
    ) -> ScrapingResult:
        """
        Scrape Facebook profile authenticity info for a given user/profile.

        Targets `ProfileCometDirectoryAuthenticityModalQuery` — the data
        behind FB's "About this profile / authenticity" modal. Single-shot,
        no pagination, no date filter. Returns a `ScrapingResult` whose
        `data` is a 1-element list containing the authenticity record
        (display name, delegate page id, profile join date, profile
        updated-since, category, meta-verified section, about fields).

        Args:
            user_id: Numeric user id (e.g. `"100044331674441"`). Sent as
                `variables.userID` and used as the bootstrap navigation URL
                (`https://www.facebook.com/<user_id>/` — FB redirects to
                the canonical profile, so no handle resolution is needed).
            mode: Currently only "hybrid" is supported.
            **params: mode-specific tuning knobs. Allowed keys and defaults
                  live in Query.ENDPOINT_REGISTRY[("ProfileAuthenticity", mode)]["params"].
                  Pass `None` (or omit) to use the registry default.

        Returns:
            ScrapingResult — `data` is `[authenticity_dict]` on success or
            `[]` on failure (with `result` carrying the reason).

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized()

        cleaned_params = {k: v for k, v in params.items() if v is not None}

        query = Query(
            endpoint="ProfileAuthenticity",
            mode=mode,
            query={"user_id": user_id},
            params=cleaned_params,
        )

        logger.debug(
            f"Submitting ProfileAuthenticity task (mode={mode}) for "
            f"user_id={user_id}"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.info(
            f"Completed ProfileAuthenticity (mode={mode}) for user_id={user_id}: "
            f"{result.result} ({len(result.data)} records, {result.time_taken})"
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
