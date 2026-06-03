"""Streaming `--continue` merge.

`cli._finalize_continue_result` used to merge a finished leg into its rolling
archive by `json.load`-ing the *entire* prior file (a 300 MB+ gzip → 8-15 GB of
Python objects), concatenating `prior + new`, and rewriting. Several such merges
overlapping blew past the container memory limit and OOM-killed the run.

`stream_merge_and_save` rewrites the file as a disk→disk stream instead: prior
records flow one at a time from the old file to a temp via `ijson.items`, the
(bounded) new records are appended, and the file is atomically `os.replace`d
into place. Peak memory is `O(one prior record + the new scraped data)` —
independent of prior-file size. This is the "stream prior records via ijson"
optimization KDD 22 anticipated; the OOM made it load-bearing. See KDD 25.

Byproducts computed in the single pass (no extra reads):
  - the resume-state sidecar (`<stem>.resume.json`) from the *merged* tail —
    computing it here (not via `write_sidecar` on the post-merge result) is what
    keeps `post_ids` correct, since after a streaming merge `result.data` holds
    only the new records;
  - the auto-unstick cursor (only when the leg ended on `no_new_posts_streak`),
    via a streaming flatten + the shared `_unstick_select` selection.

Output format: valid JSON, one record per line in the `data` array (compact,
not `indent=2`). Far smaller than the old indented files on these hundreds-of-MB
outputs, and every reader (`json.load`, `ijson`) is whitespace-agnostic.
"""
from __future__ import annotations

import decimal
import gzip
import json
import os
import tempfile

import ijson

from .logger import logger
from .resume_sidecar import (
    MAX_CURSORS,
    MAX_POST_IDS,
    RESUMABLE_ENDPOINTS,
    SCHEMA_VERSION,
    _assemble_cursors,
    _post_id,
    _write_payload,
)
from collections import deque


def _json_default(o):
    """json.dumps fallback for `Decimal` — ijson parses JSON numbers as Decimal,
    which stdlib json can't serialize. Convert back to int/float (matching the
    types the old `json.load`-based merge produced, so output is unchanged).
    Without this, every real record (all carry numeric fields) raises TypeError
    and the prior gets silently dropped to the new-only fallback."""
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _unstick_select(items, rank: int = 3):
    """Pure selection shared by the in-memory and streaming unstick paths.

    `items` is `[(created_at:int, idx:int, cursor:str|None), ...]`. Sort oldest-
    first, then from the rank-th oldest position walk forward to the first
    cursored post. Returns `(cursor, diagnostic)` or None. (rank-1 skip dodges
    the bootstrap-edge highlight outlier — see `cli._find_unstick_cursor`.)
    """
    items = sorted(items, key=lambda x: x[0])  # oldest first
    if len(items) < rank:
        return None
    for cur_rank, (ct, idx, cursor) in enumerate(items[rank - 1:], start=rank):
        if cursor:
            return (cursor, {
                "chosen_rank": cur_rank,
                "chosen_idx": idx,
                "chosen_created_at": ct,
            })
    return None


def _opener(path: str):
    """gzip vs plain, sniffed by magic bytes (matches cli._open_scrape_input)."""
    with open(path, "rb") as fh:
        is_gzip = fh.read(2) == b"\x1f\x8b"
    return gzip.open if is_gzip else open


def _detect_data_key(path: str) -> str:
    """Return 'data' or 'posts' — whichever array the prior file carries (the
    `data` rename left legacy `posts` files around). Cheap: stops at the array
    start, which sits right after the small `query`/`result` head."""
    opener = _opener(path)
    with opener(path, "rb") as fh:
        for prefix, event, _value in ijson.parse(fh):
            if event == "start_array" and prefix in ("data", "posts"):
                return prefix
    return "data"


def _write_merge_sidecar(result, dest_path, head_cursor, post_ids, rec_cursors,
                         post_count, handle=None) -> None:
    """Emit the resume sidecar from the merged-tail windows. Best-effort — a
    failure here must never fail the merge (the reader falls back to the full
    parse). Mirrors `cli._finalize_continue_result`'s best-effort contract."""
    if result.query.endpoint not in RESUMABLE_ENDPOINTS:
        return
    payload = {
        "schema": SCHEMA_VERSION,
        "endpoint": result.query.endpoint,
        "cursors": _assemble_cursors(head_cursor or "", list(rec_cursors)),
        "post_ids": list(post_ids),
        "post_count": post_count,
    }
    try:
        _write_payload(payload, dest_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"@{handle}: resume sidecar write failed ({type(e).__name__}: {e})"
        )


def _save_new_only(result, dest_path, handle=None):
    """Fallback: save just the new leg (no merge) and its sidecar. Used when
    there's no prior, or when the prior is unreadable. Reuses the atomic
    `ScrapingResult.save` + `write_sidecar` path (here `result.data` IS the full
    content, so the sidecar is correct)."""
    from .resume_sidecar import write_sidecar
    saved = result.save(dest_path, compress=True)
    try:
        write_sidecar(result, saved)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"@{handle}: resume sidecar write failed ({type(e).__name__}: {e})"
        )
    return saved


def stream_merge_and_save(result, prior_path, dest_path, *, handle=None, rank=3):
    """Merge `prior_path`'s records + `result.data` into `dest_path` by
    streaming, write the resume sidecar from the merged tail, and (on a
    `no_new_posts_streak` leg) auto-unstick the cursor.

    Returns `(dest_path, n_prior, n_new, n_total)`.

    Atomic: writes to a temp file and `os.replace`s it over `dest_path`, so a
    crash leaves the prior file intact. If the prior is unreadable/corrupt the
    new data is still saved (new-only) — never lost, never a half-merged file.
    """
    n_new = len(result.data or [])

    # No prior to merge against -> just save the new leg.
    if not prior_path or not os.path.exists(prior_path):
        _save_new_only(result, dest_path, handle=handle)
        return dest_path, 0, n_new, n_new

    needs_unstick = result.result == "no_new_posts_streak"
    parser = None
    if needs_unstick:
        from .response import FacebookGraphQLParser
        parser = FacebookGraphQLParser()

    post_ids: deque = deque(maxlen=MAX_POST_IDS)
    rec_cursors: deque = deque(maxlen=MAX_CURSORS)
    unstick_items: list = []
    counter = {"n": 0}

    directory = os.path.dirname(dest_path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(fd)
    gz = None

    def emit(rec):
        idx = counter["n"]
        counter["n"] += 1
        gz.write("," if idx else "")
        gz.write("\n")
        gz.write(json.dumps(rec, default=_json_default))
        pid = _post_id(rec)
        if pid:
            post_ids.append(pid)
        c = rec.get("cursor")
        if c:
            rec_cursors.append(c)
        if needs_unstick:
            try:
                flat = parser.flatten(rec, endpoint=result.query.endpoint)
            except Exception:  # noqa: BLE001
                flat = None
            ct = flat.get("created_at") if flat else None
            if isinstance(ct, (int, float)):
                unstick_items.append((int(ct), idx, rec.get("cursor")))

    try:
        gz = gzip.open(tmp, "wt")
        # Envelope head (same key order as ScrapingResult.to_dict).
        gz.write('{"query": ')
        gz.write(json.dumps(result.query.to_dict()))
        gz.write(', "result": ')
        gz.write(json.dumps(result.result))
        gz.write(', "data": [')

        # --- prior records (streamed). A parse failure here must not clobber
        #     the destination: discard the temp and fall back to new-only. ---
        n_prior = 0
        try:
            data_key = _detect_data_key(prior_path)
            opener = _opener(prior_path)
            with opener(prior_path, "rb") as pf:
                for rec in ijson.items(pf, f"{data_key}.item", use_float=True):
                    emit(rec)
                    n_prior += 1
        except Exception as e:  # noqa: BLE001 - any prior-read failure (corrupt
            # JSON, truncated gzip -> EOFError, IO error) must not lose the new
            # leg or clobber dest; discard the partial temp and save new-only.
            gz.close()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            logger.warning(
                f"@{handle}: prior file {prior_path} unreadable mid-merge "
                f"({type(e).__name__}: {e}); saving new leg only (no merge)"
            )
            _save_new_only(result, dest_path, handle=handle)
            return dest_path, 0, n_new, n_new

        # --- new records (already in memory, bounded by --max-posts) ---
        for rec in (result.data or []):
            emit(rec)

        # --- auto-unstick (no_new_posts_streak only) ---
        last_cursor = result.last_cursor or ""
        if needs_unstick:
            chosen = _unstick_select(unstick_items, rank=rank)
            if chosen:
                new_cursor, diag = chosen
                last_cursor = new_cursor
                logger.info(
                    f"@{handle}: no_new_posts_streak on --continue resume — "
                    f"auto-unsticking cursor to rank #{diag['chosen_rank']}, "
                    f"data[{diag['chosen_idx']}]"
                )

        # Envelope tail.
        gz.write("\n], ")
        gz.write('"time_started": ')
        gz.write(json.dumps(str(result.time_started)))
        gz.write(', "time_taken": ')
        gz.write(json.dumps(str(result.time_taken)))
        gz.write(', "last_cursor": ')
        gz.write(json.dumps(last_cursor or None))
        gz.write("}")
        gz.close()
        gz = None

        os.chmod(tmp, 0o644)
        os.replace(tmp, dest_path)
    except BaseException:
        if gz is not None:
            try:
                gz.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    n_total = counter["n"]
    logger.info(
        f"@{handle}: merged {n_prior} prior + {n_new} new posts (total {n_total})"
    )

    # Sidecar from the merged tail (post_ids/cursors reflect prior+new).
    _write_merge_sidecar(
        result, dest_path, last_cursor, post_ids, rec_cursors, n_total, handle=handle,
    )

    # Drop a superseded legacy uncompressed prior so one stem doesn't keep two
    # on-disk files.
    if prior_path != dest_path and prior_path.endswith(".json"):
        try:
            os.remove(prior_path)
        except OSError:
            pass

    return dest_path, n_prior, n_new, n_total
