"""Unit tests for the pluggable stop condition framework.

Each condition is exercised with handcrafted `StopState` fixtures, asserting
the returned result-string (or `None`). Dump-emitting conditions
(`CursorReset`, `GraphQLError`) are exercised with `tmp_path` to verify the
dump artifacts land where they should without polluting the repo.

`assemble_default_stop_conditions` is also covered: the three default
matrix cells (chronological-by-default endpoints, GroupTimeline+CHRONO,
GroupTimeline+non-CHRONO) produce the documented condition sets.
"""

import json
import os
from collections import deque

import pytest

from fbscrape.stop_conditions import (
    StopState,
    MaxPaginations,
    MaxPostsReached,
    NoNewPostsStreak,
    EndOfFeed,
    OldestInBatchBelowStartDate,
    ResponseShapeError,
    CursorReset,
    GraphQLError,
    ConsecutiveOutOfRange,
    assemble_default_stop_conditions,
    HYBRID_CURSOR_RESET_JUMP_SECONDS,
    HYBRID_CURSOR_RESET_WINDOW,
)


# ============================================================================
# MaxPaginations
# ============================================================================

def test_max_paginations_disabled_when_negative():
    cond = MaxPaginations(max_paginations=-1)
    assert cond.evaluate(StopState(iter_index=1_000_000)) is None


def test_max_paginations_fires_at_cap():
    cond = MaxPaginations(max_paginations=10)
    assert cond.evaluate(StopState(iter_index=9)) is None
    result = cond.evaluate(StopState(iter_index=10))
    assert result == 'hit max_paginations cap (10)'


# ============================================================================
# MaxPostsReached
# ============================================================================

def test_max_posts_disabled_when_negative():
    cond = MaxPostsReached(max_posts=-1)
    assert cond.evaluate(StopState(all_posts_count=10_000)) is None


def test_max_posts_fires_at_cap():
    cond = MaxPostsReached(max_posts=100)
    assert cond.evaluate(StopState(all_posts_count=99)) is None
    assert cond.evaluate(StopState(all_posts_count=100)) == 'max_posts_reached'
    assert cond.evaluate(StopState(all_posts_count=101)) == 'max_posts_reached'


# ============================================================================
# NoNewPostsStreak
# ============================================================================

def test_no_new_posts_streak_below_threshold_continues():
    cond = NoNewPostsStreak(max_no_progress_streak=5)
    assert cond.evaluate(StopState(no_progress_streak=4)) is None


def test_no_new_posts_streak_at_threshold_fires():
    cond = NoNewPostsStreak(max_no_progress_streak=5)
    assert cond.evaluate(StopState(no_progress_streak=5)) == 'no_new_posts_streak'


# ============================================================================
# EndOfFeed
# ============================================================================

def test_end_of_feed_with_cursor_continues():
    cond = EndOfFeed()
    assert cond.evaluate(StopState(end_cursor="abc123")) is None


def test_end_of_feed_null_cursor_fires():
    cond = EndOfFeed()
    assert (
        cond.evaluate(StopState(end_cursor=None))
        == 'scraped until user-specified starting date was reached'
    )
    assert (
        cond.evaluate(StopState(end_cursor=""))
        == 'scraped until user-specified starting date was reached'
    )


# ============================================================================
# OldestInBatchBelowStartDate
# ============================================================================

def test_oldest_below_start_with_no_start_unix_continues():
    cond = OldestInBatchBelowStartDate()
    assert cond.evaluate(
        StopState(start_unix=None, oldest_in_batch=1_700_000_000, cursor_sent="x")
    ) is None


def test_oldest_below_start_exempt_on_iter_1():
    """Bootstrap-edge guard: cursor_sent=None means iter 1; skip the stop.

    Reason: FB injects a "highlight" post into the first batch's bootstrap
    edge that's frequently out of chronological order. Tripping the date-
    stop on iter 1 would terminate scrapes prematurely.
    """
    cond = OldestInBatchBelowStartDate()
    # oldest_in_batch is WAY older than start_unix — but cursor_sent is None
    state = StopState(
        start_unix=1_700_000_000,
        oldest_in_batch=1_000_000_000,
        cursor_sent=None,
    )
    assert cond.evaluate(state) is None


def test_oldest_below_start_fires_on_iter_2_plus():
    cond = OldestInBatchBelowStartDate()
    state = StopState(
        start_unix=1_700_000_000,
        oldest_in_batch=1_600_000_000,  # before start_unix
        cursor_sent="anchor-from-iter-1",
    )
    assert (
        cond.evaluate(state)
        == 'scraped until user-specified starting date was reached'
    )


def test_oldest_below_start_does_not_fire_when_in_range():
    cond = OldestInBatchBelowStartDate()
    state = StopState(
        start_unix=1_700_000_000,
        oldest_in_batch=1_800_000_000,  # after start_unix
        cursor_sent="x",
    )
    assert cond.evaluate(state) is None


# ============================================================================
# ResponseShapeError
# ============================================================================

def test_response_shape_error_fires_when_posts_have_no_timestamps():
    cond = ResponseShapeError()
    state = StopState(posts_in_resp=3, oldest_in_batch=None)
    assert cond.evaluate(state) == 'response_shape_error'


def test_response_shape_error_quiet_on_empty_batch():
    cond = ResponseShapeError()
    state = StopState(posts_in_resp=0, oldest_in_batch=None)
    assert cond.evaluate(state) is None


def test_response_shape_error_quiet_when_timestamps_present():
    cond = ResponseShapeError()
    state = StopState(posts_in_resp=3, oldest_in_batch=1_700_000_000)
    assert cond.evaluate(state) is None


# ============================================================================
# CursorReset — stateful, endpoint-aware anchor
# ============================================================================

def test_cursor_reset_first_iter_just_records_anchor():
    cond = CursorReset()
    state = StopState(endpoint="UserTimeline", oldest_in_batch=1_700_000_000)
    assert cond.evaluate(state) is None
    assert cond.prev_anchor == 1_700_000_000


def test_cursor_reset_monotonic_descent_does_not_fire():
    cond = CursorReset()
    base = 1_700_000_000
    cond.evaluate(StopState(endpoint="UserTimeline", oldest_in_batch=base))
    # next batch goes older (descending) — exactly what we want
    assert cond.evaluate(
        StopState(endpoint="UserTimeline", oldest_in_batch=base - 86400)
    ) is None


def test_cursor_reset_fires_on_large_newward_jump(tmp_path):
    cond = CursorReset(dump_root=str(tmp_path / "cursor_reset"))
    base = 1_700_000_000
    cond.evaluate(StopState(endpoint="UserTimeline", oldest_in_batch=base))
    # Jump newer by 10 days > 7-day threshold
    state = StopState(
        endpoint="UserTimeline",
        oldest_in_batch=base + 10 * 86400,
        iter_index=42,
        label="testhandle",
        iter_window=deque([{"pagination_index": 1, "ts": "x"}]),
    )
    assert cond.evaluate(state) == 'cursor_reset'
    # Dump landed
    handle_dir = tmp_path / "cursor_reset" / "testhandle"
    assert handle_dir.exists()
    dumps = list(handle_dir.iterdir())
    assert len(dumps) == 1
    summary = json.load(open(dumps[0] / "summary.json"))
    assert summary["trigger_pagination"] == 42
    assert summary["jump_seconds"] == 10 * 86400


def test_cursor_reset_grouptimeline_uses_second_oldest_anchor(tmp_path):
    """GroupTimeline's bootstrap-edge highlight outlier would false-trip
    absolute oldest. The detector uses 2nd-oldest as the anchor instead.
    """
    cond = CursorReset(dump_root=str(tmp_path / "cursor_reset"))
    # Iter 1: anchor on 2nd_oldest (= 1700M), not the highlight outlier (= 1600M)
    cond.evaluate(StopState(
        endpoint="GroupTimeline",
        oldest_in_batch=1_600_000_000,         # would-be false anchor
        second_oldest_in_batch=1_700_000_000,  # real anchor
    ))
    assert cond.prev_anchor == 1_700_000_000

    # Iter 2: 2nd_oldest descends naturally — no trigger
    assert cond.evaluate(StopState(
        endpoint="GroupTimeline",
        oldest_in_batch=1_500_000_000,
        second_oldest_in_batch=1_700_000_000 - 86400,
    )) is None


def test_cursor_reset_grouptimeline_falls_back_when_second_oldest_missing():
    """Single-timed-post batches fall back to absolute oldest."""
    cond = CursorReset()
    state = StopState(
        endpoint="GroupTimeline",
        oldest_in_batch=1_700_000_000,
        second_oldest_in_batch=None,
    )
    assert cond.evaluate(state) is None
    assert cond.prev_anchor == 1_700_000_000


# ============================================================================
# GraphQLError — dump-emitting
# ============================================================================

def test_graphql_error_no_error_continues():
    cond = GraphQLError()
    assert cond.evaluate(StopState(graphql_error_detail=None)) is None


def test_graphql_error_fires_and_dumps(tmp_path):
    cond = GraphQLError(dump_root=str(tmp_path / "graphql_error"))
    err = {"message": "A server error occurred", "code": None, "severity": "ERROR"}
    state = StopState(
        label="alice",
        graphql_error_detail=err,
        iter_index=7,
        response_text='{"errors":[{"message":"A server error occurred"}]}',
        request_body="x=1",
        iter_window=deque([{"pagination_index": 5}]),
    )
    result = cond.evaluate(state)
    assert result == 'graphql_error: A server error occurred'
    handle_dir = tmp_path / "graphql_error" / "alice"
    assert handle_dir.exists()
    dump_dir = next(handle_dir.iterdir())
    summary = json.load(open(dump_dir / "summary.json"))
    assert summary["error"] == err
    assert summary["trigger_pagination"] == 7
    # window.jsonl carries prior iter(s) + the current (errored) iter
    lines = (dump_dir / "window.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2  # one prior + one current


# ============================================================================
# ConsecutiveOutOfRange
# ============================================================================

def test_consecutive_out_of_range_disabled_when_zero_or_neg():
    for n in [-1, 0]:
        cond = ConsecutiveOutOfRange(max_streak=n)
        # Even with all out-of-range posts, never fires when disabled.
        state = StopState(
            start_unix=1_700_000_000,
            end_unix=1_701_000_000,
            batch_creation_times=[1_500_000_000, 1_500_000_001, 1_500_000_002],
        )
        assert cond.evaluate(state) is None


def test_consecutive_out_of_range_resets_on_in_range_post():
    cond = ConsecutiveOutOfRange(max_streak=3)
    # 2 out, then 1 in-range — streak resets. Then 2 more out: still under cap.
    state = StopState(
        start_unix=1_700_000_000,
        end_unix=1_701_000_000,
        batch_creation_times=[
            1_500_000_000,  # below start → out
            1_500_000_001,  # below start → out (streak=2)
            1_700_500_000,  # in range → reset
            1_500_000_002,  # out (streak=1)
            1_500_000_003,  # out (streak=2)
        ],
    )
    assert cond.evaluate(state) is None
    assert cond.streak == 2


def test_consecutive_out_of_range_fires_at_threshold():
    cond = ConsecutiveOutOfRange(max_streak=3)
    state = StopState(
        start_unix=1_700_000_000,
        end_unix=1_701_000_000,
        batch_creation_times=[
            1_500_000_000,  # out (streak=1)
            1_500_000_001,  # out (streak=2)
            1_500_000_002,  # out (streak=3) → fires
            1_700_500_000,  # would-be in-range, never reached
        ],
    )
    assert cond.evaluate(state) == 'consecutive_out_of_range'


def test_consecutive_out_of_range_carries_streak_across_batches():
    """The streak isn't reset between iterations — only on encountering an
    in-range post. This is the whole point of the condition for non-chronological
    sorts where stale tails span batch boundaries.
    """
    cond = ConsecutiveOutOfRange(max_streak=4)
    # Batch 1: 2 out
    state1 = StopState(
        start_unix=1_700_000_000,
        end_unix=1_701_000_000,
        batch_creation_times=[1_500_000_000, 1_500_000_001],
    )
    assert cond.evaluate(state1) is None
    assert cond.streak == 2
    # Batch 2: 2 more out → fires
    state2 = StopState(
        start_unix=1_700_000_000,
        end_unix=1_701_000_000,
        batch_creation_times=[1_500_000_002, 1_500_000_003],
    )
    assert cond.evaluate(state2) == 'consecutive_out_of_range'


def test_consecutive_out_of_range_open_upper_bound():
    """When end_unix is None, posts newer than start_unix all count as in-range."""
    cond = ConsecutiveOutOfRange(max_streak=2)
    state = StopState(
        start_unix=1_700_000_000,
        end_unix=None,
        batch_creation_times=[
            1_500_000_000,  # below start → out (streak=1)
            9_999_999_999,  # far future → in range (since end_unix is None) → reset
        ],
    )
    assert cond.evaluate(state) is None
    assert cond.streak == 0


def test_consecutive_out_of_range_above_end_unix_is_out_of_range():
    cond = ConsecutiveOutOfRange(max_streak=2)
    state = StopState(
        start_unix=1_700_000_000,
        end_unix=1_701_000_000,
        batch_creation_times=[
            1_900_000_000,  # above end → out (streak=1)
            1_900_000_001,  # above end → out (streak=2) → fires
        ],
    )
    assert cond.evaluate(state) == 'consecutive_out_of_range'


# ============================================================================
# assemble_default_stop_conditions — the matrix cells
# ============================================================================

def _names(conds):
    return [type(c).__name__ for c in conds]


def test_assemble_user_timeline_default_set():
    """UserTimeline + hybrid (no sort param): chronological set, no
    ConsecutiveOutOfRange unless explicitly enabled."""
    conds = assemble_default_stop_conditions(
        endpoint="UserTimeline", mode="hybrid", sorting_setting=None,
        params={
            "max_no_progress_streak": 5,
            "max_paginations": -1,
            "max_posts": -1,
        },
    )
    names = _names(conds)
    assert "OldestInBatchBelowStartDate" in names
    assert "CursorReset" in names
    assert "ConsecutiveOutOfRange" not in names


def test_assemble_grouptimeline_chronological_includes_both_date_stops():
    """Belt-and-suspenders: CHRONOLOGICAL keeps the chronological stops
    AND gains ConsecutiveOutOfRange against bootstrap-edge highlights."""
    conds = assemble_default_stop_conditions(
        endpoint="GroupTimeline", mode="hybrid", sorting_setting="CHRONOLOGICAL",
        params={
            "max_no_progress_streak": 30,
            "max_paginations": -1,
            "max_posts": -1,
            "max_consecutive_out_of_range": 20,
        },
    )
    names = _names(conds)
    assert "OldestInBatchBelowStartDate" in names
    assert "CursorReset" in names
    assert "ConsecutiveOutOfRange" in names


def test_assemble_grouptimeline_top_posts_drops_chronological_stops():
    """TOP_POSTS: drop OldestInBatchBelowStartDate (unreliable) and
    CursorReset (premise doesn't hold). Keep ConsecutiveOutOfRange as the
    primary date-tail stop."""
    conds = assemble_default_stop_conditions(
        endpoint="GroupTimeline", mode="hybrid", sorting_setting="TOP_POSTS",
        params={
            "max_no_progress_streak": 30,
            "max_paginations": -1,
            "max_posts": -1,
            "max_consecutive_out_of_range": 20,
        },
    )
    names = _names(conds)
    assert "OldestInBatchBelowStartDate" not in names
    assert "CursorReset" not in names
    assert "ConsecutiveOutOfRange" in names


def test_assemble_grouptimeline_recent_activity_treated_as_non_chronological():
    conds = assemble_default_stop_conditions(
        endpoint="GroupTimeline", mode="hybrid", sorting_setting="RECENT_ACTIVITY",
        params={
            "max_no_progress_streak": 30,
            "max_paginations": -1,
            "max_posts": -1,
            "max_consecutive_out_of_range": 20,
        },
    )
    names = _names(conds)
    assert "OldestInBatchBelowStartDate" not in names
    assert "CursorReset" not in names


def test_assemble_consecutive_out_of_range_omitted_when_disabled():
    conds = assemble_default_stop_conditions(
        endpoint="GroupTimeline", mode="hybrid", sorting_setting="TOP_POSTS",
        params={
            "max_no_progress_streak": 30,
            "max_paginations": -1,
            "max_posts": -1,
            "max_consecutive_out_of_range": -1,
        },
    )
    assert "ConsecutiveOutOfRange" not in _names(conds)


def test_assemble_search_has_no_date_stops():
    """Search results are not strictly chronological within the date-filtered
    range, so both date-exit conditions are excluded. Only exhaustion/capacity
    stops apply."""
    conds = assemble_default_stop_conditions(
        endpoint="Search", mode="hybrid", sorting_setting=None,
        params={
            "max_no_progress_streak": 5,
            "max_paginations": -1,
            "max_posts": -1,
        },
    )
    names = _names(conds)
    assert "OldestInBatchBelowStartDate" not in names
    assert "CursorReset" not in names


def test_assemble_default_set_universal_stops():
    """Every assembly carries the universal stops regardless of endpoint/sort."""
    for endpoint, sort in [
        ("UserTimeline", None),
        ("Search", None),
        ("GroupTimeline", "TOP_POSTS"),
        ("GroupTimeline", "CHRONOLOGICAL"),
    ]:
        conds = assemble_default_stop_conditions(
            endpoint=endpoint, mode="hybrid", sorting_setting=sort,
            params={
                "max_no_progress_streak": 5,
                "max_paginations": -1,
                "max_posts": -1,
            },
        )
        names = set(_names(conds))
        assert {
            "GraphQLError", "EndOfFeed", "NoNewPostsStreak",
            "MaxPostsReached", "ResponseShapeError", "MaxPaginations",
        }.issubset(names), f"missing universal stops for ({endpoint}, {sort}): {names}"
