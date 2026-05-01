"""
WorkerPool for managing concurrent scraping workers.

Manages a pool of Workers that pull tasks from a shared queue and
return results via Futures.
"""

import asyncio

from .accounts_pool import AccountsPool
from .exceptions import NoAccountError
from .logger import logger
from .models import Query
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
        raise_when_no_account: bool = True,
    ):
        """
        Initialize WorkerPool configuration.

        Args:
            pool: AccountsPool for account management
            max_workers: Maximum number of concurrent workers
            scroll_threshold: Scrolls before rotating account
            headless: Run browsers in headless mode
            mobile: Use mobile browser emulation
            raise_when_no_account: If True (default), startup raises
                NoAccountError when the pool is empty/locked. If False, the
                first worker blocks until an account frees up; subsequent
                workers still fail-fast (otherwise we'd deadlock waiting for
                more accounts than the pool can ever supply at once).
        """
        self.pool = pool
        self.max_workers = max_workers
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.raise_when_no_account = raise_when_no_account

        # State
        self.workers: list[Worker] = []
        self.worker_tasks: list[asyncio.Task] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._initialized: bool = False
        self._shutdown: bool = False
        self._init_lock: asyncio.Lock = asyncio.Lock()

    async def initialize(self) -> int:
        """
        Initialize workers based on available accounts.

        Returns:
            Number of workers created

        Raises:
            NoAccountError: If no active accounts available
        """
        if self._initialized:
            logger.debug(f"WorkerPool already initialized with {len(self.workers)} workers")
            return len(self.workers)

        logger.debug(
            f"WorkerPool initializing with config: max_workers={self.max_workers}, "
            f"scroll_threshold={self.scroll_threshold}, headless={self.headless}, "
            f"raise_when_no_account={self.raise_when_no_account}"
        )
        active_accounts = await self.pool.get_active_accounts()
        num_active = len(active_accounts)

        if num_active == 0 and self.raise_when_no_account:
            raise NoAccountError("No active accounts available in pool")

        # In wait mode with 0 active accounts, give the first worker a shot —
        # get_available_or_wait() will return None right away (nothing to wait
        # for) and we'll hit the post-loop NoAccountError below.
        num_workers = max(1, min(self.max_workers, num_active)) if num_active > 0 else self.max_workers

        logger.info(
            f"WorkerPool initializing up to {num_workers} workers "
            f"(max={self.max_workers}, active_accounts={num_active}, "
            f"raise_when_no_account={self.raise_when_no_account})"
        )

        # Create workers. Every worker gets the user's persistent
        # raise_when_no_account flag (so rotations honor wait mode). At
        # STARTUP only, the FIRST worker uses the user's flag; subsequent
        # workers always fail-fast via raise_at_startup=True so we don't
        # block forever waiting for more accounts than the pool has free
        # right now. After startup, all workers behave identically.
        for i in range(num_workers):
            startup_raise = True if i > 0 else self.raise_when_no_account
            try:
                worker = await Worker.create(
                    id=f"worker-{i}",
                    pool=self.pool,
                    scroll_threshold=self.scroll_threshold,
                    headless=self.headless,
                    mobile=self.mobile,
                    raise_when_no_account=self.raise_when_no_account,
                    raise_at_startup=startup_raise,
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
        async with self._init_lock:
            if not self._initialized:
                logger.debug("WorkerPool: first task submission, initializing...")
                await self.initialize()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        await self.task_queue.put((query, future))
        logger.debug(f"WorkerPool: submitted task {query.endpoint} - {query.query}, queue_size={self.task_queue.qsize()}")

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
        logger.debug(f"WorkerPool: closing {len(self.workers)} workers")
        for worker in self.workers:
            logger.debug(f"WorkerPool: closing {worker.id}")
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
