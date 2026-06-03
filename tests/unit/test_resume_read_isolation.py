"""Resume-read isolation (`FacebookScraper.user_timeline`).

`--continue` recovers a handle's cursor + seen-post-ids by stream-reading its
saved `.json.gz` (`_stream_resume_state`) *before* the scrape task is submitted.
That read runs inside the `user_timeline` coroutine, which the CLI drives via
`gather()` (`as_completed` + `yield await c`). So a raised exception there —
e.g. a `.json.gz` truncated by a `pkill -9`-interrupted save — propagates out
of `gather()` and tears down the ENTIRE batch (observed in production
2026-06-03: one corrupt file killed a 149-handle run after 7 completions).

`user_timeline` now wraps the resume-read: on failure it logs a warning and
falls back to a **fresh scrape** for that handle (no cursor, no seen-id dedup)
instead of propagating.

Asserted invariants:
- A failing resume-read does NOT raise out of `user_timeline`; the handle is
  scraped fresh (submitted Query carries no `initial_cursor`).
- A successful resume-read still threads the cursor + seen-ids through (the
  isolation wrapper doesn't break the happy path).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from fbscrape import scraper as scraper_mod
from fbscrape.scraper import FacebookScraper
from fbscrape.models import Query, ScrapingResult


class _MockPool:
    """Stand-in worker pool: captures the submitted Query, resolves the future
    immediately with a trivial success result so user_timeline's leg loop ends."""

    def __init__(self):
        self.submitted: list[Query] = []

    async def submit_task(self, query: Query) -> asyncio.Future:
        self.submitted.append(query)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(ScrapingResult(
            query=query,
            result="success",
            data=[],                       # empty → user_timeline's dedup loop no-ops
            time_started=datetime.now(timezone.utc),
            time_taken=timedelta(seconds=1),
            last_cursor=None,
        ))
        return fut


def _make_scraper(tmp_path, monkeypatch) -> tuple[FacebookScraper, _MockPool]:
    s = FacebookScraper(db=str(tmp_path / "t.db"))
    pool = _MockPool()
    s.worker_pool = pool
    async def _noop():  # don't build a real WorkerPool
        return
    monkeypatch.setattr(s, "_ensure_initialized", _noop)
    return s, pool


async def test_corrupt_resume_falls_back_to_fresh_scrape(tmp_path, monkeypatch):
    def _boom(_path):
        raise EOFError("Compressed file ended before the end-of-stream marker")
    monkeypatch.setattr(scraper_mod, "_stream_resume_state", _boom)

    s, pool = _make_scraper(tmp_path, monkeypatch)
    # Must NOT raise despite the corrupt resume file.
    result = await s.user_timeline(
        "somehandle", mode="hybrid", resume_from="/fake/corrupt.json.gz", max_posts=10,
    )
    assert result.result == "success"
    assert len(pool.submitted) == 1
    # Fresh scrape: no cursor threaded through.
    assert pool.submitted[0].params.get("initial_cursor", "") == ""


async def test_valid_resume_threads_cursor_through(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scraper_mod, "_stream_resume_state",
        lambda _path: ("CURSOR123", ["p1", "p2"]),
    )
    s, pool = _make_scraper(tmp_path, monkeypatch)
    await s.user_timeline(
        "somehandle", mode="hybrid", resume_from="/fake/good.json.gz", max_posts=10,
    )
    assert len(pool.submitted) == 1
    assert pool.submitted[0].params.get("initial_cursor") == "CURSOR123"
    assert pool.submitted[0].params.get("seen_post_ids_to_skip") == ["p1", "p2"]
