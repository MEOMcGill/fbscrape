"""
Core Facebook scraping API - delegates to BrowserSession and WorkerPool
"""

from .accounts_pool import AccountsPool
from .models import ScrapingResult


class FacebookScraper:
    """
    High-level API for scraping Facebook.

    Manages browser sessions and account rotation through WorkerPool.
    """

    def __init__(
            self,
            db: str | AccountsPool = "accounts.db",
            max_browser_sessions: int = 5,
            scroll_threshold: int = 100,
            headless: bool = False
    ):
        """
        Initialize Facebook scraper.

        Args:
            db: Path to accounts database or AccountsPool instance
            max_browser_sessions: Maximum concurrent browser sessions
            scroll_threshold: Scrolls before rotating account
            headless: Run browsers in headless mode
        """
        self.pool = db if isinstance(db, AccountsPool) else AccountsPool(db)
        self.max_browser_sessions = max_browser_sessions
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.worker_pool = None

    async def scrape_user_homepage(
        self,
        handle: str,
        start_date: str,
        end_date: str,
    ) -> ScrapingResult:
        """
        Scrape a Facebook user's homepage.

        Args:
            handle: Facebook username/handle
            start_date: Start date for scraping (YYYY-MM-DD)
            end_date: End date for scraping (YYYY-MM-DD)

        Returns:
            ScrapingResult with outcome and collected data
        """
        # TODO: Use WorkerPool to manage browser sessions
        # For now, this is a placeholder - actual scraping is done via BrowserSession
        raise NotImplementedError(
            "Use WorkerPool.submit_task() or BrowserSession.scrape_user_homepage() directly"
        )

    async def close(self):
        """Cleanup all browser sessions"""
        if self.worker_pool:
            await self.worker_pool.close()