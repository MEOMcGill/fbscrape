"""Resume-state streamer (`_stream_resume_state`).

`--continue` re-reads saved scrape files purely to recover (a) `last_cursor`
and (b) the set of already-seen `post_id`s for dedup. The streamer must
extract both without materializing the full posts array — that's what
lets a batch of dozens of `--continue` targets start in seconds instead
of minutes.

Asserted invariants:
- Reads plain `.json` and gzipped `.json.gz` identically.
- Recognizes both the modern `{"data": [...]}` and legacy `{"posts": [...]}`
  top-level keys (the CLI loaders still accept the legacy shape).
- `node.post_id` wins over top-level `post_id` (matches the prior
  `json.load`-then-extract precedence).
- Skips records with no post_id under either path.
- Empty / missing `last_cursor` collapses to `""` (existing semantics).
- Nested `post_id` fields (e.g. inside `attached_story` or `comments`)
  do NOT leak into the result set.
"""


import gzip
import json
from datetime import datetime, timedelta

import pytest

from fbscrape.models import Query, ScrapingResult
from fbscrape.scraper import _stream_resume_state


def _write(tmp_path, name: str, payload: dict, *, gz: bool):
    path = tmp_path / name
    if gz:
        with gzip.open(path, "wt") as f:
            json.dump(payload, f, indent=2)
    else:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    return str(path)


@pytest.mark.parametrize("gz", [False, True])
def test_streams_cursor_and_post_ids(tmp_path, gz):
    payload = {
        "query": {"endpoint": "GroupTimeline"},
        "result": "success",
        "data": [
            {"node": {"post_id": "A1"}},
            {"node": {"post_id": "A2"}},
            {"post_id": "B3"},                            # top-level only
            {"node": {"post_id": "A4"}, "post_id": "X"},  # node wins over top
            {"node": {}},                                  # no pid → skipped
            {"some": "other"},                             # no pid → skipped
        ],
        "time_started": "2025-01-01 12:00:00",
        "time_taken": "0:00:42",
        "last_cursor": "ABC_cursor_token",
    }
    path = _write(tmp_path, "out.json" + (".gz" if gz else ""), payload, gz=gz)

    cursor, ids = _stream_resume_state(path)

    assert cursor == "ABC_cursor_token"
    assert ids == ["A1", "A2", "B3", "A4"]


def test_legacy_posts_key(tmp_path):
    """Pre-rename files use `"posts"` instead of `"data"` — must still resume."""
    payload = {
        "query": {"endpoint": "UserTimeline"},
        "result": "success",
        "posts": [
            {"node": {"post_id": "legacy1"}},
            {"post_id": "legacy2"},
        ],
        "last_cursor": "legacy_cursor",
    }
    path = _write(tmp_path, "legacy.json", payload, gz=False)

    cursor, ids = _stream_resume_state(path)

    assert cursor == "legacy_cursor"
    assert ids == ["legacy1", "legacy2"]


def test_empty_cursor_and_empty_data(tmp_path):
    payload = {"data": [], "last_cursor": None}
    path = _write(tmp_path, "empty.json", payload, gz=False)

    cursor, ids = _stream_resume_state(path)

    assert cursor == ""
    assert ids == []


def test_nested_post_id_fields_do_not_leak(tmp_path):
    """Deep `post_id` keys (attached_story, comments, ...) must be ignored —
    only the outer record's `post_id` / `node.post_id` is part of dedup."""
    payload = {
        "data": [
            {
                "node": {
                    "post_id": "outer1",
                    "attached_story": {"post_id": "INNER_attached"},
                    "comet_sections": {
                        "feedback": {"post_id": "INNER_feedback"},
                    },
                },
            },
            {
                "post_id": "outer2",
                "shared_post": {"node": {"post_id": "INNER_shared"}},
            },
        ],
        "last_cursor": "c",
    }
    path = _write(tmp_path, "nested.json", payload, gz=False)

    _, ids = _stream_resume_state(path)

    assert ids == ["outer1", "outer2"]


def test_matches_scraping_result_save_roundtrip(tmp_path):
    """The streamer must agree with the prior json.load path on a file
    written by `ScrapingResult.save()` — that's the format on disk."""
    q = Query(
        endpoint="GroupTimeline", mode="hybrid",
        query={"handle": "392585550772135",
               "start_date": "2024-01-01", "end_date": "2025-01-01"},
        params={},
    )
    result = ScrapingResult(
        query=q,
        result="success",
        data=[
            {"node": {"post_id": "p1"}},
            {"node": {"post_id": "p2"}},
            {"node": {"post_id": "p3"}},
        ],
        time_started=datetime(2025, 1, 1, 12, 0, 0),
        time_taken=timedelta(seconds=12),
        last_cursor="real_cursor_blob",
    )
    # `save()` now emits JSONL; the resume read goes through the dual-format
    # dispatcher (`_read_resume_state` → `read_resume_tail` for JSONL).
    from fbscrape.scraper import _read_resume_state
    plain = result.save(str(tmp_path / "rt.jsonl"), compress=False)
    gz = result.save(str(tmp_path / "rt2.jsonl"), compress=True)

    for path in (plain, gz):
        cursor, ids = _read_resume_state(path)
        assert cursor == "real_cursor_blob"
        assert ids == ["p1", "p2", "p3"]
