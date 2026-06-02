"""Pluggable stop conditions for the hybrid pagination loop.

`_hybrid_pagination_loop` builds a `StopState` per iter and walks an
ordered list of `StopCondition`s; the first to return a non-None string
terminates the loop. Auth errors and in-body rate-limits short-circuit
inline before the walk. Conditions are stateful, so build a fresh list
per scrape via `assemble_default_stop_conditions`.
"""
from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .logger import logger


HYBRID_CURSOR_RESET_WINDOW = 20
HYBRID_CURSOR_RESET_JUMP_SECONDS = 7 * 86400
HYBRID_CURSOR_RESET_DUMP_ROOT = "tmp/hybrid/cursor_reset"
HYBRID_GRAPHQL_ERROR_DUMP_ROOT = "tmp/hybrid/graphql_error"


# ============================================================================
# StopState — the per-iter snapshot passed to every condition.
# ============================================================================

@dataclass
class StopState:
    """Per-iter snapshot of loop state passed to each StopCondition."""
    # Identity
    label: str = ""
    endpoint: str = "UserTimeline"
    sorting_setting: str | None = None

    # Iteration counters
    iter_index: int = 0  # 1-based, equals total_paginations after increment

    # Cursors
    cursor_sent: str | None = None       # what we just sent (None when bootstrap-fresh)
    end_cursor: str | None = None        # what FB sent back (None = end of feed)

    # Date bounds
    start_unix: int | None = None
    end_unix: int | None = None

    # Batch contents
    batch_creation_times: list[int] = field(default_factory=list)
    oldest_in_batch: int | None = None
    newest_in_batch: int | None = None
    second_oldest_in_batch: int | None = None
    posts_in_resp: int = 0
    new_posts_in_iter: int = 0
    all_posts_count: int = 0
    no_progress_streak: int = 0

    # Diagnostic / dump context (used by CursorReset, GraphQLError)
    response_text: str = ""
    request_body: str = ""
    template_headers: dict = field(default_factory=dict)
    response_status: int = 200
    iter_start_iso: str = ""
    elapsed_s: float = 0.0
    cursor_sent_fp: str = "null"
    cursor_recv_fp: str = "null"
    csr_len: int = 0
    dyn_len: int = 0

    # Rolling window of prior iters (loop appends; conditions read for dumps).
    iter_window: deque = field(
        default_factory=lambda: deque(maxlen=HYBRID_CURSOR_RESET_WINDOW)
    )

    # In-body GraphQL error object (`{message, code, severity}`) or None.
    # Auth and rate-limit cases are short-circuited BEFORE the framework
    # walk; if this is non-None here it's a "generic" error to dump+bail on.
    graphql_error_detail: dict | None = None


# ============================================================================
# StopCondition protocol + concrete conditions.
# ============================================================================

class StopCondition(Protocol):
    """Return a result-string to terminate the loop, or None to continue."""

    def evaluate(self, state: StopState) -> str | None: ...


class MaxPaginations:
    """Bail when iter count >= max_paginations. -1 disables."""

    def __init__(self, max_paginations: int):
        self.max_paginations = max_paginations

    def evaluate(self, state: StopState) -> str | None:
        if self.max_paginations < 0:
            return None
        if state.iter_index >= self.max_paginations:
            logger.warning(
                f"[hybrid] @{state.label}: hit max_paginations cap "
                f"({self.max_paginations})"
            )
            return f'hit max_paginations cap ({self.max_paginations})'
        return None


class MaxPostsReached:
    """Bail when accumulated post count >= max_posts. -1 disables.

    Batch-boundary; may overshoot by up to pagination_count-1.
    """

    def __init__(self, max_posts: int):
        self.max_posts = max_posts

    def evaluate(self, state: StopState) -> str | None:
        if self.max_posts < 0:
            return None
        if state.all_posts_count >= self.max_posts:
            logger.info(
                f"[hybrid] @{state.label}: reached max_posts cap "
                f"({state.all_posts_count} >= {self.max_posts}) after "
                f"{state.iter_index} paginations — done"
            )
            return 'max_posts_reached'
        return None


class NoNewPostsStreak:
    """Bail after N consecutive paginations with zero new posts."""

    def __init__(self, max_no_progress_streak: int):
        self.max_no_progress_streak = max_no_progress_streak

    def evaluate(self, state: StopState) -> str | None:
        if state.no_progress_streak >= self.max_no_progress_streak:
            logger.warning(
                f"[hybrid] @{state.label}: {state.no_progress_streak} "
                f"paginations with no new posts — bailing"
            )
            return 'no_new_posts_streak'
        return None


class EndOfFeed:
    """Bail when FB returns a null `end_cursor`."""

    def evaluate(self, state: StopState) -> str | None:
        if not state.end_cursor:
            logger.info(
                f"[hybrid] @{state.label}: end_cursor null after "
                f"{state.iter_index} paginations — end of feed within filter range"
            )
            return 'scraped until user-specified starting date was reached'
        return None


class OldestInBatchBelowStartDate:
    """Bail when oldest post in batch is older than `start_unix`.

    Skips the bootstrap iter (cursor_sent is None) — the first batch's
    edge can carry an out-of-order "highlight" post. See CLAUDE.md KDD 16.
    """

    def evaluate(self, state: StopState) -> str | None:
        if state.start_unix is None:
            return None
        if state.cursor_sent is None:  # bootstrap iter — exempt
            return None
        if state.oldest_in_batch is None:
            return None
        if state.oldest_in_batch < state.start_unix:
            oldest_iso = datetime.fromtimestamp(
                state.oldest_in_batch, tz=timezone.utc
            ).isoformat()
            logger.info(
                f"[hybrid] @{state.label}: oldest post in batch "
                f"({oldest_iso}) is older than start; done"
            )
            return 'scraped until user-specified starting date was reached'
        return None


class ResponseShapeError:
    """Bail when posts parsed but no creation_times extracted.

    Signals an unknown metadata-strategy typename — terminal, non-retryable.
    """

    def evaluate(self, state: StopState) -> str | None:
        if state.posts_in_resp > 0 and state.oldest_in_batch is None:
            logger.error(
                f"[hybrid] @{state.label}: parsed {state.posts_in_resp} "
                f"posts but extracted 0 creation_times at pagination "
                f"{state.iter_index} — unknown metadata-strategy "
                f"typename(s). Aborting (no retry)."
            )
            return 'response_shape_error'
        return None


class CursorReset:
    """Bail when the per-batch anchor jumps newer by > jump_seconds.

    GroupTimeline anchors on 2nd-oldest (its bootstrap edge carries a
    periodic out-of-order outlier); other endpoints use absolute oldest.
    Dumps the rolling iter_window on trip. Chronological sorts only.
    """

    def __init__(
        self,
        jump_seconds: int = HYBRID_CURSOR_RESET_JUMP_SECONDS,
        dump_root: str = HYBRID_CURSOR_RESET_DUMP_ROOT,
    ):
        self.jump_seconds = jump_seconds
        self.dump_root = dump_root
        self.prev_anchor: int | None = None

    @staticmethod
    def _current_anchor(state: StopState) -> tuple[int | None, str]:
        if state.endpoint == "GroupTimeline":
            return (
                state.second_oldest_in_batch or state.oldest_in_batch,
                "2nd_oldest",
            )
        return state.oldest_in_batch, "oldest"

    def evaluate(self, state: StopState) -> str | None:
        cur_anchor, anchor_label = self._current_anchor(state)
        if (
            self.prev_anchor is not None
            and cur_anchor is not None
            and (cur_anchor - self.prev_anchor) > self.jump_seconds
        ):
            jump_days = (cur_anchor - self.prev_anchor) / 86400.0
            out_dir = dump_cursor_reset_window(
                label=state.label,
                trigger_index=state.iter_index,
                prev_oldest_unix=self.prev_anchor,
                cur_oldest_unix=cur_anchor,
                window=state.iter_window,
                dump_root=self.dump_root,
            )
            logger.warning(
                f"[hybrid] @{state.label}: cursor-reset detected at "
                f"pagination {state.iter_index} ({anchor_label} jumped "
                f"+{jump_days:.1f} days) — dumped window to "
                f"{out_dir or '<dump_failed>'}; bailing with partial posts"
            )
            return 'cursor_reset'
        if cur_anchor is not None:
            self.prev_anchor = cur_anchor
        return None


class GraphQLError:
    """Bail on non-auth, non-rate-limit in-body GraphQL `errors[]`.

    Dumps the rolling iter_window + the errored iter. Auth and rate-limit
    cases are short-circuited inline before the framework walk.
    """

    def __init__(self, dump_root: str = HYBRID_GRAPHQL_ERROR_DUMP_ROOT):
        self.dump_root = dump_root

    def evaluate(self, state: StopState) -> str | None:
        err = state.graphql_error_detail
        if not err:
            return None

        # only bail if we didn't collect any posts and the end_cursor is None
        if (state.posts_in_resp and state.posts_in_resp > 0) and state.end_cursor:
            logger.warning(
                f"[hybrid] @{state.label}: graphql side-fragment error detected bu tolerated "
                f"(posts={state.posts_in_resp}, end_cursor={state.end_cursor}): "
                f"{err.get('message')}"
            )
            return None

        gql_msg = err.get("message", "")
        current_iter = {
            "pagination_index": state.iter_index,
            "ts": state.iter_start_iso,
            "status": state.response_status,
            "request": {
                "body": state.request_body,
                "headers": state.template_headers,
            },
            "response": {
                "text": state.response_text,
                "size_bytes": len(state.response_text or ""),
            },
        }
        out_dir = dump_graphql_error_window(
            label=state.label,
            trigger_index=state.iter_index,
            error_detail=err,
            window=state.iter_window,
            current_iter=current_iter,
            dump_root=self.dump_root,
        )
        logger.warning(
            f"[hybrid] @{state.label}: graphql_error dumped window to "
            f"{out_dir or '<dump_failed>'}"
        )
        return f'graphql_error: {gql_msg}'


class ConsecutiveOutOfRange:
    """Bail after N posts in a row fall outside `[start_unix, end_unix]`.

    For non-chronological sorts where `OldestInBatchBelowStartDate` is
    unreliable. Counts per-post across batch boundaries; untimed posts
    are skipped (neither reset nor increment).
    """

    def __init__(self, max_streak: int):
        self.max_streak = max_streak
        self.streak = 0

    def evaluate(self, state: StopState) -> str | None:
        if self.max_streak <= 0:  # disabled
            return None
        for ct in state.batch_creation_times:
            in_range = (
                (state.start_unix is None or ct >= state.start_unix)
                and (state.end_unix is None or ct <= state.end_unix)
            )
            if in_range:
                self.streak = 0
            else:
                self.streak += 1
                if self.streak >= self.max_streak:
                    logger.info(
                        f"[hybrid] @{state.label}: {self.streak} "
                        f"consecutive out-of-range posts (limit "
                        f"{self.max_streak}) at pagination "
                        f"{state.iter_index} — bailing"
                    )
                    return 'consecutive_out_of_range'
        return None


# ============================================================================
# Diagnostic dump helpers — module-level so conditions don't depend on
# BrowserSession. Both write to `<dump_root>/<safe_label>/<UTC_ts>/`.
# Failures are logged, never raised.
# ============================================================================

def dump_cursor_reset_window(
    label: str,
    trigger_index: int,
    prev_oldest_unix: int | None,
    cur_oldest_unix: int | None,
    window: deque,
    dump_root: str = HYBRID_CURSOR_RESET_DUMP_ROOT,
) -> str | None:
    """Persist the rolling iter window to `<dump_root>/<label>/<UTC_ts>/`."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = label.lstrip("@") or "unknown"
    out_dir = os.path.join(dump_root, safe_label, ts)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "window.jsonl"), "w") as f:
            for rec in window:
                f.write(json.dumps(rec) + "\n")
        summary = {
            "label": label,
            "trigger_pagination": trigger_index,
            "prev_oldest_unix": prev_oldest_unix,
            "cur_oldest_unix": cur_oldest_unix,
            "prev_oldest_iso": (
                datetime.fromtimestamp(prev_oldest_unix, tz=timezone.utc).isoformat()
                if prev_oldest_unix is not None else None
            ),
            "cur_oldest_iso": (
                datetime.fromtimestamp(cur_oldest_unix, tz=timezone.utc).isoformat()
                if cur_oldest_unix is not None else None
            ),
            "jump_seconds": (
                cur_oldest_unix - prev_oldest_unix
                if (prev_oldest_unix is not None and cur_oldest_unix is not None)
                else None
            ),
            "window_size": len(window),
            "dumped_at": ts,
        }
        with open(os.path.join(out_dir, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        return out_dir
    except Exception as e:
        logger.warning(f"[hybrid] @{label}: cursor-reset dump failed: {e}")
        return None


def dump_graphql_error_window(
    label: str,
    trigger_index: int,
    error_detail: dict,
    window: deque,
    current_iter: dict | None = None,
    dump_root: str = HYBRID_GRAPHQL_ERROR_DUMP_ROOT,
) -> str | None:
    """Persist the rolling window + errored iter to `<dump_root>/<label>/<UTC_ts>/`."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = label.lstrip("@") or "unknown"
    out_dir = os.path.join(dump_root, safe_label, ts)
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "window.jsonl"), "w") as f:
            for rec in window:
                f.write(json.dumps(rec) + "\n")
            if current_iter is not None:
                f.write(json.dumps(current_iter) + "\n")
        summary = {
            "label": label,
            "trigger_pagination": trigger_index,
            "error": error_detail,
            "window_size": len(window) + (1 if current_iter is not None else 0),
            "dumped_at": ts,
        }
        with open(os.path.join(out_dir, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        return out_dir
    except Exception as e:
        logger.warning(f"[hybrid] @{label}: graphql-error dump failed: {e}")
        return None


# ============================================================================
# Default condition assembly.
# ============================================================================

def assemble_default_stop_conditions(
    endpoint: str,
    mode: str,
    sorting_setting: str | None,
    params: dict,
) -> list[StopCondition]:
    """Build the canonical stop-condition list for an (endpoint, mode, sort).

    Always: `GraphQLError`, `EndOfFeed`, `NoNewPostsStreak`,
    `MaxPostsReached`, `ResponseShapeError`, `MaxPaginations`.
    Chronological sorts add `OldestInBatchBelowStartDate` + `CursorReset`.
    `ConsecutiveOutOfRange` is opt-in via `params['max_consecutive_out_of_range']`.
    Order matters — `GraphQLError` runs first so forensics dump before
    anything else interprets the response.

    `CommentsList` is exhaustion-only with optional max_results — it skips
    every date-bound and Story-shape-specific condition.
    """
    # CommentsList: exhaustion-only + optional max cap. No date semantics
    # (comments are non-chronological), no Story-shape parsing (so
    # `ResponseShapeError`'s premise doesn't hold), no cursor-reset (no
    # chronological monotonicity to anchor on).
    if endpoint == "CommentsList":
        return [
            GraphQLError(),
            EndOfFeed(),
            NoNewPostsStreak(params["max_no_progress_streak"]),
            MaxPostsReached(params.get("max_posts", -1)),
            MaxPaginations(params["max_paginations"]),
        ]

    is_chronological = (
        endpoint in ("UserTimeline", "Search")
        or (endpoint == "GroupTimeline" and sorting_setting == "CHRONOLOGICAL")
    )

    conditions: list[StopCondition] = []

    conditions.append(GraphQLError())
    conditions.append(EndOfFeed())

    if is_chronological:
        conditions.append(OldestInBatchBelowStartDate())

    max_consecutive = params.get("max_consecutive_out_of_range", -1)
    if isinstance(max_consecutive, int) and max_consecutive > 0:
        conditions.append(ConsecutiveOutOfRange(max_consecutive))

    conditions.append(NoNewPostsStreak(params["max_no_progress_streak"]))
    conditions.append(MaxPostsReached(params.get("max_posts", -1)))
    conditions.append(ResponseShapeError())

    if is_chronological:
        conditions.append(CursorReset())

    conditions.append(MaxPaginations(params["max_paginations"]))

    return conditions
