"""Resume-state sidecar — a tiny companion file next to each saved scrape
output that lets `--continue` recover resume state without re-parsing the
multi-hundred-MB posts payload.

`scraper._stream_resume_state` ijson-walks the *entire* gzipped posts array of
a prior run just to recover two small things: the resume `last_cursor` and the
recent `post_id`s used to seed cross-leg dedup. On a slow host that read takes
minutes per file and (being GIL-bound) starves the event loop so browsers can't
even open until it finishes. The sidecar persists exactly that derived state at
save time — both values are already in memory — so the resume read becomes an
O(1) load of a KB-sized file instead of an O(file-size) parse.

Format (`<stem>.resume.json`):

    {
      "schema": 1,
      "endpoint": "UserTimeline",
      "cursors": ["<last_cursor>", ...up to 99 per-post fallback cursors...],
      "post_ids": [...last 150, node.post_id||post_id precedence...],
      "post_count": <int>,
      "source_size": <bytes of the main file>,
      "source_mtime": <float mtime of the main file>
    }

`source_size` / `source_mtime` are the consistency validator: a reader trusts
the sidecar only if `os.stat(main_file)` still matches, so any out-of-band edit
of the main file silently invalidates it and the reader falls back to the full
parse. See Key Design Decision 24.

This module is the single source of truth for the format. It must stay in sync
with `tmp/backfill_resume_sidecars.py` (the corpus-retrofit tool, which derives
the same payload via a streaming ijson pass instead of an in-memory result).
"""
from __future__ import annotations

import json
import os
import tempfile

SCHEMA_VERSION = 1

# The seeded dedup set must cover FB's worst-case re-serve window on resume,
# else the NoNewPostsStreak backstop can't fire (re-served posts look like
# progress) and the merged file accretes duplicates. That window is bounded by
# `max_no_progress_streak * pagination_count` — GroupTimeline's worst case is
# 30 * 3 = 90 — so 150 clears it with cushion. Raise if either registry default
# grows. See Key Design Decision 24.
MAX_POST_IDS = 150

# Resume head (`cursors[0]`, the only entry consumed today) plus up to 99
# per-post fallback cursors, stored for a future unstick-via-ladder feature.
MAX_CURSORS = 100

RESUMABLE_ENDPOINTS = frozenset({"UserTimeline", "GroupTimeline", "CommentsList"})


def sidecar_path(main_path: str) -> str:
    """`<stem>.json[.gz]` -> `<stem>.resume.json`. The one place the name is
    derived; reader, writer, and the backfill tool must agree on it."""
    for ext in (".json.gz", ".json"):
        if main_path.endswith(ext):
            return main_path[: -len(ext)] + ".resume.json"
    return main_path + ".resume.json"


def _post_id(rec: dict) -> str | None:
    """Same `node.post_id` -> top-level `post_id` precedence as
    `scraper._stream_resume_state`, so sidecar ids match the full-parse ids."""
    node = rec.get("node") or {}
    return node.get("post_id") or rec.get("post_id")


def _build_payload(result) -> dict:
    data = result.data or []
    post_ids: list[str] = []
    rec_cursors: list[str] = []
    for rec in data:
        pid = _post_id(rec)
        if pid:
            post_ids.append(pid)
        c = rec.get("cursor")
        if c:
            rec_cursors.append(c)

    head = result.last_cursor or ""
    cursors = [head]
    seen = {head}
    for c in rec_cursors[-MAX_CURSORS:]:
        if len(cursors) >= MAX_CURSORS:
            break
        if c not in seen:
            cursors.append(c)
            seen.add(c)

    return {
        "schema": SCHEMA_VERSION,
        "endpoint": result.query.endpoint,
        "cursors": cursors,
        "post_ids": post_ids[-MAX_POST_IDS:],
        "post_count": len(data),
    }


def write_sidecar(result, main_path: str) -> str | None:
    """Write `<stem>.resume.json` next to `main_path` (the just-saved scrape
    file) for resumable endpoints. No-op (returns None) for non-resumable
    endpoints. Atomic (tempfile + os.replace). Raises on IO error — callers
    treat sidecar failure as non-fatal (the full-parse fallback still works)."""
    if result.query.endpoint not in RESUMABLE_ENDPOINTS:
        return None
    payload = _build_payload(result)
    st = os.stat(main_path)
    payload["source_size"] = st.st_size
    payload["source_mtime"] = st.st_mtime

    sc_path = sidecar_path(main_path)
    directory = os.path.dirname(sc_path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, sc_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sc_path


def read_sidecar(main_path: str) -> tuple[str, list[str]] | None:
    """Return `(head_cursor, post_ids)` from a *current* sidecar, or None when
    it's missing / stale (size+mtime mismatch) / malformed — in which case the
    caller falls back to the full ijson parse. Mirrors the
    `_stream_resume_state` return contract (head cursor + post_id list)."""
    sc_path = sidecar_path(main_path)
    try:
        with open(sc_path) as f:
            d = json.load(f)
        st = os.stat(main_path)
    except (OSError, ValueError):
        return None
    if (
        d.get("schema") != SCHEMA_VERSION
        or d.get("source_size") != st.st_size
        or d.get("source_mtime") != st.st_mtime
    ):
        return None
    cursors = d.get("cursors") or [""]
    head = cursors[0] if cursors else ""
    return head, list(d.get("post_ids") or [])
