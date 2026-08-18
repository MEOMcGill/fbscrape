"""One-post-per-line JSONL store (`fbscrape/jsonl_store.py`).

Pins the format contract the migration depends on:
- writer emits one self-contained envelope line per post; the FINAL line carries
  the terminal result/time_taken, mid-leg lines carry null; zero-post legs write
  a status-only (`data: null`) line;
- numeric fields survive (Decimal trap that bit the streaming merge);
- append mode adds a gzip member; readers see all members;
- `read_resume_tail` recovers (last_cursor, recent post_ids) across appended legs;
- `iter_posts` / `iter_post_lines` skip status-only + malformed-trailing lines;
- `looks_like_jsonl` distinguishes JSONL from a legacy envelope;
- `convert_envelope_to_jsonl` round-trips a legacy file to JSONL.
"""

import gzip
import json
from datetime import datetime, timedelta, timezone

from fbscrape import jsonl_store as js
from fbscrape.jsonl_store import (
    JsonlPostWriter,
    convert_envelope_to_jsonl,
    iter_post_lines,
    iter_posts,
    looks_like_jsonl,
    read_meta,
    read_resume_tail,
)
from fbscrape.models import Query, ScrapingResult


def _lines(path):
    with gzip.open(path, "rt") if path.endswith(".gz") else open(path) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _query():
    return {"endpoint": "UserTimeline", "mode": "hybrid", "query": {"handle": "h"}, "params": {}}


# --- writer -----------------------------------------------------------------

def test_writer_one_line_per_post_final_line_stamped(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    w.write_post({"post_id": "a", "reactions": 3}, "CUR_A")
    w.write_post({"post_id": "b", "reactions": 4}, "CUR_A")
    w.write_post({"post_id": "c", "reactions": 5}, "CUR_B")
    w.finalize("success", timedelta(seconds=2), last_cursor="CUR_FINAL")

    lines = _lines(p)
    assert [ln["data"]["post_id"] for ln in lines] == ["a", "b", "c"]
    # mid-leg lines: result/time_taken null
    assert lines[0]["result"] is None and lines[0]["time_taken"] is None
    assert lines[1]["result"] is None
    # final line carries terminal status + the explicit final cursor
    assert lines[2]["result"] == "success"
    assert lines[2]["time_taken"] == str(timedelta(seconds=2))
    assert lines[2]["last_cursor"] == "CUR_FINAL"
    # numeric fields preserved as native types
    assert lines[0]["data"]["reactions"] == 3 and isinstance(lines[0]["data"]["reactions"], int)
    # constant metadata on every line
    assert all(ln["query"]["query"]["handle"] == "h" for ln in lines)


def test_writer_zero_posts_writes_status_line(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    w.finalize("no_new_posts_streak", timedelta(seconds=1), last_cursor="C")
    lines = _lines(p)
    assert len(lines) == 1
    assert lines[0]["data"] is None
    assert lines[0]["result"] == "no_new_posts_streak"
    assert lines[0]["last_cursor"] == "C"


def test_writer_decimal_safe(tmp_path):
    import decimal
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    w.write_post({"post_id": "a", "v": decimal.Decimal("12"), "f": decimal.Decimal("1.5")}, "C")
    w.finalize("success", timedelta(seconds=1))
    d = _lines(p)[0]["data"]
    assert d["v"] == 12 and d["f"] == 1.5


def test_append_adds_member_and_reads_back(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w1 = JsonlPostWriter(p, _query(), None)
    for i in range(3):
        w1.write_post({"post_id": f"p{i}"}, "C1")
    w1.finalize("success", timedelta(seconds=1), last_cursor="C1")

    w2 = JsonlPostWriter(p, _query(), None, append=True)
    for i in range(2):
        w2.write_post({"post_id": f"n{i}"}, "C2")
    w2.finalize("success", timedelta(seconds=1), last_cursor="C2")

    ids = [ln["data"]["post_id"] for ln in _lines(p)]
    assert ids == ["p0", "p1", "p2", "n0", "n1"]


# --- readers ----------------------------------------------------------------

def test_iter_posts_skips_status_only_lines(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    w.finalize("no_new_posts_streak", timedelta(seconds=1))  # status-only, data null
    assert list(iter_posts(p)) == []
    assert len(list(iter_post_lines(p))) == 1  # the status line is still a line


def test_iter_posts_tolerates_malformed_trailing_line(tmp_path):
    p = str(tmp_path / "o.jsonl")  # plain so we can append raw text
    w = JsonlPostWriter(p, _query(), None, compress=False)
    w.write_post({"post_id": "a"}, "C")
    w.finalize("success", timedelta(seconds=1))
    with open(p, "a") as f:
        f.write('{"data": {"post_id": "partial"  ')  # truncated, no newline-close
    assert [post["post_id"] for post in iter_posts(p)] == ["a"]  # bad line skipped


def test_read_meta_returns_last_line(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    w.write_post({"post_id": "a"}, "C1")
    w.finalize("cursor_reset", timedelta(seconds=1), last_cursor="CLAST")
    meta = read_meta(p)
    assert meta["result"] == "cursor_reset" and meta["last_cursor"] == "CLAST"


def test_read_resume_tail_across_legs(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w1 = JsonlPostWriter(p, _query(), None)
    for i in range(5):
        w1.write_post({"post_id": f"p{i}"}, "C1")
    w1.finalize("success", timedelta(seconds=1), last_cursor="C1")
    w2 = JsonlPostWriter(p, _query(), None, append=True)
    for i in range(3):
        w2.write_post({"post_id": f"n{i}"}, "C2")
    w2.finalize("success", timedelta(seconds=1), last_cursor="CRESUME")

    cursor, ids = read_resume_tail(p)
    assert cursor == "CRESUME"
    assert ids == ["p0", "p1", "p2", "p3", "p4", "n0", "n1", "n2"]


def test_read_resume_tail_caps_ids(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    for i in range(300):
        w.write_post({"post_id": f"p{i}"}, "C")
    w.finalize("success", timedelta(seconds=1), last_cursor="C")
    cursor, ids = read_resume_tail(p, n_ids=150)
    assert len(ids) == 150
    assert ids[0] == "p150" and ids[-1] == "p299"


def test_read_resume_tail_node_post_id_precedence(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    w.write_post({"node": {"post_id": "NESTED"}, "post_id": "TOP"}, "C")
    w.finalize("success", timedelta(seconds=1), last_cursor="C")
    _, ids = read_resume_tail(p)
    assert ids == ["NESTED"]


# --- format detection -------------------------------------------------------

def test_looks_like_jsonl_true(tmp_path):
    p = str(tmp_path / "o.jsonl.gz")
    w = JsonlPostWriter(p, _query(), None)
    w.write_post({"post_id": "a"}, "C")
    w.finalize("success", timedelta(seconds=1))
    assert looks_like_jsonl(p) is True


def test_looks_like_jsonl_false_on_legacy_envelope(tmp_path):
    p = _save_legacy(tmp_path, "legacy.json", [{"post_id": "a"}])
    assert looks_like_jsonl(p) is False


# --- converter --------------------------------------------------------------

def _save_legacy(tmp_path, name, data, last_cursor="LEGACYCUR", result="success"):
    """Write a genuine legacy whole-file envelope (NOT via save(), which now
    emits JSONL). Compact one-line gzip — `data` is a list, so looks_like_jsonl
    classifies it as legacy."""
    envelope = {
        "query": {"endpoint": "UserTimeline", "mode": "hybrid",
                  "query": {"handle": "h"}, "params": {}},
        "result": result,
        "data": data,
        "time_started": "2026-01-01 00:00:00+00:00",
        "time_taken": "0:00:03",
        "last_cursor": last_cursor,
    }
    p = str(tmp_path / (name + ".gz"))
    with gzip.open(p, "wt") as f:
        json.dump(envelope, f)
    return p


def test_convert_envelope_roundtrip(tmp_path):
    legacy = _save_legacy(
        tmp_path, "x.json",
        [{"post_id": "p0", "n": 1}, {"node": {"post_id": "p1"}}, {"post_id": "p2"}],
        last_cursor="LEGACYCUR",
    )
    dest = str(tmp_path / "x.jsonl.gz")
    n, endpoint = convert_envelope_to_jsonl(legacy, dest)

    assert n == 3 and endpoint == "UserTimeline"
    lines = _lines(dest)
    assert [js._post_id(ln["data"]) for ln in lines] == ["p0", "p1", "p2"]
    # leg metadata stamped on the final line; cursor preserved for resume
    assert lines[-1]["result"] == "success"
    assert lines[-1]["last_cursor"] == "LEGACYCUR"
    assert lines[0]["result"] is None  # mid-leg
    # resume-tail recovery works on the converted file
    cursor, ids = read_resume_tail(dest)
    assert cursor == "LEGACYCUR" and ids == ["p0", "p1", "p2"]
