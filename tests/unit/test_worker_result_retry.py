"""Retry policy for RESULT STRINGS returned by a scrape (`Worker.execute_task`).

`execute_task`'s retry budget is driven almost entirely by exceptions — every
`retry_count += 1` sits in an `except` block. A scrape that returns normally is
only inspected against a handful of `outcome.result` strings before
`return result`, so any result not named there consumes exactly one attempt no
matter how transient it is.

`template_capture_timeout` was in that gap. It is returned when the pagination
template never arrives, which by the hybrid phase order (navigate -> bootstrap
scroll -> capture template -> paginate) happens BEFORE any post is collected:
the outcome carries no data and no cursor, so retrying costs nothing and
returning early saves nothing. It is also typically transient — the query simply
does not fire on that page load.

Asserted invariants:
- `template_capture_timeout` retries, and a subsequent success is what the
  caller receives.
- The retry does not rotate the account (the failure is not account-specific,
  and a rotation would spend a cooldown lock for nothing).
- The retry gets a FRESH session even under a request budget, where
  `_task_session` would otherwise keep the browser that just failed.
- A PERSISTENT failure returns the diagnosis rather than raising
  RetryBudgetExhaustedError — the result string is the only record of why a
  target produced nothing.
- The retry budget is shared, not per-result: repeated timeouts stop at
  `max_retries` attempts.
- Results that are terminal by design (`response_shape_error`) still return on
  the first attempt.
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
    def __init__(self):
        self.accounts = [_Acct("a@example.com"), _Acct("b@example.com"), _Acct("c@example.com")]
        self._next = 1
        self.released = []
        self.locks = []

    async def get_available(self, order_by=None):
        acct = self.accounts[self._next % len(self.accounts)]
        self._next += 1
        return acct

    async def get_available_or_wait(self, order_by=None):
        return await self.get_available(order_by=order_by)

    async def release_account(self, identifier):
        self.released.append(identifier)

    async def lock_until(self, identifier, until, error_msg=None):
        self.locks.append((identifier, until))


def _outcome(result, data=None):
    return ScrapeOutcome(
        result=result,
        data=data,
        time_started=datetime.now(timezone.utc),
        time_taken=timedelta(seconds=1),
    )


def _session_factory(log, results):
    """BrowserSession stand-in yielding `results` (one per task) in order."""

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
            self.requests_sent += 1
            # `data=None` mirrors a real capture failure: nothing was collected,
            # so the accumulator was never populated.
            return results.pop(0) if results else _outcome("success", [])

        user_timeline_hybrid = _run

    return _FakeSession


def _query():
    return Query(endpoint="UserTimeline", mode="hybrid", query={"handle": "h"}, params={})


def _make_worker(monkeypatch, log, results, requests_per_session=None, pool=None):
    monkeypatch.setattr(worker_mod, "BrowserSession", _session_factory(log, results))
    worker = Worker(
        id="w0",
        pool=pool if pool is not None else _FakePool(),
        requests_per_session=requests_per_session,
    )
    worker.current_account = _Acct()
    return worker


def test_template_capture_timeout_is_retried_and_the_retry_wins(monkeypatch):
    """A transient capture miss must not cost the target its whole scrape."""
    log = []
    results = [_outcome("template_capture_timeout"), _outcome("success", [{"post_id": "1"}])]
    worker = _make_worker(monkeypatch, log, results)

    result = asyncio.run(worker.execute_task(_query()))

    assert result.result == "success"
    assert len(result.data) == 1
    # Two attempts: the timeout, then the one that captured.
    assert [e[0] for e in log].count("task") == 2


def test_retry_does_not_rotate_the_account(monkeypatch):
    """The miss is not account-specific — rotating would burn a cooldown lock."""
    log = []
    pool = _FakePool()
    results = [_outcome("template_capture_timeout"), _outcome("success", [])]
    worker = _make_worker(monkeypatch, log, results, pool=pool)
    before = worker.current_account.identifier

    asyncio.run(worker.execute_task(_query()))

    assert pool.released == []
    assert pool.locks == []
    assert worker.current_account.identifier == before


def test_retry_gets_a_fresh_session_under_a_request_budget(monkeypatch):
    """`_task_session` keeps the browser alive on a clean exit when a budget is
    set; the retry must not inherit the one that just failed to capture."""
    log = []
    results = [_outcome("template_capture_timeout"), _outcome("success", [])]
    worker = _make_worker(monkeypatch, log, results, requests_per_session=100)

    asyncio.run(worker.execute_task(_query()))

    # open/task/close for the failed attempt, then a second open for the retry.
    assert [e[0] for e in log] == ["open", "task", "close", "open", "task"]


def test_persistent_failure_returns_the_diagnosis(monkeypatch):
    """Exhausting the budget must not replace the reason with a generic error."""
    log = []
    results = [_outcome("template_capture_timeout") for _ in range(5)]
    worker = _make_worker(monkeypatch, log, results)

    result = asyncio.run(worker.execute_task(_query()))

    assert result.result == "template_capture_timeout"
    # Stops at the budget rather than retrying forever.
    assert [e[0] for e in log].count("task") == 3


def test_response_shape_error_still_returns_on_the_first_attempt(monkeypatch):
    """A structural bug is not instance-specific: the next attempt sees the same
    shape, so it must not consume the retry budget."""
    log = []
    results = [_outcome("response_shape_error", [{"post_id": "1"}])]
    worker = _make_worker(monkeypatch, log, results)

    result = asyncio.run(worker.execute_task(_query()))

    assert result.result == "response_shape_error"
    assert [e[0] for e in log].count("task") == 1
