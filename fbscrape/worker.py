"""
Worker class for managing account lifecycle and executing scraping tasks.

Each task gets a fresh BrowserSession (via context manager), allowing clean
separation between tasks and automatic resource cleanup.
"""

import asyncio
from typing import Callable, Optional

from .accounts_pool import AccountsPool
from .account import Account
from .browser_session import BrowserSession
from .exceptions import (
    AccountBannedError,
    FailedLoginError,
    NoAccountError,
    RateLimitError,
)
from .logger import logger
from .models import Query, ScrapingResult


class Worker:
    """
    Manages account lifecycle and executes scraping tasks.

    Creates a fresh BrowserSession for each task via context manager,
    tracks scroll counts across tasks, and handles account rotation
    when thresholds are reached or errors occur.
    """

    # Maps endpoint names to BrowserSession method names
    ENDPOINT_METHODS = {
        "UserTimeline": "user_timeline",
        # Add more as implemented:
        # "Search": "search",
        # "GroupTimeline": "group_timeline",
    }

    def __init__(
        self,
        id: str,
        pool: AccountsPool,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        stall_timeout_seconds: int = 300,
    ):
        """
        Initialize Worker with configuration only.

        Use Worker.create() factory method or context manager for proper initialization.

        Args:
            id: Worker identifier for logging
            pool: AccountsPool for account management
            scroll_threshold: Scroll count before rotating account
            headless: Run browser in headless mode
            mobile: Use mobile browser emulation
            stall_timeout_seconds: Bail out if no GraphQL response arrives within N seconds
        """
        self.id = id
        self.pool = pool
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.stall_timeout_seconds = stall_timeout_seconds

        # State set during initialize()
        self.current_account: Optional[Account] = None
        self.scroll_count: int = 0
        self._initialized: bool = False

    @classmethod
    async def create(
        cls,
        id: str,
        pool: AccountsPool,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        stall_timeout_seconds: int = 300,
    ) -> "Worker":
        """
        Factory method to create and initialize a Worker.

        Args:
            id: Worker identifier for logging
            pool: AccountsPool for account management
            scroll_threshold: Scroll count before rotating account
            headless: Run browser in headless mode
            mobile: Use mobile browser emulation
            stall_timeout_seconds: Bail out if no GraphQL response arrives within N seconds

        Returns:
            Initialized Worker instance

        Raises:
            NoAccountError: If no account available in pool
        """
        logger.debug(f"Worker.create({id}): creating with scroll_threshold={scroll_threshold}, headless={headless}")
        instance = cls(
            id=id,
            pool=pool,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            stall_timeout_seconds=stall_timeout_seconds,
        )
        success = await instance.initialize()
        if not success:
            raise NoAccountError(f"Worker {id}: no account available")
        return instance

    async def __aenter__(self) -> "Worker":
        """Async context manager entry - initialize worker."""
        if not self._initialized:
            success = await self.initialize()
            if not success:
                raise NoAccountError(f"Worker {self.id}: no account available")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Async context manager exit - release account."""
        await self.close()
        return False  # Don't suppress exceptions

    async def initialize(self) -> bool:
        """
        Initialize worker by acquiring an account from the pool.

        Returns:
            True if account acquired successfully, False otherwise
        """
        logger.debug(f"Worker {self.id}: initializing, requesting account from pool")
        account = await self.pool.get_available()
        if not account:
            logger.warning(f"Worker {self.id}: no account available")
            return False

        self.current_account = account
        self.scroll_count = 0
        self._initialized = True

        logger.info(f"Worker {self.id} initialized with account {self.current_account.display_name}")
        return True

    async def close(self):
        """Release current account back to the pool."""
        logger.debug(f"Worker {self.id}: closing, scroll_count={self.scroll_count}")
        if self.current_account:
            await self.pool.release_account(self.current_account.identifier)
            logger.info(f"Worker {self.id} released account {self.current_account.display_name}")
            self.current_account = None

        self.scroll_count = 0
        self._initialized = False

    async def run(self, task_queue: asyncio.Queue) -> list[ScrapingResult]:
        """
        Process tasks from queue until empty.

        Args:
            task_queue: AsyncIO queue containing Query objects

        Returns:
            List of ScrapingResult objects from completed tasks
        """
        results: list[ScrapingResult] = []

        while True:
            try:
                task: Query = task_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            logger.info(f"Worker {self.id} processing task: {task.endpoint} - {task.query}")

            try:
                result = await self.execute_task(task)
                results.append(result)
                logger.info(
                    f"Worker {self.id} completed task: {task.endpoint} - "
                    f"{len(result.posts)} posts, result='{result.result}'"
                )
            except NoAccountError:
                # No account available for rotation - put task back and stop
                logger.error(f"Worker {self.id}: no account available, stopping")
                await task_queue.put(task)
                break
            except Exception as e:
                logger.error(f"Worker {self.id} unexpected error: {e}")
                # Continue with next task
            finally:
                task_queue.task_done()

        logger.info(f"Worker {self.id} finished, processed {len(results)} tasks")
        return results

    async def execute_task(self, task: Query) -> ScrapingResult:
        """
        Execute a single scraping task.

        Creates a fresh BrowserSession for the task, executes the scraping
        method, and handles errors with account rotation.

        Args:
            task: Query object describing the scraping task

        Returns:
            ScrapingResult from the scraping operation

        Raises:
            NoAccountError: If no account available after rotation attempt
        """
        # Check scroll threshold BEFORE task
        if self.scroll_count >= self.scroll_threshold:
            logger.info(
                f"Worker {self.id} reached scroll threshold ({self.scroll_threshold}), "
                f"rotating account {self.current_account.display_name}"
            )
            await self.rotate_account()

        max_retries = 3
        retry_count = 0

        logger.debug(f"Worker {self.id}: executing task {task.endpoint}, current scroll_count={self.scroll_count}")

        while retry_count < max_retries:
            logger.debug(f"Worker {self.id}: attempt {retry_count + 1}/{max_retries} for {task.endpoint}")
            try:
                # Create fresh BrowserSession for this task
                async with BrowserSession(
                    account=self.current_account,
                    pool=self.pool,
                    headless=self.headless,
                    mobile=self.mobile,
                    stall_timeout_seconds=self.stall_timeout_seconds,
                ) as session:
                    # Get scraping method
                    method = self._get_scraping_method(session, task.endpoint)

                    # Execute scraping
                    result = await method(
                        **task.query
                    )

                    # Update Worker's scroll count from session
                    endpoint_scrolls = await session.get_scroll_count(task.endpoint)
                    self.scroll_count += endpoint_scrolls
                    logger.debug(f"Worker {self.id}: task complete, endpoint_scrolls={endpoint_scrolls}, total scroll_count={self.scroll_count}")

                    return result

            except FailedLoginError as e:
                logger.warning(
                    f"Worker {self.id}: login failed for {self.current_account.display_name}, "
                    f"marking inactive and rotating"
                )
                await self.pool.mark_inactive(
                    self.current_account.identifier, f"Login failed: {e}"
                )
                await self.rotate_account()
                retry_count += 1

            except AccountBannedError as e:
                logger.warning(
                    f"Worker {self.id}: account {self.current_account.display_name} banned, "
                    f"marking inactive and rotating"
                )
                await self.pool.mark_inactive(
                    self.current_account.identifier, f"Account banned: {e}"
                )
                await self.rotate_account()
                retry_count += 1

            except RateLimitError as e:
                logger.warning(
                    f"Worker {self.id}: rate limited on {self.current_account.display_name}: {e}, "
                    f"locking temporarily and rotating"
                )
                await self.pool.lock_until(
                    self.current_account.identifier,
                    "datetime('now', '+1 hour')",
                )
                await self.rotate_account()
                retry_count += 1

        # If we exhausted retries, raise to signal failure
        raise RuntimeError(
            f"Worker {self.id}: failed to execute task after {max_retries} retries"
        )

    async def rotate_account(self):
        """
        Release current account and acquire a new one.

        Adds a brief cooldown lock to prevent immediately re-acquiring the same account.

        Raises:
            NoAccountError: If no account available for rotation
        """
        logger.debug(f"Worker {self.id}: rotating account, current={self.current_account.display_name if self.current_account else 'None'}")
        # Release current account with cooldown to prevent immediate re-acquisition
        if self.current_account:
            await self.pool.lock_until(
                self.current_account.identifier,
                "datetime('now', '+5 minutes')"
            )
            await self.pool.release_account(self.current_account.identifier)
            logger.info(f"Worker {self.id} released account {self.current_account.display_name} (5s cooldown)")
            self.current_account = None

        # Reset state
        self.scroll_count = 0
        self._initialized = False

        # Get new account
        success = await self.initialize()
        if not success:
            raise NoAccountError(f"Worker {self.id}: no account available for rotation")

    def _get_scraping_method(self, session: BrowserSession, endpoint: str) -> Callable:
        """
        Get the BrowserSession method for a given endpoint.

        Args:
            session: BrowserSession instance
            endpoint: Endpoint name (e.g., 'UserTimeline')

        Returns:
            Bound method from BrowserSession

        Raises:
            ValueError: If endpoint is not supported
        """
        if endpoint not in self.ENDPOINT_METHODS:
            raise ValueError(
                f"Unsupported endpoint: {endpoint}. "
                f"Supported endpoints: {list(self.ENDPOINT_METHODS.keys())}"
            )
        method_name = self.ENDPOINT_METHODS[endpoint]
        logger.debug(f"Worker {self.id}: endpoint {endpoint} -> method {method_name}")
        return getattr(session, method_name)
