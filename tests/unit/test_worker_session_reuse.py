"""BrowserSession reuse across tasks (`Worker.tasks_per_session`).

Historically `Worker.execute_task` opened a fresh `BrowserSession` — a browser
process launch plus a login — inside its retry loop, for every task. That cost
is invisible on a long timeline scrape but dominates short single-shot
endpoints (ProfileInfo, ProfileAbout, GroupInfo, GroupAbout), where the actual
work is one navigation.

`tasks_per_session=N` keeps one session alive across N consecutive tasks. The
scrape methods were already re-entrant on a single session (each one opens with
`response_interceptor.flush()`, and `_install_stream_hook` assigns rather than
appends), so the reuse is a Worker-level policy change only.

Asserted invariants:
- `tasks_per_session=1` (the default) is byte-for-byte the old behavior: one
  session opened and closed per task.
- With N>1 one session serves N tasks, then closes.
- A task that raises always closes the session — every error path in
  `execute_task` rotates or retries and assumes a clean browser.
- `rotate_account()` closes the session: a session outliving its account would
  scrape as the wrong user.
- Scroll accounting uses a per-task delta, so a reused session's cumulative
  `scrolls_recorded` is not counted repeatedly.
- ALWAYS_ROTATE_ENDPOINTS rotate every task at N=1 and every N tasks at N>1.
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

    def __init__(self):
        self.accounts = [_Acct("a@example.com"), _Acct("b@example.com"), _Acct("c@example.com")]
        # Workers in these tests start out holding accounts[0], so hand out
        # accounts[1] first — otherwise a "rotation" returns the same account.
        self._next = 1
        self.released = []

    async def get_available(self, order_by=None):
        acct = self.accounts[self._next % len(self.accounts)]
        self._next += 1
        return acct

    async def get_available_or_wait(self, order_by=None):
        return await self.get_available(order_by=order_by)

    async def release_account(self, identifier):
        self.released.append(identifier)

    async def lock_until(self, identifier, until, error_msg=None):
        pass


def _session_factory(log, scrolls_per_task=0, raises=None):
    """Build a BrowserSession stand-in that records open/close events in `log`."""

    class _FakeSession:
        instances = []

        def __init__(self, account=None, **kwargs):
            self.account = account
            self.scrolls_recorded = 0
            self.closed = False
            _FakeSession.instances.append(self)

        async def __aenter__(self):
            log.append(("open", self.account.identifier))
            return self

        async def __aexit__(self, *exc):
            self.closed = True
            log.append(("close", self.account.identifier))
            return False

        async def _run(self, **kwargs):
            log.append(("task", self.account.identifier))
            # Cumulative across tasks on a reused session — the worker must
            # subtract its own per-task snapshot.
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


def _make_worker(monkeypatch, log, tasks_per_session=1, scrolls_per_task=0, raises=None):
    monkeypatch.setattr(
        worker_mod, "BrowserSession", _session_factory(log, scrolls_per_task, raises)
    )
    pool = _FakePool()
    worker = Worker(id="w0", pool=pool, tasks_per_session=tasks_per_session)
    worker.current_account = _Acct()
    return worker


def test_default_opens_and_closes_a_session_per_task(monkeypatch):
    """tasks_per_session=1 reproduces the original per-task session lifecycle."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=1)

    for _ in range(3):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert log == [
        ("open", "a@example.com"), ("task", "a@example.com"), ("close", "a@example.com"),
        ("open", "a@example.com"), ("task", "a@example.com"), ("close", "a@example.com"),
        ("open", "a@example.com"), ("task", "a@example.com"), ("close", "a@example.com"),
    ]
    assert worker.session is None


def test_one_session_serves_n_tasks_then_closes(monkeypatch):
    """N tasks run on a single session; the Nth closes it."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=3)

    for _ in range(3):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert [e[0] for e in log] == ["open", "task", "task", "task", "close"]
    assert worker.session is None

    # A 4th task opens a second session.
    asyncio.run(worker.execute_task(_timeline_query()))
    assert [e[0] for e in log] == ["open", "task", "task", "task", "close", "open", "task"]
    assert worker.session is not None


def test_session_survives_between_tasks_below_the_cap(monkeypatch):
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=5)

    asyncio.run(worker.execute_task(_timeline_query()))
    first = worker.session
    assert first is not None and not first.closed

    asyncio.run(worker.execute_task(_timeline_query()))
    assert worker.session is first, "second task must reuse the same session"
    assert worker.tasks_on_session == 2


def test_raising_task_closes_the_session(monkeypatch):
    """Every error path in execute_task assumes a clean browser on retry."""
    log = []
    # A bare RuntimeError on a non-ALWAYS_ROTATE endpoint propagates (the
    # generic handler re-raises), which is the cleanest way to observe it.
    worker = _make_worker(
        monkeypatch, log, tasks_per_session=10, raises=RuntimeError("boom")
    )

    with pytest.raises(RuntimeError):
        asyncio.run(worker.execute_task(_timeline_query()))

    assert log == [("open", "a@example.com"), ("task", "a@example.com"), ("close", "a@example.com")]
    assert worker.session is None


def test_rotate_account_closes_the_session(monkeypatch):
    """A session is bound to its account and must not outlive it."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=10)

    asyncio.run(worker.execute_task(_timeline_query()))
    assert worker.session is not None
    opened_as = worker.session.account.identifier

    asyncio.run(worker.rotate_account())

    assert worker.session is None
    assert log[-1] == ("close", opened_as)
    assert worker.current_account.identifier != opened_as

    # The next task opens a session on the NEW account.
    asyncio.run(worker.execute_task(_timeline_query()))
    assert log[-2] == ("open", worker.current_account.identifier)


def test_scroll_accounting_uses_a_per_task_delta(monkeypatch):
    """A reused session's scrolls_recorded is cumulative; scroll_count must not
    re-count earlier tasks (which would rotate accounts far too early)."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=4, scrolls_per_task=10)

    for _ in range(3):
        asyncio.run(worker.execute_task(_timeline_query()))

    # 3 tasks x 10 scrolls. Counting session totals instead of deltas would
    # give 10 + 20 + 30 = 60.
    assert worker.session.scrolls_recorded == 30
    assert worker.scroll_count == 30


def test_always_rotate_endpoint_rotates_every_task_by_default(monkeypatch):
    """ProfileInfo & co. never scroll, so scroll_threshold can't rotate them —
    at the default they keep rotating on every task."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=1)
    first = worker.current_account.identifier

    asyncio.run(worker.execute_task(_profile_query()))

    assert worker.current_account.identifier != first
    assert worker.session is None


def test_always_rotate_endpoint_rotates_on_the_session_boundary(monkeypatch):
    """With N>1 those endpoints rotate every N tasks instead of every task."""
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=3)
    first = worker.current_account.identifier

    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier == first, "no rotation mid-session"
    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier == first

    asyncio.run(worker.execute_task(_profile_query()))
    assert worker.current_account.identifier != first, "rotates on the 3rd"
    assert worker.session is None
    assert [e[0] for e in log] == ["open", "task", "task", "task", "close"]


def test_worker_close_closes_the_session(monkeypatch):
    log = []
    worker = _make_worker(monkeypatch, log, tasks_per_session=10)

    asyncio.run(worker.execute_task(_timeline_query()))
    assert worker.session is not None

    asyncio.run(worker.close())
    assert worker.session is None
    assert log[-1][0] == "close"


def test_tasks_per_session_must_be_positive():
    with pytest.raises(ValueError, match="tasks_per_session"):
        Worker(id="w0", pool=object(), tasks_per_session=0)
