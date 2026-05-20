"""
Worker class for managing account lifecycle and executing scraping tasks.

Each task gets a fresh BrowserSession (via context manager), allowing clean
separation between tasks and automatic resource cleanup.
"""

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

from .accounts_pool import AccountsPool
from .account import Account
from .browser_session import BrowserSession
from .exceptions import (
    AccountBannedError,
    AccountDisabledError,
    AutomationCheckpointError,
    CheckpointError,
    FailedLoginError,
    NoAccountError,
    RateLimitError,
    RendererHangError,
    RetryBudgetExhaustedError,
    TransientLoginError,
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

    # Maps (endpoint, mode) -> BrowserSession method name. Endpoints describe
    # *what* to scrape (UserTimeline, GroupTimeline, ...); modes describe *how*
    # (manual = scroll-driven, hybrid = page.request driven, api = pure replay).
    # Allowed (endpoint, mode) pairs and their params live in Query.ENDPOINT_REGISTRY.
    ENDPOINT_MODE_METHODS = {
        ("UserTimeline", "manual"): "user_timeline_manual",
        ("UserTimeline", "hybrid"): "user_timeline_hybrid",
        ("Search", "hybrid"): "search_hybrid",
        ("GroupTimeline", "hybrid"): "group_timeline_hybrid",
        ("PageTransparency", "hybrid"): "page_transparency_hybrid",
        ("ProfileAuthenticity", "hybrid"): "profile_authenticity_hybrid",
        # ("UserTimeline", "api"): "user_timeline_api",  -- future
    }

    def __init__(
        self,
        id: str,
        pool: AccountsPool,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        raise_when_no_account: bool = True,
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
            raise_when_no_account: If True (default), `initialize()` uses
                `get_available()` and returns False on empty pool so callers
                raise NoAccountError. If False, `initialize()` uses
                `get_available_or_wait()` and blocks (polling every 5s) until
                an account frees up; only returns False when the pool has zero
                active accounts (everything banned/inactive).
        """
        self.id = id
        self.pool = pool
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.raise_when_no_account = raise_when_no_account

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
        raise_when_no_account: bool = True,
        raise_at_startup: bool | None = None,
    ) -> "Worker":
        """
        Factory method to create and initialize a Worker.

        Args:
            id: Worker identifier for logging
            pool: AccountsPool for account management
            scroll_threshold: Scroll count before rotating account
            headless: Run browser in headless mode
            mobile: Use mobile browser emulation
            raise_when_no_account: persistent flag — see Worker.__init__. Used
                for *future* initialize() calls (e.g. during rotation).
            raise_at_startup: one-shot override for THIS create's initialize()
                call only. Defaults to `raise_when_no_account`. Used by
                WorkerPool to fail-fast on extra workers at startup while
                still letting the persistent flag honor user wait preference
                during rotations.

        Returns:
            Initialized Worker instance

        Raises:
            NoAccountError: If no account available in pool
        """
        logger.debug(
            f"Worker.create({id}): creating with scroll_threshold={scroll_threshold}, "
            f"headless={headless}, raise_when_no_account={raise_when_no_account}, "
            f"raise_at_startup={raise_at_startup}"
        )
        instance = cls(
            id=id,
            pool=pool,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=raise_when_no_account,
        )
        success = await instance.initialize(raise_override=raise_at_startup)
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

    async def initialize(self, raise_override: bool | None = None) -> bool:
        """
        Initialize worker by acquiring an account from the pool.

        With `raise_when_no_account=False`, blocks (polling every 5s) until an
        account frees up. Returns False only when the pool has zero active
        accounts (everything banned) — that's not a transient state.

        Args:
            raise_override: one-shot override for `self.raise_when_no_account`.
                If None (default), use the persistent flag. WorkerPool passes
                True at startup for extra workers so they fail-fast even when
                the user has globally requested wait mode (rotations still
                wait, since they call initialize() with no override).

        Returns:
            True if account acquired successfully, False otherwise
        """
        flag = self.raise_when_no_account if raise_override is None else raise_override
        logger.debug(
            f"Worker {self.id}: initializing, requesting account from pool "
            f"(raise_when_no_account={flag}, persistent={self.raise_when_no_account})"
        )
        if flag:
            account = await self.pool.get_available()
        else:
            account = await self.pool.get_available_or_wait()
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
        # A previous task's rotate_account() may have raised NoAccountError (pool
        # empty at that moment) and left current_account = None. Recover by
        # acquiring an account here. Use get_available_or_wait so we BLOCK while
        # accounts exist but are merely locked (cooldown / rate-limit), and
        # FAIL FAST only when there are no active accounts at all (everything
        # banned or checkpointed — no point waiting in that case).
        if self.current_account is None:
            logger.warning(
                f"Worker {self.id}: no current account; waiting for one to become available"
            )
            account = await self.pool.get_available_or_wait()
            if account is None:
                # No active accounts in the pool (all banned/checkpointed).
                raise NoAccountError(
                    f"Worker {self.id}: no active accounts available for task"
                )
            self.current_account = account
            self.scroll_count = 0
            self._initialized = True
            logger.info(
                f"Worker {self.id} resumed with account {self.current_account.display_name}"
            )

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
                ) as session:
                    method = self._get_scraping_method(session, task.endpoint, task.mode)
                    # Query.params is fully populated with registry defaults at
                    # Query construction, so a single spread covers everything
                    # the BrowserSession method expects. The method returns a
                    # ScrapeOutcome (Query-agnostic); we attach the canonical
                    # `task` here so the rebuild that used to happen inside
                    # BrowserSession is gone — the Query is constructed exactly
                    # once, in scraper.user_timeline.
                    outcome = await method(**task.query, **task.params)

                    # Update Worker's scroll count from session
                    endpoint_scrolls = await session.get_scroll_count(task.endpoint)
                    self.scroll_count += endpoint_scrolls
                    logger.debug(f"Worker {self.id}: task complete, endpoint_scrolls={endpoint_scrolls}, total scroll_count={self.scroll_count}")

                    result = ScrapingResult.from_outcome(task, outcome)

                    # Cursor reset is a soft signal that this account/session
                    # has been throttled into a degraded response stream.
                    # Mirrors the RateLimitError path (lock + rotate) but as
                    # a result-string branch since the loop returned cleanly
                    # with partial posts. High-level scraper owns the resume
                    # retry policy — Worker just locks and yields control.
                    if outcome.result == 'cursor_reset':
                        logger.warning(
                            f"Worker {self.id}: cursor_reset on "
                            f"{self.current_account.display_name} "
                            f"(records={len(outcome.data)}); locking 30 min and rotating"
                        )
                        await self.rotate_account(
                            lock_until="datetime('now', '+10 minutes')"
                        )

                    # In-body GraphQL rate-limit (FB code 1675004 / "Rate
                    # limit exceeded"). FB throttles at the account level —
                    # verified manually that the same error fires for a
                    # human in a browser using the same account. Lock the
                    # account 24h + rotate; partial data is preserved on the
                    # ScrapeOutcome. Mirrors the HTTP-429 RateLimitError path
                    # but as a result-string branch since the loop returned
                    # cleanly with partial posts rather than raising.
                    if outcome.result == 'rate_limit':
                        ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
                        rl_msg = (
                            f"Rate limited at {ts}: in-body GraphQL error "
                            f"(FB code 1675004 / 'Rate limit exceeded'); locked 24h"
                        )
                        logger.warning(
                            f"Worker {self.id}: rate_limit (in-body GraphQL) on "
                            f"{self.current_account.display_name} "
                            f"(records={len(outcome.data)}); locking account 24h and rotating"
                        )
                        await self.rotate_account(
                            lock_until="datetime('now', '+24 hours')",
                            error_msg=rl_msg,
                        )

                    # Response-shape error: the hybrid loop saw a response shape
                    # it doesn't know how to read (all posts in a batch had a
                    # metadata-strategy typename outside _METADATA_TIMESTAMP_TYPENAMES).
                    # Structural bug, not instance-specific — do NOT mark the
                    # account inactive, do NOT rotate, do NOT burn a retry slot.
                    # The next account would hit the same shape. Returning the
                    # partial result terminates the multi-leg loop in
                    # FacebookScraper.user_timeline naturally (it only resumes
                    # on `cursor_reset`).
                    if outcome.result == 'response_shape_error':
                        logger.error(
                            f"Worker {self.id}: response_shape_error on "
                            f"{self.current_account.display_name} "
                            f"(records={len(outcome.data)}) — structural bug, "
                            f"not instance-specific, not retrying. "
                            f"Returning partial result."
                        )

                    return result

            except AccountDisabledError as e:
                # Detector (_wait_for_log_in_outcome) already wrote a specific error_msg
                # and marked the account inactive. Rotate to a fresh account but do NOT
                # increment retry_count — a dead account shouldn't burn our retry budget.
                logger.error(
                    f"Worker {self.id}: account {self.current_account.display_name} is "
                    f"permanently disabled (url={e.url}); rotating without counting as retry"
                )
                await self.rotate_account()

            except AutomationCheckpointError as e:
                # FB flagged the account as suspected automation. Distinct from
                # generic CheckpointError: re-trying soon is pointless and likely
                # accelerates account loss, but the account is recoverable in
                # principle, so we lock 24h (active=True) instead of marking
                # inactive. Mirrors the RateLimitError handling.
                # MUST be ordered BEFORE the generic CheckpointError clause
                # since AutomationCheckpointError is a subclass.
                ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
                am_msg = f"automation suspected at {ts} ({e.url}); locked 24h"
                logger.warning(
                    f"Worker {self.id}: automation-suspected checkpoint on "
                    f"{self.current_account.display_name} (url={e.url}); "
                    f"locking account 24h and rotating"
                )
                await self.rotate_account(
                    lock_until="datetime('now', '+24 hours')",
                    error_msg=am_msg,
                )
                retry_count += 1

            except CheckpointError as e:
                # Detector already wrote error_msg + marked inactive. Needs manual action
                # to recover; try a different account for this task.
                logger.warning(
                    f"Worker {self.id}: checkpoint challenge on {self.current_account.display_name} "
                    f"(url={e.url}); rotating"
                )
                await self.rotate_account()
                retry_count += 1

            except TransientLoginError as e:
                # Unexpected error inside login() that both internal attempts couldn't recover.
                # Probably a transient playwright / page issue — do NOT mark this account
                # inactive. Rotate to a different account and count as a retry.
                logger.warning(
                    f"Worker {self.id}: transient login error on {self.current_account.display_name}: "
                    f"{e} — rotating (account stays active)"
                )
                await self.rotate_account()
                retry_count += 1

            except RendererHangError as e:
                # Browser is wedged; account is fine. Restart with the SAME account
                # on a fresh BrowserSession (the `async with BrowserSession(...)`
                # block exits and the next iteration opens a new one). Discard
                # partial posts.
                # TODO: progress save / resume — preserve pre-hang records so a
                # restart picks up where the wedged session left off instead of
                # from scratch.
                logger.warning(
                    f"Worker {self.id}: renderer hang on {self.current_account.display_name}: "
                    f"{e} — restarting task with same account (no rotation, partial discarded)"
                )
                retry_count += 1

            except FailedLoginError as e:
                # Generic login failure — detector may not have written DB (e.g. form
                # submit silently failed), so mark inactive here as the safety net.
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
                ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
                rl_msg = f"Rate limited at {ts}: HTTP 429 ({e}); locked 24h"
                logger.warning(
                    f"Worker {self.id}: rate limited (HTTP 429) on "
                    f"{self.current_account.display_name}: {e}; "
                    f"locking account 24h and rotating"
                )
                await self.rotate_account(
                    lock_until="datetime('now', '+24 hours')",
                    error_msg=rl_msg,
                )
                retry_count += 1

        # If we exhausted retries, raise to signal failure
        raise RetryBudgetExhaustedError(
            f"Worker {self.id}: failed to execute task after {max_retries} retries"
        )

    async def rotate_account(
        self,
        lock_until: str | None = None,
        error_msg: str | None = None,
    ):
        """
        Release current account and acquire a new one.

        Adds a brief cooldown lock to prevent immediately re-acquiring the
        same account. When `error_msg` is provided, it's written to the
        account's `error_msg` column alongside the lock so post-hoc DB
        inspection can explain *why* the account was locked (the lock
        itself expires; the error_msg persists).

        Raises:
            NoAccountError: If no account available for rotation
        """
        logger.debug(f"Worker {self.id}: rotating account, current={self.current_account.display_name if self.current_account else 'None'}")
        # Release the current account with cooldown to prevent immediate re-acquisition
        if self.current_account:
            await self.pool.lock_until(
                self.current_account.identifier,
                "datetime('now', '+2 minutes')" if lock_until is None else lock_until,
                error_msg=error_msg,
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

    def _get_scraping_method(self, session: BrowserSession, endpoint: str, mode: str) -> Callable:
        """
        Get the BrowserSession method for a given (endpoint, mode) pair.

        Args:
            session: BrowserSession instance
            endpoint: Endpoint name (e.g., 'UserTimeline')
            mode: Mode name (e.g., 'manual', 'hybrid')

        Returns:
            Bound method from BrowserSession

        Raises:
            ValueError: If (endpoint, mode) is not supported
        """
        key = (endpoint, mode)
        if key not in self.ENDPOINT_MODE_METHODS:
            raise ValueError(
                f"Unsupported (endpoint, mode): {key}. "
                f"Supported: {list(self.ENDPOINT_MODE_METHODS.keys())}"
            )
        method_name = self.ENDPOINT_MODE_METHODS[key]
        logger.debug(f"Worker {self.id}: ({endpoint}, {mode}) -> method {method_name}")
        return getattr(session, method_name)
