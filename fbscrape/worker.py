"""
Worker class for managing account lifecycle and executing scraping tasks.

By default each task gets a fresh BrowserSession (via context manager),
allowing clean separation between tasks and automatic resource cleanup.
Setting `tasks_per_session > 1` reuses one session (browser process + login)
across that many consecutive tasks before tearing it down — see
`Worker._task_session`.
"""

import asyncio
from contextlib import asynccontextmanager
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

    Creates a BrowserSession via context manager (a fresh one per task by
    default, or one shared across `tasks_per_session` consecutive tasks),
    tracks scroll counts across tasks, and handles account rotation
    when thresholds are reached or errors occur.
    """

    # Maps (endpoint, mode) -> BrowserSession method name. Endpoints describe
    # *what* to scrape (UserTimeline, GroupTimeline, ...); modes describe *how*
    # (hybrid = page.request driven, api = pure replay).
    # Allowed (endpoint, mode) pairs and their params live in Query.ENDPOINT_REGISTRY.
    ENDPOINT_MODE_METHODS = {
        ("UserTimeline", "hybrid"): "user_timeline_hybrid",
        ("Search", "hybrid"): "search_hybrid",
        ("GroupTimeline", "hybrid"): "group_timeline_hybrid",
        ("CommentsList", "hybrid"): "comments_list_hybrid",
        ("PageTransparency", "hybrid"): "page_transparency_hybrid",
        ("ProfileAuthenticity", "hybrid"): "profile_authenticity_hybrid",
        ("PostDetail", "hybrid"): "post_detail_hybrid",
        ("ProfileInfo", "hybrid"): "profile_info_hybrid",
        ("ProfileAbout", "hybrid"): "profile_about_hybrid",
        ("GroupInfo", "hybrid"): "group_info_hybrid",
        ("GroupAbout", "hybrid"): "group_about_hybrid",
        # ("UserTimeline", "api"): "user_timeline_api",  -- future
    }

    # These endpoints never scroll, so scroll_count-based rotation below never
    # fires for them — rotate on the session boundary instead. With the default
    # `tasks_per_session=1` that boundary is every task (unconditional
    # rotation, the original behavior); with N>1 they rotate every N tasks.
    ALWAYS_ROTATE_ENDPOINTS = frozenset({
        "ProfileInfo", "ProfileAbout", "GroupInfo", "GroupAbout",
    })

    # Default account selection (scroll_count_overall_24h ASC) is meaningless
    # for the endpoints above; least-recently-used first, with scroll count
    # only as a tie-breaker.
    LAST_USED_ORDER_BY = "last_used ASC, scroll_count_overall_24h ASC"

    def __init__(
        self,
        id: str,
        pool: AccountsPool,
        scroll_threshold: int = 500,
        headless: bool = False,
        mobile: bool = False,
        raise_when_no_account: bool = True,
        tasks_per_session: int = 1,
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
            tasks_per_session: How many consecutive tasks reuse one
                BrowserSession before it is torn down. 1 (default) reproduces
                the original behavior exactly — a fresh browser + login per
                task. Higher values amortize that startup cost across tasks,
                which matters most for short single-shot endpoints
                (ProfileInfo, ProfileAbout, ...) where launch + login dominates
                the run time. The account is unchanged across the reused
                session; account rotation still follows `scroll_threshold`
                and the error paths.
        """
        self.id = id
        self.pool = pool
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.mobile = mobile
        self.raise_when_no_account = raise_when_no_account
        if tasks_per_session < 1:
            raise ValueError(
                f"tasks_per_session must be >= 1, got {tasks_per_session}"
            )
        self.tasks_per_session = tasks_per_session

        # State set during initialize()
        self.current_account: Optional[Account] = None
        self.scroll_count: int = 0
        self._initialized: bool = False

        # Reused BrowserSession (see `_task_session`). None means "no live
        # session" — the next task opens one. `tasks_on_session` counts tasks
        # STARTED on the current session, incremented at acquire time so the
        # in-task rotation checks can see it.
        self.session: Optional[BrowserSession] = None
        self.tasks_on_session: int = 0
        # `session.scrolls_recorded` is cumulative over a reused session, so
        # snapshot it at task start to recover the per-task delta.
        self._scrolls_at_task_start: int = 0

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
        tasks_per_session: int = 1,
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
            tasks_per_session: see Worker.__init__.

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
            tasks_per_session=tasks_per_session,
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

    async def initialize(
        self, raise_override: bool | None = None, order_by: str | None = None,
    ) -> bool:
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
            order_by: passed through to `AccountsPool.get_available[_or_wait]`
                — see `ALWAYS_ROTATE_ENDPOINTS`.

        Returns:
            True if account acquired successfully, False otherwise
        """
        flag = self.raise_when_no_account if raise_override is None else raise_override
        logger.debug(
            f"Worker {self.id}: initializing, requesting account from pool "
            f"(raise_when_no_account={flag}, persistent={self.raise_when_no_account})"
        )
        if flag:
            account = await self.pool.get_available(order_by=order_by)
        else:
            account = await self.pool.get_available_or_wait(order_by=order_by)
        if not account:
            logger.warning(f"Worker {self.id}: no account available")
            return False

        self.current_account = account
        self.scroll_count = 0
        self._initialized = True

        logger.info(f"Worker {self.id} initialized with account {self.current_account.display_name}")
        return True

    async def close(self):
        """Close any live BrowserSession and release the account to the pool."""
        logger.debug(f"Worker {self.id}: closing, scroll_count={self.scroll_count}")
        await self._close_session()
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
                # Session for this task: a fresh BrowserSession, or the
                # worker's live one when `tasks_per_session > 1`.
                async with self._task_session() as session:
                    method = self._get_scraping_method(session, task.endpoint, task.mode)
                    # Query.params is fully populated with registry defaults at
                    # Query construction, so a single spread covers everything
                    # the BrowserSession method expects. The method returns a
                    # ScrapeOutcome (Query-agnostic); we attach the canonical
                    # `task` here so the rebuild that used to happen inside
                    # BrowserSession is gone — the Query is constructed exactly
                    # once, in scraper.user_timeline.
                    #
                    # `runtime_options` carries the non-serializable per-call
                    # extras (streaming media sinks / on_new_posts callback);
                    # they spread as kwargs like params but never reach the
                    # saved Query. On a retry the fresh session re-installs
                    # them, so a rotated account keeps streaming media.
                    outcome = await method(
                        **task.query, **task.params, **(task.runtime_options or {})
                    )

                    # Accumulate scrolls performed by THIS TASK. A session may
                    # be reused across tasks, so `scrolls_recorded` is
                    # cumulative — subtract the snapshot taken when the task
                    # acquired the session. `self.scroll_count` tracks
                    # the running total across every session this worker spun
                    # up while owning the current account; rotation zeroes it.
                    # DB scroll columns keep updating via `record_scroll` for
                    # account selection / prioritization, but are NOT consulted
                    # here — they're cumulative-lifetime and would over-count.
                    session_scrolls = (
                        session.scrolls_recorded - self._scrolls_at_task_start
                    )
                    self.scroll_count += session_scrolls
                    logger.debug(f"Worker {self.id}: task complete, session_scrolls={session_scrolls}, total scroll_count={self.scroll_count}")

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

                    # Rotate on the session boundary. `tasks_on_session` was
                    # incremented when this task acquired the session, so with
                    # the default tasks_per_session=1 this fires on every task
                    # — identical to the original unconditional rotation.
                    if (
                        task.endpoint in self.ALWAYS_ROTATE_ENDPOINTS
                        and self.tasks_on_session >= self.tasks_per_session
                    ):
                        await self.rotate_account(order_by=self.LAST_USED_ORDER_BY)

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
                # on a fresh BrowserSession (`_task_session` closes the wedged
                # session on the way out — including a reused one — so the next
                # iteration opens a new one). Discard partial posts.
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

            except Exception as e:
                # Anything not covered above (e.g. a raw Playwright/Camoufox
                # driver crash) would otherwise escape execute_task entirely
                # without ever rotating — leaving this account stuck for the
                # worker's next task. Only ALWAYS_ROTATE_ENDPOINTS get this;
                # everything else keeps its prior behavior (propagate as-is).
                if task.endpoint not in self.ALWAYS_ROTATE_ENDPOINTS:
                    raise
                logger.warning(
                    f"Worker {self.id}: unexpected error on "
                    f"{self.current_account.display_name if self.current_account else 'None'} "
                    f"for {task.endpoint}: {e!r} — rotating and retrying"
                )
                await self.rotate_account(order_by=self.LAST_USED_ORDER_BY)
                retry_count += 1

        # If we exhausted retries, raise to signal failure
        raise RetryBudgetExhaustedError(
            f"Worker {self.id}: failed to execute task after {max_retries} retries"
        )

    async def rotate_account(
        self,
        lock_until: str | None = None,
        error_msg: str | None = None,
        order_by: str | None = None,
    ):
        """
        Release current account and acquire a new one.

        Adds a brief cooldown lock to prevent immediately re-acquiring the
        same account. When `error_msg` is provided, it's written to the
        account's `error_msg` column alongside the lock so post-hoc DB
        inspection can explain *why* the account was locked (the lock
        itself expires; the error_msg persists).

        Args:
            order_by: passed through to `initialize()` — see
                `ALWAYS_ROTATE_ENDPOINTS`.

        Raises:
            NoAccountError: If no account available for rotation
        """
        logger.debug(f"Worker {self.id}: rotating account, current={self.current_account.display_name if self.current_account else 'None'}")
        # A BrowserSession is bound to the account it was constructed with, so
        # any live session must die with the account. Doing it here — rather
        # than at each of the ~8 call sites — is what keeps session reuse from
        # leaking a session logged into the previous account.
        await self._close_session()
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
        success = await self.initialize(order_by=order_by)
        if not success:
            raise NoAccountError(f"Worker {self.id}: no account available for rotation")

    @asynccontextmanager
    async def _task_session(self):
        """Yield the BrowserSession for one task, honoring `tasks_per_session`.

        Replaces the per-task `async with BrowserSession(...)`. The session is
        opened on demand and kept on the worker; it is torn down when

          - the task raises (every error path in `execute_task` rotates or
            retries, and both assume a clean browser), or
          - `tasks_per_session` tasks have run on it, or
          - `rotate_account()` / `close()` drop it.

        With `tasks_per_session=1` a session is opened and closed around every
        task, which is exactly the original behavior.
        """
        session = await self._acquire_session()
        try:
            yield session
        except BaseException:
            # Don't hand a wedged/errored browser to the retry — the handlers
            # in execute_task all expect to restart on a clean one.
            await self._close_session()
            raise
        else:
            if self.tasks_on_session >= self.tasks_per_session:
                await self._close_session()

    async def _acquire_session(self) -> BrowserSession:
        """Return a live BrowserSession for the current account, opening one
        if needed, and count this task against it."""
        # Defensive: a session outliving its account would scrape as the wrong
        # user. rotate_account() already closes it; this covers any other path
        # that swaps current_account.
        if (
            self.session is not None
            and self.current_account is not None
            and self.session.account.identifier != self.current_account.identifier
        ):
            logger.warning(
                f"Worker {self.id}: live session belongs to "
                f"{self.session.account.display_name} but current account is "
                f"{self.current_account.display_name}; closing it"
            )
            await self._close_session()

        if self.session is None:
            session = BrowserSession(
                account=self.current_account,
                pool=self.pool,
                headless=self.headless,
                mobile=self.mobile,
            )
            try:
                # Drive the async-context-manager protocol rather than calling
                # initialize()/close() directly: that's the exact contract the
                # per-task `async with BrowserSession(...)` had, so anything
                # standing in for a BrowserSession keeps working unchanged.
                await session.__aenter__()
            except BaseException:
                # __aenter__ already cleans up its own partial state before
                # re-raising; just make sure we don't retain a dead session.
                self.session = None
                self.tasks_on_session = 0
                raise
            self.session = session
            self.tasks_on_session = 0
            logger.debug(
                f"Worker {self.id}: opened BrowserSession for "
                f"{self.current_account.display_name} "
                f"(tasks_per_session={self.tasks_per_session})"
            )

        self.tasks_on_session += 1
        self._scrolls_at_task_start = self.session.scrolls_recorded
        logger.debug(
            f"Worker {self.id}: task {self.tasks_on_session}/"
            f"{self.tasks_per_session} on this session"
        )
        return self.session

    async def _close_session(self):
        """Tear down the live BrowserSession, if any. Idempotent."""
        if self.session is None:
            return
        session, self.session = self.session, None
        self.tasks_on_session = 0
        self._scrolls_at_task_start = 0
        try:
            await session.__aexit__(None, None, None)
        except Exception as e:
            # A browser that won't close cleanly must not take down the task
            # or the rotation that asked for the teardown.
            logger.warning(f"Worker {self.id}: error closing BrowserSession: {e!r}")

    def _get_scraping_method(self, session: BrowserSession, endpoint: str, mode: str) -> Callable:
        """
        Get the BrowserSession method for a given (endpoint, mode) pair.

        Args:
            session: BrowserSession instance
            endpoint: Endpoint name (e.g., 'UserTimeline')
            mode: Mode name (e.g., 'hybrid')

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
