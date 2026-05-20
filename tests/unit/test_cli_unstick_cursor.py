"""Unit tests for `_find_unstick_cursor` and the `fbscrape unstick-cursor` CLI.

The helper is the load-bearing piece — both the explicit CLI subcommand and
the auto-fallback in `scrape group-timeline` / `scrape user-timeline` route
through it. Verifies:

  - picks the rank-th chronologically-oldest cursored post
  - skips cursorless posts ahead of the target rank (bootstrap-edge artifact)
  - returns None when the file is too small / has no cursored posts deep enough
  - the CLI subcommand round-trips (load → unstick → save → load again)
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fbscrape.cli import _find_unstick_cursor, cli


def _post(post_id: str, created_at: int, cursor: str | None) -> dict:
    """Mint a minimal ScrapingResult.data entry shaped like the parser emits.

    Includes the `comet_sections.context_layout.story.comet_sections.metadata`
    nesting that `FacebookGraphQLParser._extract_times` walks to pull
    `creation_time` out — otherwise `_find_unstick_cursor`'s call to
    `parser.flatten(...)` would return created_at=None and the post would be
    invisible to the ranker.
    """
    rec: dict = {
        "node": {
            "__typename": "Story",
            "__isFeedUnit": "Story",
            "post_id": post_id,
            "comet_sections": {
                "context_layout": {
                    "story": {
                        "comet_sections": {
                            "metadata": [
                                {
                                    "__typename": "CometFeedStoryLongerTimestampStrategy",
                                    "story": {"creation_time": created_at},
                                },
                            ],
                        },
                    },
                },
            },
        },
    }
    if cursor is not None:
        rec["cursor"] = cursor
    return rec


def test_picks_third_oldest_cursored_post_by_default():
    """Rank=3 (default) returns the 3rd-chronologically-oldest post's cursor."""
    # Five posts, oldest → newest. All have cursors.
    data = [
        _post("p1", 1_000, "cursor-1"),  # oldest
        _post("p2", 2_000, "cursor-2"),
        _post("p3", 3_000, "cursor-3"),  # ← rank-3, target
        _post("p4", 4_000, "cursor-4"),
        _post("p5", 5_000, "cursor-5"),  # newest
    ]
    result = _find_unstick_cursor(data, endpoint="GroupTimeline")
    assert result is not None
    cursor, diag = result
    assert cursor == "cursor-3"
    assert diag["chosen_rank"] == 3


def test_walks_forward_when_rank_target_lacks_cursor():
    """When the rank-th post is cursorless (bootstrap-edge artifact), walks
    forward chronologically to the next cursored post and reports the
    actual rank it landed on."""
    data = [
        _post("p1", 1_000, "cursor-1"),
        _post("p2", 2_000, "cursor-2"),
        _post("p3", 3_000, None),        # rank-3 cursorless
        _post("p4", 4_000, None),        # rank-4 cursorless too
        _post("p5", 5_000, "cursor-5"),  # ← rank-5, first cursored ≥ rank-3
    ]
    result = _find_unstick_cursor(data, endpoint="GroupTimeline")
    assert result is not None
    cursor, diag = result
    assert cursor == "cursor-5"
    assert diag["chosen_rank"] == 5


def test_returns_none_when_data_too_small_for_rank():
    """Fewer timestamped posts than the requested rank → None."""
    data = [
        _post("p1", 1_000, "cursor-1"),
        _post("p2", 2_000, "cursor-2"),
    ]
    assert _find_unstick_cursor(data, endpoint="GroupTimeline", rank=3) is None


def test_returns_none_when_no_cursored_post_deep_enough():
    """All posts beyond rank-3 are cursorless → None."""
    data = [
        _post("p1", 1_000, "cursor-1"),
        _post("p2", 2_000, "cursor-2"),
        _post("p3", 3_000, None),
        _post("p4", 4_000, None),
    ]
    assert _find_unstick_cursor(data, endpoint="GroupTimeline") is None


def test_skips_posts_with_no_creation_time():
    """Posts missing a parseable creation_time aren't ranked at all —
    they're dropped from the chronological order entirely."""
    data = [
        _post("p1", 1_000, "cursor-1"),
        # Missing creation_time entirely:
        {"node": {"__typename": "Story", "post_id": "broken"}, "cursor": "cursor-broken"},
        _post("p2", 2_000, "cursor-2"),
        _post("p3", 3_000, "cursor-3"),
    ]
    # The broken post shouldn't shift the ranking — rank-3 is still p3.
    result = _find_unstick_cursor(data, endpoint="GroupTimeline")
    assert result is not None
    cursor, diag = result
    assert cursor == "cursor-3"


def test_higher_rank_anchors_deeper():
    """rank=5 picks the 5th-chronologically-oldest, not the 3rd."""
    data = [_post(f"p{i}", 1_000 * i, f"cursor-{i}") for i in range(1, 8)]
    result = _find_unstick_cursor(data, endpoint="GroupTimeline", rank=5)
    assert result is not None
    cursor, diag = result
    assert cursor == "cursor-5"
    assert diag["chosen_rank"] == 5


def _write_scraping_result(path: Path, data: list[dict], last_cursor: str | None = None) -> None:
    """Build a minimal ScrapingResult-shaped JSON and gzip-save it."""
    payload = {
        "query": {
            "endpoint": "GroupTimeline",
            "mode": "hybrid",
            "query": {
                "handle": "testgroup",
                "start_date": "2020-01-01",
                "end_date": "2026-05-14",
            },
            "params": {},
        },
        "result": "no_new_posts_streak",
        "data": data,
        "time_started": "2026-05-15 00:00:00+00:00",
        "time_taken": "0:01:00",
        "last_cursor": last_cursor,
    }
    with gzip.open(path, "wt") as f:
        json.dump(payload, f)


def test_cli_subcommand_swaps_last_cursor(tmp_path):
    """End-to-end: unstick-cursor CLI loads a file, swaps last_cursor, saves
    back. After invocation the file's last_cursor is the rank-3 post's cursor."""
    data = [
        _post("p1", 1_000, "cursor-1"),
        _post("p2", 2_000, "cursor-2"),
        _post("p3", 3_000, "cursor-3"),
        _post("p4", 4_000, "cursor-4"),
        _post("p5", 5_000, "cursor-5"),
    ]
    fixture = tmp_path / "testgroup_GroupTimeline_hybrid_2020-01-01_2026-05-14.json.gz"
    _write_scraping_result(fixture, data, last_cursor="STUCK-CURSOR")

    runner = CliRunner()
    result = runner.invoke(cli, ["unstick-cursor", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "[FIXED]" in result.output

    with gzip.open(fixture, "rt") as f:
        after = json.load(f)
    assert after["last_cursor"] == "cursor-3"


def test_cli_subcommand_only_if_stuck_skips_clean_files(tmp_path):
    """--only-if-stuck must NOT modify files whose result != 'no_new_posts_streak'."""
    data = [_post(f"p{i}", 1_000 * i, f"cursor-{i}") for i in range(1, 6)]
    fixture = tmp_path / "testgroup_GroupTimeline_hybrid_2020-01-01_2026-05-14.json.gz"
    _write_scraping_result(fixture, data, last_cursor="ORIGINAL")
    # Override result to a clean exit
    with gzip.open(fixture, "rt") as f:
        payload = json.load(f)
    payload["result"] = "max_posts_reached"
    with gzip.open(fixture, "wt") as f:
        json.dump(payload, f)

    runner = CliRunner()
    result = runner.invoke(cli, ["unstick-cursor", "--only-if-stuck", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "[skip ]" in result.output

    with gzip.open(fixture, "rt") as f:
        after = json.load(f)
    assert after["last_cursor"] == "ORIGINAL"  # unchanged


def test_cli_subcommand_dry_run_does_not_write(tmp_path):
    """--dry-run reports the swap that would happen but doesn't modify the file."""
    data = [_post(f"p{i}", 1_000 * i, f"cursor-{i}") for i in range(1, 6)]
    fixture = tmp_path / "testgroup_GroupTimeline_hybrid_2020-01-01_2026-05-14.json.gz"
    _write_scraping_result(fixture, data, last_cursor="ORIGINAL")

    runner = CliRunner()
    result = runner.invoke(cli, ["unstick-cursor", "--dry-run", str(fixture)])
    assert result.exit_code == 0, result.output
    assert "[dry  ]" in result.output

    with gzip.open(fixture, "rt") as f:
        after = json.load(f)
    assert after["last_cursor"] == "ORIGINAL"  # unchanged
