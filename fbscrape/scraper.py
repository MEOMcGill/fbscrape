"""
Core Facebook scraping API - delegates to WorkerPool for orchestration.

Provides a high-level interface for scraping Facebook that manages
browser sessions and account rotation automatically.
"""

import asyncio

from .accounts_pool import AccountsPool
from .logger import logger
from .models import Query, ScrapingResult
from .worker_pool import WorkerPool


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
            ScrapingResult with outcome and collected posts

        Raises:
            NoAccountError: If no accounts available in pool
            ValueError: If endpoint/mode/query/params validation fails
        """
        await self._ensure_initialized() # unsure what this line does

        # Drop None entries so registry defaults win in Query.__post_init__.
        cleaned_params = {k: v for k, v in params.items() if v is not None}

        query = Query(
            endpoint="UserTimeline",
            mode=mode,
            query={
                "handle": handle,
                "start_date": start_date,
                "end_date": end_date,
            },
            params=cleaned_params,
        )

        logger.debug(
            f"Submitting UserTimeline task (mode={mode}) for handle={handle}, "
            f"date_range={start_date} to {end_date}"
        )
        future = await self.worker_pool.submit_task(query)
        result = await future
        logger.debug(
            f"Completed UserTimeline (mode={mode}) for handle={handle}, "
            f"result={result.result}, posts={len(result.posts)}"
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
