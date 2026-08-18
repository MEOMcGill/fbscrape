"""`_finalize_continue_result` — save as JSONL; `--continue` APPENDS.

JSONL makes `--continue` an append (a new gzip member), not a whole-file
rewrite. A legacy whole-file envelope prior is migrated to JSONL once, then
appended to. On a `no_new_posts_streak` resume leg, a deeper cursor is chosen
(auto-unstick) and appended as a trailing status line.

Invariants:
- No `--continue`: writes `<stem>.jsonl.gz`.
- `--continue` + JSONL prior: prior lines kept, new appended after.
- `--continue` + legacy envelope prior: migrated to JSONL, then appended; legacy removed.
- `--continue` + missing prior: just saves.
- `--continue` + no_new_posts_streak: trailing status line carries a deeper cursor.
"""


import gzip
import json
import os
from datetime import datetime, timedelta

from fbscrape.cli import _finalize_continue_result
from fbscrape.models import Query, ScrapingResult
from fbscrape.jsonl_store import load_scrape_file, read_meta


def _make_result(handle, new_records, *, last_cursor="new_cursor", result="success"):
    q = Query(
        endpoint="GroupTimeline", mode="hybrid",
        query={"handle": handle, "start_date": "2024-01-01", "end_date": "2025-01-01"},
        params={},
    )
    return ScrapingResult(
        query=q, result=result, data=list(new_records),
        time_started=datetime(2025, 1, 1, 12, 0, 0),
        time_taken=timedelta(seconds=5), last_cursor=last_cursor,
    )


def _ids(path):
    _, records = load_scrape_file(path)
    return [r["node"]["post_id"] for r in records]


def test_no_continue_just_saves(tmp_path):
    r = _make_result("zuck", [{"node": {"post_id": "n1"}}])
    _finalize_continue_result(r, str(tmp_path), continue_=False)
    dest = str(tmp_path / "zuck_GroupTimeline_hybrid.jsonl.gz")
    assert _ids(dest) == ["n1"]
    assert read_meta(dest)["last_cursor"] == "new_cursor"


def test_continue_appends_to_jsonl_prior(tmp_path):
    dest = str(tmp_path / "zuck_GroupTimeline_hybrid.jsonl.gz")
    _make_result("zuck", [{"node": {"post_id": "p1"}}, {"node": {"post_id": "p2"}}],
                 last_cursor="old").save(dest)
    new = _make_result("zuck", [{"node": {"post_id": "n1"}}], last_cursor="new_cursor")
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    assert _ids(dest) == ["p1", "p2", "n1"]          # appended, not rewritten
    assert read_meta(dest)["last_cursor"] == "new_cursor"


def test_continue_migrates_legacy_envelope_then_appends(tmp_path):
    # A legacy whole-file envelope prior (compact one-line gzip; data is a list).
    legacy = {
        "query": {"endpoint": "GroupTimeline", "mode": "hybrid",
                  "query": {"handle": "zuck"}, "params": {}},
        "result": "success", "data": [{"node": {"post_id": "p1"}}],
        "time_started": "2025-01-01 00:00:00", "time_taken": "0:00:01",
        "last_cursor": "old",
    }
    legacy_path = tmp_path / "zuck_GroupTimeline_hybrid.json.gz"
    with gzip.open(legacy_path, "wt") as f:
        json.dump(legacy, f)

    new = _make_result("zuck", [{"node": {"post_id": "n1"}}], last_cursor="new_cursor")
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    dest = str(tmp_path / "zuck_GroupTimeline_hybrid.jsonl.gz")
    assert _ids(dest) == ["p1", "n1"]                 # migrated prior + appended new
    assert not os.path.exists(legacy_path)            # legacy removed after migrate


def test_continue_missing_prior_just_saves(tmp_path):
    new = _make_result("nobody", [{"node": {"post_id": "n1"}}])
    _finalize_continue_result(new, str(tmp_path), continue_=True)
    assert _ids(str(tmp_path / "nobody_GroupTimeline_hybrid.jsonl.gz")) == ["n1"]


def test_continue_no_new_posts_streak_triggers_unstick(tmp_path):
    base_ts = 1_700_000_000
    prior_records = [
        {
            "node": {
                "post_id": f"p{i}",
                "comet_sections": {"context_layout": {"story": {"comet_sections": {
                    "metadata": [{
                        "__typename": "CometFeedStoryMinimizedTimestampStrategy",
                        "story": {"creation_time": base_ts + i * 3600},
                    }],
                }}}},
            },
            "cursor": f"cursor_{i}",
        }
        for i in range(10)
    ]
    dest = str(tmp_path / "zuck_GroupTimeline_hybrid.jsonl.gz")
    _make_result("zuck", prior_records, last_cursor="stale_cursor").save(dest)

    new = _make_result("zuck", [], result="no_new_posts_streak", last_cursor="stale_cursor")
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    # Trailing status line carries the deeper (rank-3 oldest cursored) cursor.
    last_cursor = read_meta(dest)["last_cursor"]
    assert last_cursor != "stale_cursor"
    assert last_cursor.startswith("cursor_")
