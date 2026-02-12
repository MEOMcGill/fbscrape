import asyncio

from .accounts_pool import AccountsPool
from .worker import Worker

class WorkerPool:
    def __init__(
            self,
            pool: AccountsPool,
            max_workers: int = 5,
            scroll_threshold: int = 100,
            headless: bool = False
    ):
        self.pool = pool
        self.max_workers = max_workers
        self.scroll_threshold = scroll_threshold
        self.headless = headless

        self.workers: list[Worker] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._running: bool = False


    async def initialize(self):
        """Start worker pool - creates browser sessions"""
        active_accounts = await self.pool.get_active_accounts()
        num_active_accounts = len(active_accounts)
        num_workers = min(self.max_workers, num_active_accounts)

        for i in range(num_workers):
            worker = Worker(
                id=f"worker-{i}",
                pool=self.pool,
                scroll_threshold=self.scroll_threshold,
                headless=self.headless
            )

