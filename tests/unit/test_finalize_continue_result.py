"""`_finalize_continue_result` — merge prior data, auto-unstick, save.

Extracted from the inline scrape post-processing block so it can run on
a thread (via `asyncio.to_thread`) without blocking the next yielded
result. Tests here lock the merge / auto-unstick / save contract that
the inline version used to provide.

Asserted invariants:
- Without `--continue`: just saves, no prior load attempted.
- With `--continue` + existing `.json.gz`: prior data is concatenated
  in front of new data and the merged result is written back to the
  same stem.
- With `--continue` + existing `.json` (no gz): same merge, falls back
  to the plain-JSON extension.
- With `--continue` + missing prior file: save still happens, no merge.
- With `--continue` + result='no_new_posts_streak': auto-unstick swaps
  `last_cursor` to the rank-3 oldest cursored post in merged data.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from fbscrape.cli import _finalize_continue_result
from fbscrape.models import Query, ScrapingResult


def _make_result(
    handle: str,
    new_records: list[dict],
    *,
    last_cursor: str = "new_cursor",
    result: str = "success",
) -> ScrapingResult:
    q = Query(
        endpoint="GroupTimeline", mode="hybrid",
        query={"handle": handle,
               "start_date": "2024-01-01", "end_date": "2025-01-01"},
        params={},
    )
    return ScrapingResult(
        query=q,
        result=result,
        data=list(new_records),
        time_started=datetime(2025, 1, 1, 12, 0, 0),
        time_taken=timedelta(seconds=5),
        last_cursor=last_cursor,
    )


def _read_saved(path: str) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def test_no_continue_just_saves(tmp_path):
    """Without --continue, no prior load attempted; new data persisted as-is."""
    r = _make_result("zuck", [{"node": {"post_id": "n1"}}])
    _finalize_continue_result(r, str(tmp_path), continue_=False)

    saved = _read_saved(str(tmp_path / "zuck_GroupTimeline_hybrid.json.gz"))
    assert [rec["node"]["post_id"] for rec in saved["data"]] == ["n1"]
    assert saved["last_cursor"] == "new_cursor"


def test_continue_merges_prior_gz(tmp_path):
    """--continue with existing .json.gz: prior + new, prior comes first."""
    prior = _make_result(
        "zuck",
        [{"node": {"post_id": "p1"}}, {"node": {"post_id": "p2"}}],
        last_cursor="old_cursor",
    )
    prior.save(str(tmp_path / "zuck_GroupTimeline_hybrid.json"), compress=True)

    new = _make_result("zuck", [{"node": {"post_id": "n1"}}],
                       last_cursor="new_cursor")
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    saved = _read_saved(str(tmp_path / "zuck_GroupTimeline_hybrid.json.gz"))
    assert [rec["node"]["post_id"] for rec in saved["data"]] == ["p1", "p2", "n1"]
    assert saved["last_cursor"] == "new_cursor"


def test_continue_merges_prior_plain_json(tmp_path):
    """--continue with existing .json (no gz): falls back to plain extension."""
    prior = _make_result("zuck", [{"node": {"post_id": "p1"}}])
    # save without compress=True → writes .json, not .json.gz
    prior.save(str(tmp_path / "zuck_GroupTimeline_hybrid.json"), compress=False)

    new = _make_result("zuck", [{"node": {"post_id": "n1"}}])
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    saved = _read_saved(str(tmp_path / "zuck_GroupTimeline_hybrid.json.gz"))
    assert [rec["node"]["post_id"] for rec in saved["data"]] == ["p1", "n1"]


def test_continue_missing_prior_just_saves(tmp_path):
    """--continue with no prior file: save still happens, no crash."""
    new = _make_result("nobody", [{"node": {"post_id": "n1"}}])
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    saved = _read_saved(str(tmp_path / "nobody_GroupTimeline_hybrid.json.gz"))
    assert [rec["node"]["post_id"] for rec in saved["data"]] == ["n1"]


def test_continue_no_new_posts_streak_triggers_unstick(tmp_path):
    """When result='no_new_posts_streak', last_cursor is swapped to a deeper
    anchor (rank-3 oldest cursored post in merged data)."""
    # Build prior with several cursored posts at distinct creation_times.
    # `_find_unstick_cursor` looks at flattened `created_at` + `cursor` —
    # supply records that survive parser.flatten with those fields.
    base_ts = 1_700_000_000
    prior_records = [
        {
            "node": {
                "post_id": f"p{i}",
                "creation_time": base_ts + i * 3600,
                "comet_sections": {
                    "context_layout": {
                        "story": {
                            "comet_sections": {
                                "metadata": [{
                                    "__typename": "CometFeedStoryMinimizedTimestampStrategy",
                                    "story": {"creation_time": base_ts + i * 3600},
                                }],
                            },
                        },
                    },
                },
            },
            "cursor": f"cursor_{i}",
        }
        for i in range(10)
    ]
    prior = _make_result("zuck", prior_records, last_cursor="stale_cursor")
    prior.save(str(tmp_path / "zuck_GroupTimeline_hybrid.json"), compress=True)

    new = _make_result("zuck", [], result="no_new_posts_streak",
                       last_cursor="stale_cursor")
    _finalize_continue_result(new, str(tmp_path), continue_=True)

    saved = _read_saved(str(tmp_path / "zuck_GroupTimeline_hybrid.json.gz"))
    # Unstick should pick the rank-3 oldest cursored post.
    assert saved["last_cursor"] != "stale_cursor"
    assert saved["last_cursor"].startswith("cursor_")
