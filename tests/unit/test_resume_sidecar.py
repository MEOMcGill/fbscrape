"""Resume-state sidecar (`fbscrape/resume_sidecar.py`) + the sidecar-first
read path (`scraper._read_resume_state`).

The sidecar persists `(last_cursor, recent post_ids)` next to each saved scrape
so `--continue` recovers resume state without re-parsing the whole posts file
(Key Design Decision 24). These tests pin:
- write -> read round-trip, including `node.post_id` -> `post_id` precedence
  (must match `_stream_resume_state` so sidecar ids == full-parse ids);
- the caps (`MAX_POST_IDS` = 150 tail, `MAX_CURSORS` = 100 incl. head);
- the size+mtime validator: an out-of-band edit of the main file invalidates
  the sidecar (read returns None -> caller falls back to the full parse);
- non-resumable endpoints get no sidecar;
- `_read_resume_state` prefers a valid sidecar over `_stream_resume_state`,
  and falls back to it when the sidecar is absent.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fbscrape import resume_sidecar
from fbscrape.resume_sidecar import (
    MAX_CURSORS,
    MAX_POST_IDS,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)
from fbscrape import scraper as scraper_mod
from fbscrape.models import Query, ScrapingResult


def _result(data, last_cursor="HEAD", endpoint="UserTimeline", query=None):
    if query is None:
        query = {"handle": "someone"}
    return ScrapingResult(
        query=Query(endpoint=endpoint, mode="hybrid", query=query, params={}),
        result="success",
        data=data,
        time_started=datetime.now(timezone.utc),
        time_taken=timedelta(seconds=1),
        last_cursor=last_cursor,
    )


def _dummy_main_file(tmp_path):
    """write_sidecar/read_sidecar only os.stat the main file — content is
    irrelevant, just its size+mtime. A small placeholder suffices."""
    p = tmp_path / "x_UserTimeline_hybrid.json.gz"
    p.write_bytes(b"not-real-gzip-just-a-placeholder")
    return str(p)


def test_sidecar_path_strips_extensions():
    assert sidecar_path("/d/x.json.gz") == "/d/x.resume.json"
    assert sidecar_path("/d/x.json") == "/d/x.resume.json"


def test_write_then_read_roundtrip(tmp_path):
    main = _dummy_main_file(tmp_path)
    data = [
        {"node": {"post_id": "n1"}, "post_id": "ignored", "cursor": "c1"},
        {"post_id": "t2", "cursor": "c2"},
        {"node": {}, "post_id": "t3"},  # no cursor
    ]
    sc = write_sidecar(_result(data), main)
    assert sc == sidecar_path(main)

    head, post_ids = read_sidecar(main)
    assert head == "HEAD"
    # node.post_id wins over top-level post_id on the first record.
    assert post_ids == ["n1", "t2", "t3"]

    # cursors[0] is the head; per-post cursors follow in collection order.
    raw = json.loads(open(sc).read())
    assert raw["cursors"][0] == "HEAD"
    assert raw["cursors"][1:] == ["c1", "c2"]
    assert raw["endpoint"] == "UserTimeline"
    assert raw["post_count"] == 3


def test_caps_applied(tmp_path):
    main = _dummy_main_file(tmp_path)
    data = [{"post_id": f"p{i}", "cursor": f"c{i}"} for i in range(200)]
    write_sidecar(_result(data), main)
    raw = json.loads(open(sidecar_path(main)).read())

    assert len(raw["post_ids"]) == MAX_POST_IDS          # 150
    assert raw["post_ids"][0] == "p50"                   # last 150 -> p50..p199
    assert raw["post_ids"][-1] == "p199"
    assert len(raw["cursors"]) == MAX_CURSORS            # head + 99
    assert raw["cursors"][0] == "HEAD"
    assert raw["post_count"] == 200


def test_stale_main_file_invalidates_sidecar(tmp_path):
    main = _dummy_main_file(tmp_path)
    write_sidecar(_result([{"post_id": "p1"}]), main)
    assert read_sidecar(main) is not None

    # Mutate the main file -> size+mtime change -> sidecar no longer trusted.
    with open(main, "ab") as f:
        f.write(b"more-bytes")
    assert read_sidecar(main) is None


def test_missing_sidecar_returns_none(tmp_path):
    main = _dummy_main_file(tmp_path)
    assert read_sidecar(main) is None


def test_malformed_sidecar_returns_none(tmp_path):
    main = _dummy_main_file(tmp_path)
    with open(sidecar_path(main), "w") as f:
        f.write("{ not valid json")
    assert read_sidecar(main) is None


def test_non_resumable_endpoint_writes_nothing(tmp_path):
    main = _dummy_main_file(tmp_path)
    res = _result(
        [{"id": "whatever"}],
        endpoint="PageTransparency",
        query={"page_id": "123"},
    )
    assert write_sidecar(res, main) is None
    assert read_sidecar(main) is None  # no file created


def test_read_resume_state_prefers_sidecar(tmp_path, monkeypatch):
    main = _dummy_main_file(tmp_path)
    write_sidecar(_result([{"post_id": "a"}, {"post_id": "b"}], last_cursor="CUR"), main)

    # If the sidecar is honored, the heavy parse must never run.
    def _boom(_path):
        raise AssertionError("_stream_resume_state should not be called when a "
                             "valid sidecar exists")
    monkeypatch.setattr(scraper_mod, "_stream_resume_state", _boom)

    cursor, ids = scraper_mod._read_resume_state(main)
    assert cursor == "CUR"
    assert ids == ["a", "b"]


def test_read_resume_state_falls_back_when_no_sidecar(tmp_path, monkeypatch):
    main = _dummy_main_file(tmp_path)  # no sidecar written
    monkeypatch.setattr(
        scraper_mod, "_stream_resume_state",
        lambda _path: ("FALLBACK_CURSOR", ["x", "y"]),
    )
    cursor, ids = scraper_mod._read_resume_state(main)
    assert cursor == "FALLBACK_CURSOR"
    assert ids == ["x", "y"]


# --- backfill (streaming retrofit, `fbscrape utils backfill-sidecars`) -------

def _save_real_file(tmp_path, data, last_cursor="HEAD",
                    endpoint="UserTimeline", query=None, compress=True):
    """Save a genuine ScrapingResult to disk so the streaming backfill has a
    real (optionally gzipped) JSON file to parse."""
    res = _result(data, last_cursor=last_cursor, endpoint=endpoint, query=query)
    return res.save(str(tmp_path / "saved.json"), compress=compress)


def test_backfill_matches_inmemory_and_full_parse(tmp_path):
    """The streaming backfill must produce the SAME (head, post_ids) as both the
    in-memory writer and scraper._stream_resume_state on the same file."""
    data = [
        {"node": {"post_id": "n1"}, "post_id": "ignored", "cursor": "c1"},
        {"post_id": "t2", "cursor": "c2"},
        {"node": {}, "post_id": "t3"},
    ]
    main = _save_real_file(tmp_path, data, last_cursor="HEAD")

    outcome, info = resume_sidecar.backfill_file(main)
    assert outcome == "written"
    assert info["endpoint"] == "UserTimeline"
    assert info["post_count"] == 3

    head, post_ids = read_sidecar(main)
    assert head == "HEAD"
    assert post_ids == ["n1", "t2", "t3"]

    # Identical to the full ijson parse the sidecar replaces.
    full_cursor, full_ids = scraper_mod._stream_resume_state(main)
    assert full_cursor == "HEAD"
    assert full_ids == ["n1", "t2", "t3"]


def test_backfill_idempotent_and_force(tmp_path):
    main = _save_real_file(tmp_path, [{"post_id": "p1", "cursor": "c1"}])
    assert resume_sidecar.backfill_file(main)[0] == "written"
    # Second pass: a current sidecar exists -> skipped.
    assert resume_sidecar.backfill_file(main)[0] == "skipped-current"
    # --force rewrites regardless.
    assert resume_sidecar.backfill_file(main, force=True)[0] == "written"


def test_backfill_dry_run_writes_nothing(tmp_path):
    main = _save_real_file(tmp_path, [{"post_id": "p1"}])
    outcome, _ = resume_sidecar.backfill_file(main, dry_run=True)
    assert outcome == "written"
    assert read_sidecar(main) is None  # nothing actually written


def test_backfill_skips_non_resumable_endpoint(tmp_path):
    main = _save_real_file(
        tmp_path, [{"id": "x"}], endpoint="PageTransparency",
        query={"page_id": "123"},
    )
    outcome, info = resume_sidecar.backfill_file(main)
    assert outcome == "skipped-endpoint"
    assert info["endpoint"] == "PageTransparency"
    assert read_sidecar(main) is None
