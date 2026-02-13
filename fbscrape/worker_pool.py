"""
WorkerPool for managing concurrent scraping workers.

Manages a pool of Workers that pull tasks from a shared queue and
return results via Futures.
"""

import asyncio
from typing import Optional

from .accounts_pool import AccountsPool
from .exceptions import NoAccountError
from .logger import logger
from .models import Query, ScrapingResult
from .worker import Worker


class WorkerPool:
    """
    Manages a pool of Workers for concurrent scraping.

    Workers pull tasks from a shared asyncio.Queue and resolve Futures
    with ScrapingResults. Lazy initialization - workers are created on
    first submit_task() call.
    """

    def __init__(
        self,
        pool: AccountsPool,
        max_workers: int = 5,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        queue_name: str = "general",
    ):
        """
        Initialize WorkerPool configuration.

        Args:
            pool: AccountsPool for account management
            max_workers: Maximum number of concurrent workers
            scroll_threshold: Scrolls before rotating account
            headless: Run browsers in headless mode
            mobile: Use mobile browser emulation
            queue_name: Queue name for account locking
        """
        self.pool = pool
        self.max_workers = max_workers
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.queue_name = queue_name

        # State
        self.workers: list[Worker] = []
        self.worker_tasks: list[asyncio.Task] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._initialized: bool = False
        self._shutdown: bool = False

    async def initialize(self, num_tasks: Optional[int] = None) -> int:
        """
        Initialize workers based on available accounts and pending tasks.

        Args:
            num_tasks: Optional number of pending tasks to optimize worker count

        Returns:
            Number of workers created

        Raises:
            NoAccountError: If no active accounts available
        """
        if self._initialized:
            return len(self.workers)

        active_accounts = await self.pool.get_active_accounts()
        num_active = len(active_accounts)

        if num_active == 0:
            raise NoAccountError("No active accounts available in pool")

        # Calculate optimal worker count
        if num_tasks is not None:
            num_workers = min(self.max_workers, num_active, num_tasks)
        else:
            num_workers = min(self.max_workers, num_active)

        # Ensure at least 1 worker
        num_workers = max(1, num_workers)

        logger.info(
            f"WorkerPool initializing {num_workers} workers "
            f"(max={self.max_workers}, active_accounts={num_active}, tasks={num_tasks})"
        )

        # Create workers
        for i in range(num_workers):
            try:
                worker = await Worker.create(
                    id=f"worker-{i}",
                    pool=self.pool,
                    scroll_threshold=self.scroll_threshold,
                    headless=self.headless,
                    mobile=self.mobile,
                    queue=self.queue_name,
                )
                self.workers.append(worker)

                # Start worker loop as background task
                task = asyncio.create_task(self._worker_loop(worker))
                self.worker_tasks.append(task)

                logger.info(f"WorkerPool created {worker.id}")

            except NoAccountError:
                # No more accounts available
                logger.warning(
                    f"WorkerPool: could only create {len(self.workers)} workers "
                    f"(requested {num_workers})"
                )
                break

        if not self.workers:
            raise NoAccountError("Failed to create any workers - no accounts available")

        self._initialized = True
        return len(self.workers)

    async def _worker_loop(self, worker: Worker):
        """
        Worker loop - processes tasks from queue until shutdown.

        Args:
            worker: Worker instance to execute tasks
        """
        logger.info(f"WorkerPool: {worker.id} loop started")

        while not self._shutdown:
            try:
                # Wait for task with timeout to allow checking shutdown flag
                query, future = await asyncio.wait_for(
                    self.task_queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                # No task available, check shutdown flag and continue
                continue

            logger.info(f"WorkerPool: {worker.id} processing {query.endpoint} - {query.query}")

            try:
                result = await worker.execute_task(query)
                future.set_result(result)
                logger.info(
                    f"WorkerPool: {worker.id} completed {query.endpoint} - "
                    f"{len(result.posts)} posts"
                )
            except Exception as e:
                logger.error(f"WorkerPool: {worker.id} failed {query.endpoint}: {e}")
                future.set_exception(e)
            finally:
                self.task_queue.task_done()

        logger.info(f"WorkerPool: {worker.id} loop exiting")

    async def submit_task(self, query: Query) -> asyncio.Future:
        """
        Submit a scraping task and return a Future for the result.

        Lazy-initializes the WorkerPool on first call.

        Args:
            query: Query object describing the scraping task

        Returns:
            Future that resolves to ScrapingResult when task completes

        Raises:
            NoAccountError: If no accounts available for initialization
        """
        if not self._initialized:
            await self.initialize()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        await self.task_queue.put((query, future))
        logger.debug(f"WorkerPool: submitted task {query.endpoint} - {query.query}")

        return future

    async def close(self):
        """
        Shutdown the WorkerPool gracefully.

        Sets shutdown flag, waits for worker loops to exit, and closes all workers.
        """
        if not self._initialized:
            return

        logger.info("WorkerPool: shutting down...")

        # Signal shutdown
        self._shutdown = True

        # Wait for worker loops to exit
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)

        # Close all workers (release accounts)
        for worker in self.workers:
            await worker.close()

        # Reset state
        self.workers = []
        self.worker_tasks = []
        self._initialized = False
        self._shutdown = False

        logger.info("WorkerPool: shutdown complete")

    async def __aenter__(self) -> "WorkerPool":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Async context manager exit - close pool."""
        await self.close()
        return False
