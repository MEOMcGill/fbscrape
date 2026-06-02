"""WorkerPool failed-task isolation (`_worker_loop`).

A single task that raises must NOT bring down the batch. Before this fix the
generic `except` did `future.set_exception(e)`, which — because every
`FacebookScraper.<endpoint>` awaits its future inside `gather()` — re-raised
and tore down ALL concurrent handles (observed in production 2026-06-02: one
login `Page.goto` timeout → `RetryBudgetExhaustedError` killed ~150 handles).

`_worker_loop` now *resolves* the future with a failed `ScrapingResult`
(`result="task_failed: <ExcName>"`, `data=[]`) so the caller records the
failure and the pool keeps serving the rest of the queue. `NoAccountError`
keeps its distinct requeue+exit semantics (it's pool-level, not task-level).

Asserted invariants:
- A worker exception resolves the future as a `task_failed` result, not a raise.
- The pool keeps serving subsequent tasks after one fails.
- `NoAccountError` is still handled distinctly (requeue + worker exit; the
  drain path surfaces it as a raised `NoAccountError`, NOT a `task_failed`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fbscrape.worker_pool import WorkerPool
from fbscrape.models import Query, ScrapingResult
from fbscrape.exceptions import RetryBudgetExhaustedError, NoAccountError


def _make_query(handle: str = "testhandle") -> Query:
    # params={} → Query.__post_init__ fills registry defaults for (UserTimeline, hybrid).
    return Query(endpoint="UserTimeline", mode="hybrid", query={"handle": handle}, params={})


def _ok_result(query: Query) -> ScrapingResult:
    return ScrapingResult(
        query=query,
        result="success",
        data=[{"post_id": "1"}],
        time_started=datetime.now(timezone.utc),
        time_taken=timedelta(seconds=1),
    )


class _FakeWorker:
    """Stands in for Worker: _worker_loop only touches .id, execute_task, close."""

    def __init__(self, worker_id: str, side_effects: list):
        self.id = worker_id
        self._side_effects = list(side_effects)
        self.closed = False
        self.calls = 0

    async def execute_task(self, query):
        effect = (self._side_effects[self.calls]
                  if self.calls < len(self._side_effects)
                  else self._side_effects[-1])
        self.calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def close(self):
        self.closed = True


def _make_pool() -> WorkerPool:
    # __init__ only stores `pool`; _worker_loop never uses it, so a dummy is fine.
    pool = WorkerPool(pool=object(), max_workers=1)
    pool._initialized = True
    return pool


async def test_worker_exception_resolves_as_failed_task():
    pool = _make_pool()
    worker = _FakeWorker("worker-0", [RetryBudgetExhaustedError("retries exhausted")])
    pool.workers.append(worker)

    query = _make_query()
    future = asyncio.get_running_loop().create_future()
    await pool.task_queue.put((query, future))

    loop_task = asyncio.create_task(pool._worker_loop(worker))
    result = await asyncio.wait_for(future, timeout=2.0)
    pool._shutdown = True
    await asyncio.wait_for(loop_task, timeout=2.0)

    # Resolved (not raised) as a failed result.
    assert isinstance(result, ScrapingResult)
    assert result.result == "task_failed: RetryBudgetExhaustedError"
    assert result.data == []
    assert result.query is query
    assert worker.closed


async def test_pool_keeps_serving_after_failed_task():
    pool = _make_pool()
    q1, q2 = _make_query("fails"), _make_query("succeeds")
    worker = _FakeWorker(
        "worker-0",
        [RetryBudgetExhaustedError("boom"), _ok_result(q2)],
    )
    pool.workers.append(worker)

    f1 = asyncio.get_running_loop().create_future()
    f2 = asyncio.get_running_loop().create_future()
    await pool.task_queue.put((q1, f1))
    await pool.task_queue.put((q2, f2))

    loop_task = asyncio.create_task(pool._worker_loop(worker))
    r1 = await asyncio.wait_for(f1, timeout=2.0)
    r2 = await asyncio.wait_for(f2, timeout=2.0)
    pool._shutdown = True
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert r1.result == "task_failed: RetryBudgetExhaustedError"
    assert r2.result == "success"          # the failure didn't stop the worker
    assert r2.data == [{"post_id": "1"}]
    assert worker.calls == 2


async def test_no_account_error_requeues_not_failed():
    pool = _make_pool()
    worker = _FakeWorker("worker-0", [NoAccountError("none free")])
    pool.workers.append(worker)

    query = _make_query()
    future = asyncio.get_running_loop().create_future()
    await pool.task_queue.put((query, future))

    # NoAccountError → requeue + break; as the last worker, the loop's finally
    # drains the queue and surfaces NoAccountError on the future (terminal),
    # which is distinctly NOT a task_failed ScrapingResult.
    loop_task = asyncio.create_task(pool._worker_loop(worker))
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert future.done()
    with pytest.raises(NoAccountError):
        future.result()
    assert worker.closed
