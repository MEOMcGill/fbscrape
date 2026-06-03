"""Streaming `--continue` merge (`fbscrape/merge.py`, KDD 25).

`stream_merge_and_save` rewrites a saved scrape file as `prior + new` by
streaming the prior disk→disk (bounded memory), and computes the resume sidecar
+ auto-unstick cursor in the same pass. These tests pin:
- equivalence: merged file == prior_data + new_data, envelope fields intact;
- sidecar reflects the *merged* tail (not new-only — the bug a naive streaming
  merge would introduce, since post-merge `result.data` holds only new records);
- auto-unstick picks the same cursor as the in-memory `_find_unstick_cursor`
  (shared `_unstick_select`);
- corrupt/truncated prior -> new leg saved anyway, dest never half-written;
- no prior -> new-only save + sidecar.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from fbscrape import merge
from fbscrape.merge import _unstick_select, stream_merge_and_save
from fbscrape.models import Query, ScrapingResult
from fbscrape.resume_sidecar import read_sidecar


def _res(data, last_cursor="HEAD", result="success",
         endpoint="UserTimeline", query=None):
    if query is None:
        query = {"handle": "h"}
    return ScrapingResult(
        query=Query(endpoint=endpoint, mode="hybrid", query=query, params={}),
        result=result,
        data=data,
        time_started=datetime.now(timezone.utc),
        time_taken=timedelta(seconds=1),
        last_cursor=last_cursor,
    )


def _read(dest):
    with gzip.open(dest, "rt") as f:
        return json.load(f)


# --- pure selection ---------------------------------------------------------

def test_unstick_select_skips_to_rank_then_first_cursored():
    # oldest-first after sort: ct 1,2,3,4,5; rank=3 starts at ct=3.
    items = [
        (5, 0, "c5"), (1, 1, None), (3, 2, None), (2, 3, "c2"), (4, 4, "c4"),
    ]
    # from rank-3 (ct=3, no cursor) walk forward -> ct=4 has cursor "c4".
    cursor, diag = _unstick_select(items, rank=3)
    assert cursor == "c4"
    assert diag["chosen_created_at"] == 4
    assert _unstick_select(items[:2], rank=3) is None  # too few


# --- streaming merge --------------------------------------------------------

def test_streaming_merge_equivalence(tmp_path):
    # Records carry NUMERIC fields (int + float) — ijson parses these as Decimal,
    # which stdlib json.dumps can't serialize. This guards the regression where
    # that TypeError silently dropped the whole prior to the new-only fallback.
    prior = _res(
        [{"post_id": f"p{i}", "cursor": f"pc{i}", "reactions": i, "ratio": 1.5}
         for i in range(5)],
        last_cursor="PRIORCUR",
    )
    prior_path = prior.save(str(tmp_path / "saved.json"), compress=True)  # .json.gz
    new = _res([{"post_id": f"n{i}", "cursor": f"nc{i}", "reactions": 100 + i}
                for i in range(3)],
               last_cursor="NEWCUR")
    dest = str(tmp_path / "saved.json.gz")  # == prior_path (aliasing case)

    out, n_prior, n_new, n_total = stream_merge_and_save(new, prior_path, dest, handle="h")
    assert (out, n_prior, n_new, n_total) == (dest, 5, 3, 8)

    doc = _read(dest)
    assert [r["post_id"] for r in doc["data"]] == \
        ["p0", "p1", "p2", "p3", "p4", "n0", "n1", "n2"]
    # Numeric fields survived as native int/float (not lost, not Decimal-stringified).
    assert doc["data"][0]["reactions"] == 0 and doc["data"][0]["ratio"] == 1.5
    assert doc["data"][5]["reactions"] == 100
    assert doc["result"] == "success"
    assert doc["last_cursor"] == "NEWCUR"       # no unstick on a success leg
    assert doc["query"]["query"]["handle"] == "h"


def test_merge_sidecar_reflects_merged_tail(tmp_path):
    # New leg adds < MAX_POST_IDS posts -> sidecar tail must include prior ids,
    # which new-only derivation would miss.
    prior = _res([{"post_id": f"p{i}", "cursor": f"pc{i}"} for i in range(5)])
    prior_path = prior.save(str(tmp_path / "s.json"), compress=True)
    new = _res([{"post_id": "n0", "cursor": "nc0"}], last_cursor="NEWCUR")
    dest = str(tmp_path / "s.json.gz")

    stream_merge_and_save(new, prior_path, dest, handle="h")
    head, post_ids = read_sidecar(dest)
    assert head == "NEWCUR"
    assert post_ids == ["p0", "p1", "p2", "p3", "p4", "n0"]


def test_merge_no_prior_saves_new_only(tmp_path):
    new = _res([{"post_id": "n0", "cursor": "nc0"}], last_cursor="C")
    dest = str(tmp_path / "s.json.gz")
    out, n_prior, n_new, n_total = stream_merge_and_save(new, None, dest, handle="h")
    assert (n_prior, n_new, n_total) == (0, 1, 1)
    assert [r["post_id"] for r in _read(dest)["data"]] == ["n0"]
    _, post_ids = read_sidecar(dest)
    assert post_ids == ["n0"]


def test_merge_corrupt_prior_falls_back_to_new_only(tmp_path):
    prior = _res([{"post_id": f"p{i}"} for i in range(5)])
    prior_path = prior.save(str(tmp_path / "s.json"), compress=True)
    # Truncate the gzip mid-stream -> EOFError when ijson reaches the cut.
    with open(prior_path, "r+b") as f:
        size = f.seek(0, 2)
        f.truncate(size // 2)

    new = _res([{"post_id": "n0", "cursor": "nc0"}], last_cursor="NEWCUR")
    dest = str(tmp_path / "s.json.gz")  # == prior_path
    out, n_prior, n_new, n_total = stream_merge_and_save(new, prior_path, dest, handle="h")

    assert (n_prior, n_total) == (0, 1)          # prior dropped, new preserved
    doc = _read(dest)                            # dest is a valid file, not half-merged
    assert [r["post_id"] for r in doc["data"]] == ["n0"]
    assert doc["last_cursor"] == "NEWCUR"


def test_streaming_unstick_matches_inmemory(tmp_path, monkeypatch):
    """no_new_posts_streak leg: streaming unstick must pick the same cursor as
    cli._find_unstick_cursor on the equivalent full list. Both use the same
    `_unstick_select`; a fake parser supplies `created_at` so we don't need a
    real Comet Story fixture."""
    class _FakeParser:
        def flatten(self, rec, endpoint):
            return {"created_at": rec.get("_ct")}
    monkeypatch.setattr("fbscrape.response.FacebookGraphQLParser", _FakeParser)

    # Mixed cursored/uncursored with non-sorted created_at.
    prior_recs = [
        {"post_id": "p0", "_ct": 50, "cursor": "cp0"},
        {"post_id": "p1", "_ct": 10, "cursor": None},
        {"post_id": "p2", "_ct": 30, "cursor": "cp2"},
    ]
    new_recs = [
        {"post_id": "n0", "_ct": 20, "cursor": "cn0"},
        {"post_id": "n1", "_ct": 40, "cursor": None},
    ]
    prior = _res(prior_recs)
    prior_path = prior.save(str(tmp_path / "s.json"), compress=True)
    new = _res(new_recs, last_cursor="ORIG", result="no_new_posts_streak")
    dest = str(tmp_path / "s.json.gz")

    stream_merge_and_save(new, prior_path, dest, handle="h")

    # Expected via the in-memory path on the full merged list.
    from fbscrape.cli import _find_unstick_cursor
    expected = _find_unstick_cursor(prior_recs + new_recs, endpoint="UserTimeline", rank=3)
    assert expected is not None
    assert _read(dest)["last_cursor"] == expected[0]
