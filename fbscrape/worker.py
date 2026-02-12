import asyncio

from .accounts_pool import AccountsPool
from .account import Account
from .logger import logger
from .browser_session import BrowserSession
from .models import Query, ScrapingResult

from typing import Optional

class Worker:
    """This deals with a single browser session: execute tasks and handle account rotation"""
    def __init__(
            self,
            id: str,
            pool: AccountsPool,
            scroll_threshold: int = 500,
            headless: bool = False,
            mobile: bool = False,
            queue: str = "general"
    ):
        self.id = id
        self.pool = pool
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.queue = queue

        # set at init
        self.current_account: Optional[Account] = None
        self.browser_session: Optional[BrowserSession] = None
        self.scroll_count = 0

    async def initialize(self) -> bool:
        # fetch a free account
        account: Account =  await self.pool.get_for_queue(queue=self.queue)
        if not account:
            logger.warning("no account available for worker")
            return False
        self.current_account = account
        self.browser_session = await BrowserSession.create(account=self.current_account, headless=self.headless, mobile=self.mobile)
        self.scroll_count: int = 0
        logger.info(f"worker {self.id} initialized with account {self.current_account.email}")
        return True

    async def run(self, task_queue: asyncio.Queue):
        while not task_queue.empty():
            task: Query = await task_queue.get()
            try:
                result: ScrapingResult = None
            except Exception as e:
                logger.error(f'worker {self.id} error: {e}')

    async def execute_task(self, task: Query) -> ScrapingResult:
        """Execute a single scraping task and rotate accounts if needed"""
        if self.scroll_count > self.scroll_threshold:
            logger.info(f'worker {self.id} reached scroll threshold ({self.scroll_threshold}), rotating account {self.current_account.email}')
            await self.rotate_account()

        try:
            # execute scraping task
            result = None

        # here list all the types of scraping errors you can encounter
        # (e.g. account banned, max limit of calls, private account etc...)
        except Exception as e:
            logger.error(f'worker {self.id} error: {e}')

    async def rotate_account(self):
        """Release current account, get new one, recreate browser session"""
        # Release old account
        if self.current_account:
            await self.pool.release_account(self.current_account, queue=self.queue)

        # Close old browser session
        if self.browser_session:
            await self.browser_session.close()

        # Reinitialize with new account
        await self.initialize()

    async def close(self):
        """Cleanup browser session"""
        if self.browser_session:
            await self.browser_session.close()
        if self.current_account:
            await self.pool.release_account(self.current_account, "user_page")

