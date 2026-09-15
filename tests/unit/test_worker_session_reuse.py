"""BrowserSession reuse under a request budget (`Worker.requests_per_session`).

Historically `Worker.execute_task` opened a fresh `BrowserSession` — a browser
process launch plus a login — inside its retry loop, for every task. That cost
is amortized on a long timeline scrape but dominates short single-shot
endpoints (ProfileInfo, ProfileAbout, GroupInfo, GroupAbout), where the actual
work is one navigation.

The scrape methods were already re-entrant on one session (each opens with
`response_interceptor.flush()`, and `_install_stream_hook` assigns rather than
appends), so reuse is a Worker-level policy change only.

The budget is counted in REQUESTS, not tasks: `BrowserSession.requests_sent`
counts scrape navigations plus replay POSTs, so a timeline scrape that
paginates 500 times and a profile fetch that does one navigation are not both
"one unit" of account activity.

Asserted invariants:
- `requests_per_session=None` (the default) is the old behavior: one session
  opened and closed per task, and ALWAYS_ROTATE_ENDPOINTS rotate every task.
- Under a budget one session serves consecutive tasks; crossing the budget
  rotates the ACCOUNT (a session is bound to the account it logged in as).
- Cheap tasks get many per session, expensive ones few — the point of counting
  requests rather than tasks.
- The budget is a high-water mark: a task is never torn down mid-scrape, so a
  single long task may overshoot it.
- A task that raises always closes the session — every error path in
  `execute_task` rotates or retries and assumes a clean browser.
- `rotate_account()` closes the session.
- Scroll accounting uses a per-task delta, so a reused session's cumulative
  `scrolls_recorded` is not counted repeatedly.
- A dry pool at budget-rotation time does not destroy the completed task's
  result.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import fbscrape.worker as worker_mod
from fbscrape.models import Query, ScrapeOutcome
from fbscrape.worker import Worker


class _Acct:
    def __init__(self, ident="a@example.com"):
        self.identifier = ident
        self.display_name = ident


class _FakePool:
    """Minimal AccountsPool stand-in covering the rotation path."""

    def __init__(self, exhausted_after=None):
        self.accounts = [_Acct("a@example.com"), _Acct("b@example.com"), _Acct("c@example.com")]
        # Workers in these tests start out holding accounts[0], so hand out
        # accounts[1] first — otherwise a "rotation" returns the same account.
        self._next = 1
        self._handed_out = 0
        self.exhausted_after = exhausted_after
        self.released = []

    async def get_available(self, order_by=None):
        if self.exhausted_after is not None and self._handed_out >= self.exhausted_after:
            return None
        self._handed_out += 1
        acct = self.accounts[self._next % len(self.accounts)]
        self._next += 1
        return acct

    async def get_available_or_wait(self, order_by=None):
        return await self.get_available(order_by=order_by)

    async def release_account(self, identifier):
        self.released.append(identifier)

    async def lock_until(self, identifier, until, error_msg=None):
        pass


def _session_factory(log, requests_per_task=1, scrolls_per_task=0, raises=None):
    """BrowserSession stand-in recording open/close/task events in `log`.

    `requests_per_task` simulates what `_hybrid_navigate` / `_hybrid_send_replay`
    would add to `requests_sent` for one task.
    """

    class _FakeSession:
        def __init__(self, account=None, **kwargs):
            self.account = account
            self.scrolls_recorded = 0
            self.requests_sent = 0
            self.closed = False

        async def __aenter__(self):
            log.append(("open", self.account.identifier))
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            log.append(("close", self.account.identifier))
            return False

        async def _run(self, **kwargs):
            log.append(("task", self.account.identifier))
            # Both counters are cumulative across tasks on a reused session.
            self.requests_sent += requests_per_task
            self.scrolls_recorded += scrolls_per_task
            if raises is not None:
                raise raises
            return ScrapeOutcome(
                result="success",
                data=[],
                time_started=datetime.now(timezone.utc),
                time_taken=timedelta(seconds=1),
            )

        user_timeline_hybrid = _run
        profile_info_hybrid = _run

    return _FakeSession


def _timeline_query():
    return Query(endpoint="UserTimeline", mode="hybrid", query={"handle": "h"}, params={})


def _profile_query():
    return Query(endpoint="ProfileInfo", mode="hybrid", query={"handle": "h"}, params={})


def _make_worker(monkeypatch, log, requests_per_session=None, requests_per_task=1,
                 scrolls_per_task=0, raises=None, pool=None):
    monkeypatch.setattr(
        worker_mod,
        "BrowserSession",
        _session_factory(log, requests_per_task, scrolls_per_task, raises),
    )
    worker = Worker(
        id="w0",
        pool=pool if pool is not None else _FakePool(),
        requests_per_session=requests_per_session,
    )
    worker.current_account = _Acct()
    return worker


def test_default_opens_and_closes_a_session_per_task(monkeypatch):
    """requests_per_session=None reproduces the original per-task lifecycle."""
    log = []
    worker = _make_worker(monkeypatch, log, requests_per_session=None)

    for _ in range(3):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert [e[0] for e in log] == ["open", "task", "close"] * 3
    assert worker.session is None


def test_session_is_reused_while_under_budget(monkeypatch):
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=10, requests_per_task=1
    )

    asyncio.run(worker.execute_task(_timeline_query()))
    first = worker.session
    assert first is not None and not first.closed

    asyncio.run(worker.execute_task(_timeline_query()))
    assert worker.session is first, "second task must reuse the same session"
    assert first.requests_sent == 2
    assert [e[0] for e in log] == ["open", "task", "task"]


def test_spending_the_budget_rotates_the_account(monkeypatch):
    """A session is bound to its account, so the budget rotates both."""
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=3, requests_per_task=1
    )
    first = worker.current_account.identifier

    asyncio.run(worker.execute_task(_timeline_query()))
    asyncio.run(worker.execute_task(_timeline_query()))
    assert worker.current_account.identifier == first, "no rotation under budget"
    assert worker.session is not None

    asyncio.run(worker.execute_task(_timeline_query()))

    assert worker.session is None, "budget spent → session torn down"
    assert worker.current_account.identifier != first, "budget spent → account rotated"
    assert [e[0] for e in log] == ["open", "task", "task", "task", "close"]


def test_cheap_and_expensive_tasks_get_different_task_counts(monkeypatch):
    """The reason for counting requests instead of tasks: one profile fetch is
    not the same amount of account activity as a 50-pagination timeline scrape."""
    budget = 50

    cheap_log = []
    cheap = _make_worker(
        monkeypatch, cheap_log, requests_per_session=budget, requests_per_task=1
    )
    for _ in range(50):
        asyncio.run(cheap.execute_task(_profile_query()))
    assert cheap_log.count(("open", "a@example.com")) == 1, "50 cheap tasks, 1 session"

    pricey_log = []
    pricey = _make_worker(
        monkeypatch, pricey_log, requests_per_session=budget, requests_per_task=50
    )
    for _ in range(3):
        asyncio.run(pricey.execute_task(_timeline_query()))
    assert [e[0] for e in pricey_log] == [
        "open", "task", "close",   # one task spends the whole budget
        "open", "task", "close",
        "open", "task", "close",
    ]


def test_budget_is_a_high_water_mark_not_a_hard_cap(monkeypatch):
    """A session is never torn down mid-task, so one long task can overshoot."""
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=10, requests_per_task=500
    )

    result = asyncio.run(worker.execute_task(_timeline_query()))

    assert result.result == "success", "the overshooting task still completes"
    assert [e[0] for e in log] == ["open", "task", "close"]
    assert worker.session is None


def test_raising_task_closes_the_session(monkeypatch):
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=100, raises=RuntimeError("boom")
    )

    with pytest.raises(RuntimeError):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert [e[0] for e in log] == ["open", "task", "close"]
    assert worker.session is None


def test_rotate_account_closes_the_session(monkeypatch):
    log = []
    worker = _make_worker(monkeypatch, log, requests_per_session=100)

    asyncio.run(worker.execute_task(_timeline_query()))
    opened_as = worker.session.account.identifier

    asyncio.run(worker.rotate_account())

    assert worker.session is None
    assert log[-1] == ("close", opened_as)


def test_scroll_accounting_uses_a_per_task_delta(monkeypatch):
    """Counting session totals instead of deltas would give 10+20+30=60 and
    rotate accounts far too early."""
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=100, scrolls_per_task=10
    )

    for _ in range(3):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert worker.session.scrolls_recorded == 30
    assert worker.scroll_count == 30


def test_always_rotate_endpoint_still_rotates_per_task_without_a_budget(monkeypatch):
    log = []
    worker = _make_worker(monkeypatch, log, requests_per_session=None)
    first = worker.current_account.identifier

    asyncio.run(worker.execute_task(_profile_query()))

    assert worker.current_account.identifier != first
    assert worker.session is None


def test_always_rotate_endpoint_defers_to_the_budget(monkeypatch):
    """With a budget set, ProfileInfo must NOT rotate after every task —
    otherwise reuse buys nothing for exactly the workload it's meant for."""
    log = []
    worker = _make_worker(
        monkeypatch, log, requests_per_session=3, requests_per_task=1
    )
    first = worker.current_account.identifier

    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier == first
    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier == first

    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier != first
    assert [e[0] for e in log] == ["open", "task", "task", "task", "close"]


def test_dry_pool_at_budget_rotation_keeps_the_result(monkeypatch):
    """The task already succeeded; an empty pool must not throw its result away."""
    log = []
    pool = _FakePool(exhausted_after=0)
    worker = _make_worker(
        monkeypatch, log, requests_per_session=1, requests_per_task=1, pool=pool
    )

    result = asyncio.run(worker.execute_task(_timeline_query()))

    assert result.result == "success"
    assert worker.session is None
    assert worker.current_account is None, "released, with nothing to rotate into"


def test_worker_close_closes_the_session(monkeypatch):
    log = []
    worker = _make_worker(monkeypatch, log, requests_per_session=100)

    asyncio.run(worker.execute_task(_timeline_query()))
    asyncio.run(worker.close())

    assert worker.session is None
    assert log[-1][0] == "close"


def test_requests_per_session_must_be_positive_or_none():
    with pytest.raises(ValueError, match="requests_per_session"):
        Worker(id="w0", pool=object(), requests_per_session=0)
    Worker(id="w0", pool=object(), requests_per_session=None)  # allowed
