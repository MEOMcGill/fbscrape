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

import gzip
import json
import os
import tempfile
from collections import deque
from pathlib import Path

import ijson

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


def _assemble_cursors(head: str, rec_cursors: list[str]) -> list[str]:
    """Resume head (`cursors[0]`) + the last `MAX_CURSORS - 1` distinct per-post
    cursors, in collection order. Shared by the in-memory and streaming builders
    so both produce an identical ladder."""
    cursors = [head]
    seen = {head}
    for c in rec_cursors[-MAX_CURSORS:]:
        if len(cursors) >= MAX_CURSORS:
            break
        if c not in seen:
            cursors.append(c)
            seen.add(c)
    return cursors


def _build_payload_from_result(result) -> dict:
    """Derive the sidecar payload from an in-memory ScrapingResult (the hot
    save path — `result.data` is already materialized)."""
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

    return {
        "schema": SCHEMA_VERSION,
        "endpoint": result.query.endpoint,
        "cursors": _assemble_cursors(result.last_cursor or "", rec_cursors),
        "post_ids": post_ids[-MAX_POST_IDS:],
        "post_count": len(data),
    }


def _atomic_write_json(path: str, obj: dict) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.chmod(tmp, 0o644)  # mkstemp is 0600; match the save() default
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_payload(payload: dict, main_path: str) -> str:
    """Stamp the size+mtime validator from `main_path` onto `payload` and write
    it atomically to the sidecar path. Returns the sidecar path."""
    st = os.stat(main_path)
    payload = {**payload, "source_size": st.st_size, "source_mtime": st.st_mtime}
    sc_path = sidecar_path(main_path)
    _atomic_write_json(sc_path, payload)
    return sc_path


def write_sidecar(result, main_path: str) -> str | None:
    """Write `<stem>.resume.json` next to `main_path` (the just-saved scrape
    file) for resumable endpoints. No-op (returns None) for non-resumable
    endpoints. Atomic. Raises on IO error — callers treat sidecar failure as
    non-fatal (the full-parse fallback still works)."""
    if result.query.endpoint not in RESUMABLE_ENDPOINTS:
        return None
    return _write_payload(_build_payload_from_result(result), main_path)


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


# ---------------------------------------------------------------------------
# Backfill: retrofit sidecars onto an existing corpus (the `fbscrape utils
# backfill-sidecars` subcommand). Derives the SAME payload as the in-memory
# writer, but via a single bounded streaming ijson pass — for files whose
# ScrapingResult is no longer in memory.
# ---------------------------------------------------------------------------

# ijson prefixes — accept both the modern `data` key and the legacy `posts`
# key (pre-rename files), mirroring scraper._stream_resume_state.
_TOP_PID = ("data.item.post_id", "posts.item.post_id")
_NODE_PID = ("data.item.node.post_id", "posts.item.node.post_id")
_REC_CURSOR = ("data.item.cursor", "posts.item.cursor")
_REC_END = ("data.item", "posts.item")


def _opener(path: str):
    """gzip vs plain, sniffed by magic bytes (matches cli._open_scrape_input)."""
    with open(path, "rb") as fh:
        is_gzip = fh.read(2) == b"\x1f\x8b"
    return gzip.open if is_gzip else open


def _build_payload_streaming(main_path: str) -> dict:
    """Single bounded-memory ijson pass over a saved scrape file. Sliding
    `deque` windows keep only the tails, so this never materializes the posts
    array (safe on multi-hundred-MB files). Parser-free — pulls endpoint /
    last_cursor / per-post cursor / post_id by prefix, same as
    `_stream_resume_state` pulls post_id."""
    opener = _opener(main_path)
    endpoint: str | None = None
    last_cursor = ""
    post_ids: deque = deque(maxlen=MAX_POST_IDS)
    rec_cursors: deque = deque(maxlen=MAX_CURSORS)
    post_count = 0
    node_pid = top_pid = rec_cursor = None

    with opener(main_path, "rb") as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix == "query.endpoint" and event == "string":
                endpoint = value
            elif prefix == "last_cursor" and event == "string":
                last_cursor = value or ""
            elif prefix in _TOP_PID and event == "string":
                top_pid = value
            elif prefix in _NODE_PID and event == "string":
                node_pid = value
            elif prefix in _REC_CURSOR and event == "string":
                rec_cursor = value or None
            elif prefix in _REC_END and event == "end_map":
                post_count += 1
                pid = node_pid or top_pid
                if pid:
                    post_ids.append(pid)
                if rec_cursor:
                    rec_cursors.append(rec_cursor)
                node_pid = top_pid = rec_cursor = None

    return {
        "schema": SCHEMA_VERSION,
        "endpoint": endpoint,
        "cursors": _assemble_cursors(last_cursor, list(rec_cursors)),
        "post_ids": list(post_ids),
        "post_count": post_count,
    }


def backfill_file(main_path: str, *, force: bool = False, dry_run: bool = False):
    """Write a sidecar for one saved scrape file by streaming it.

    Returns `(outcome, info)` where outcome is one of:
      - 'written'           sidecar written (or would be, under dry_run)
      - 'skipped-current'   a valid sidecar already exists (use force to rewrite)
      - 'skipped-endpoint'  non-resumable endpoint (no sidecar applicable)
      - 'error'             the streaming parse raised (info['error'] has detail)
    """
    if not force and read_sidecar(main_path) is not None:
        return "skipped-current", {}
    try:
        payload = _build_payload_streaming(main_path)
    except Exception as e:  # noqa: BLE001 - reported per-file, batch continues
        return "error", {"error": f"{type(e).__name__}: {e}"}

    if payload["endpoint"] not in RESUMABLE_ENDPOINTS:
        return "skipped-endpoint", {"endpoint": payload["endpoint"]}

    info = {
        "endpoint": payload["endpoint"],
        "post_count": payload["post_count"],
        "post_ids": len(payload["post_ids"]),
        "cursors": len(payload["cursors"]),
        "head_cursor": bool(payload["cursors"][0]),
        "sidecar": sidecar_path(main_path),
    }
    if not dry_run:
        _write_payload(payload, main_path)
    return "written", info


def collect_post_files(paths: list[str]) -> list[str]:
    """Expand files/dirs into a deduped list of post files. Skips sidecars and
    .tmp; when both `X.json` and `X.json.gz` exist, prefers the `.gz`."""
    by_stem: dict[str, str] = {}
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            candidates = sorted(p.glob("*.json")) + sorted(p.glob("*.json.gz"))
        elif p.exists():
            candidates = [p]
        else:
            continue
        for c in candidates:
            name = c.name
            if name.endswith(".resume.json") or name.endswith(".tmp"):
                continue
            stem = sidecar_path(str(c))  # same stem for X.json and X.json.gz
            if stem in by_stem and by_stem[stem].endswith(".json.gz"):
                continue
            by_stem[stem] = str(c)
    return list(by_stem.values())
