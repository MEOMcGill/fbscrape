"""Login rotations get their own budget, separate from the scrape retry budget.

A `TransientLoginError` is one account declining to let us in. It says nothing
about the task, so spending a scrape retry on it is the wrong accounting --
and it is the wrong accounting in a way that compounds, because
`AccountsPool._order_by` is `scroll_count_overall_24h ASC`: an account that
never completes a login never scrolls, so it keeps the lowest count in the pool
and keeps getting picked FIRST. The weakest accounts therefore sit at the head
of the queue, and a shared budget of 3 can be exhausted before any working
account is reached. Observed 2026-11-16: a group chain died at leg7 after three
consecutive login timeouts on the three lowest-scroll accounts, with four
healthy accounts untouched behind them.

Asserted invariants:
- Login rotations do not consume `max_retries`, so more than 3 bad accounts in
  a row can still be walked past.
- The login budget is finite: `MAX_LOGIN_RETRIES` rotations raise
  `RetryBudgetExhaustedError`, naming the login budget rather than the scrape one.
- The two budgets are independent: login rotations leave the scrape budget whole.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import fbscrape.worker as worker_mod
from fbscrape.exceptions import RetryBudgetExhaustedError, TransientLoginError
from fbscrape.models import Query, ScrapeOutcome
from fbscrape.worker import MAX_LOGIN_RETRIES, Worker


class _Acct:
    def __init__(self, ident):
        self.identifier = ident
        self.display_name = ident


class _FakePool:
    """Hands out a fresh account each rotation, as the real pool does.

    `record_transient_login_failure` returns 1 every time: each rotation lands
    on a DIFFERENT account, so no account accumulates a consecutive streak and
    none is retired. That is exactly the real-world case this budget is for.
    """

    def __init__(self):
        self.accounts = [_Acct(f"acct{i}@example.com") for i in range(12)]
        self._next = 0
        self.released = []
        self.marked_inactive = []
        self.order_bys = []

    async def get_available(self, order_by=None):
        self.order_bys.append(order_by)
        acct = self.accounts[self._next % len(self.accounts)]
        self._next += 1
        return acct

    async def get_available_or_wait(self, order_by=None):
        return await self.get_available(order_by=order_by)

    async def release_account(self, identifier):
        self.released.append(identifier)

    async def lock_until(self, identifier, until, error_msg=None):
        pass

    async def mark_inactive(self, identifier, error_msg=None):
        self.marked_inactive.append(identifier)

    async def record_transient_login_failure(self, identifier):
        return 1

    async def clear_transient_login_failures(self, identifier):
        pass

    async def record_login_success(self, identifier):
        pass


def _outcome(result, data=None):
    return ScrapeOutcome(
        result=result,
        data=data,
        time_started=datetime.now(timezone.utc),
        time_taken=timedelta(seconds=1),
    )


def _session_factory(log, script):
    """Session stand-in. `script` is a list of "login_fail" / ScrapeOutcome."""

    class _FakeSession:
        def __init__(self, account=None, **kwargs):
            self.account = account
            self.scrolls_recorded = 0
            self.requests_sent = 0

        async def __aenter__(self):
            step = script[0] if script else _outcome("success", [])
            if step == "login_fail":
                script.pop(0)
                log.append(("login_fail", self.account.identifier))
                raise TransientLoginError(
                    f"Login navigation flaked (transient) for {self.account.identifier}: "
                    "Page.goto: Timeout 30000ms exceeded."
                )
            log.append(("open", self.account.identifier))
            return self

        async def __aexit__(self, *exc):
            return False

        async def _run(self, **kwargs):
            log.append(("task", self.account.identifier))
            self.requests_sent += 1
            return script.pop(0) if script else _outcome("success", [])

        user_timeline_hybrid = _run

    return _FakeSession


def _query():
    return Query(endpoint="UserTimeline", mode="hybrid", query={"handle": "h"}, params={})


def _make_worker(monkeypatch, log, script):
    pool = _FakePool()
    monkeypatch.setattr(worker_mod, "BrowserSession", _session_factory(log, script))
    worker = Worker(id="w0", pool=pool)
    worker.current_account = _Acct("acct0@example.com")
    return worker, pool


def test_four_bad_logins_do_not_exhaust_the_scrape_budget(monkeypatch):
    """The exact leg7 shape: more consecutive bad accounts than max_retries."""
    log = []
    script = ["login_fail"] * 4 + [_outcome("success", [{"post_id": "1"}])]
    worker, pool = _make_worker(monkeypatch, log, script)

    result = asyncio.run(worker.execute_task(_query()))

    assert result.result == "success"
    assert len(result.data) == 1
    # Four accounts refused, the fifth worked -- unreachable on a budget of 3.
    assert [e[0] for e in log] == ["login_fail"] * 4 + ["open", "task"]
    # None of them was retired: a single failure is not evidence against an account.
    assert pool.marked_inactive == []


def test_login_budget_is_finite_and_names_itself(monkeypatch):
    """A pool where nothing logs in must still fail, and say why."""
    log = []
    script = ["login_fail"] * (MAX_LOGIN_RETRIES + 3)
    worker, _ = _make_worker(monkeypatch, log, script)

    with pytest.raises(RetryBudgetExhaustedError) as exc:
        asyncio.run(worker.execute_task(_query()))

    assert "login rotations" in str(exc.value)
    assert "no account completed a login" in str(exc.value)
    assert len(log) == MAX_LOGIN_RETRIES


def test_login_rotations_leave_the_scrape_budget_whole(monkeypatch):
    """Budgets are independent: bad logins must not eat the scrape's retries."""
    log = []
    # Two accounts refuse, then the scrape itself flakes twice before working.
    script = [
        "login_fail",
        "login_fail",
        _outcome("template_capture_timeout"),
        _outcome("template_capture_timeout"),
        _outcome("success", [{"post_id": "1"}]),
    ]
    worker, _ = _make_worker(monkeypatch, log, script)

    result = asyncio.run(worker.execute_task(_query()))

    assert result.result == "success"
    assert sum(1 for e in log if e[0] == "login_fail") == 2
    assert sum(1 for e in log if e[0] == "task") == 3


def test_login_attempts_escalate_from_rest_to_reliability(monkeypatch):
    """Tier order: rested accounts get real chances, then reliability takes over.

    The pool's default ordering is by rest (`scroll_count_overall_24h ASC`).
    That is the right default and the wrong response to repeated login failure,
    because an account that never logs in never scrolls and so keeps the lowest
    count -- it is served first forever. Escalating only AFTER the rested tier
    is spent keeps both properties: a healthy account that had a bad night is
    still tried on merit, and a chain is not held hostage by accounts that have
    never once completed a login.
    """
    log = []
    script = ["login_fail"] * 5
    worker, pool = _make_worker(monkeypatch, log, script)

    with pytest.raises(RetryBudgetExhaustedError):
        asyncio.run(worker.execute_task(_query()))

    # One rotation per failure; the ordering asked for escalates.
    assert pool.order_bys == [
        None,                                 # after failure 1 -> attempt 2: rested
        None,                                 # after failure 2 -> attempt 3: rested
        Worker.LOGIN_ORDER_BY_RELIABLE,       # after failure 3 -> attempt 4
        Worker.LOGIN_ORDER_BY_BEST_HISTORY,   # after failure 4 -> attempt 5
        Worker.LOGIN_ORDER_BY_BEST_HISTORY,   # budget ends; ordering stays pinned
    ]


def test_login_order_by_tiers_are_pure():
    """The tier choice is a function of failures so far, nothing else."""
    w = Worker.__new__(Worker)
    assert w._login_order_by(0) is None
    assert w._login_order_by(1) is None
    assert w._login_order_by(2) is None
    assert w._login_order_by(3) == Worker.LOGIN_ORDER_BY_RELIABLE
    assert w._login_order_by(4) == Worker.LOGIN_ORDER_BY_BEST_HISTORY
    assert w._login_order_by(99) == Worker.LOGIN_ORDER_BY_BEST_HISTORY
    # Rested tier is exactly the first three attempts.
    assert Worker.LOGIN_TIER_RESTED_ATTEMPTS == 3
    # Tier 5 must not mention scroll count at all -- that is what makes it
    # reachable for a heavily used but reliable account.
    assert "scroll_count" not in Worker.LOGIN_ORDER_BY_BEST_HISTORY
