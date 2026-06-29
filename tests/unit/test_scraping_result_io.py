"""ScrapingResult.save() — now one-post-per-line JSONL (format migration).

`save()` writes a self-contained envelope per line (`{query, result, …,
last_cursor, data: <single record>}`), gzip by default, atomic on a fresh
write, append for `--continue`. Loadable via `jsonl_store` (and the dual-format
loaders the CLI flatten/download use). Legacy whole-file envelopes still load.
"""


import gzip
import json
from datetime import datetime, timedelta

from fbscrape.models import Query, ScrapeOutcome, ScrapingResult
from fbscrape.jsonl_store import load_scrape_file, read_meta, iter_post_lines


def _make_result(data=None, last_cursor="cur") -> ScrapingResult:
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={},
    )
    return ScrapingResult(
        query=q,
        result="success",
        data=[{"node": {"post_id": "abc"}}, {"node": {"post_id": "def"}}]
        if data is None else data,
        time_started=datetime(2025, 1, 1, 12, 0, 0),
        time_taken=timedelta(seconds=42),
        last_cursor=last_cursor,
    )


def test_save_writes_jsonl_one_line_per_post(tmp_path):
    r = _make_result()
    path = r.save(str(tmp_path / "out.jsonl"))   # compress default True
    assert path == str(tmp_path / "out.jsonl.gz")

    lines = list(iter_post_lines(path))
    assert [ln["data"]["node"]["post_id"] for ln in lines] == ["abc", "def"]
    # every line carries the constant query metadata
    assert all(ln["query"]["query"]["handle"] == "zuck" for ln in lines)
    # final line is authoritative for leg status
    assert lines[-1]["result"] == "success"
    assert lines[-1]["last_cursor"] == "cur"
    assert lines[0]["result"] is None  # mid-leg


def test_save_compressed_appends_gz(tmp_path):
    r = _make_result()
    actual = r.save(str(tmp_path / "out.jsonl"), compress=True)
    assert actual.endswith(".jsonl.gz")
    with gzip.open(actual, "rt") as f:
        first = json.loads(f.readline())
    assert first["query"]["endpoint"] == "UserTimeline"


def test_save_uncompressed(tmp_path):
    r = _make_result()
    path = r.save(str(tmp_path / "out.jsonl"), compress=False)
    assert path == str(tmp_path / "out.jsonl")
    query_meta, records = load_scrape_file(path)
    assert [rec["node"]["post_id"] for rec in records] == ["abc", "def"]


def test_save_append_adds_lines(tmp_path):
    dest = str(tmp_path / "out.jsonl.gz")
    _make_result([{"node": {"post_id": "p1"}}], last_cursor="c1").save(dest)
    _make_result([{"node": {"post_id": "p2"}}], last_cursor="c2").save(dest, append=True)

    _, records = load_scrape_file(dest)
    assert [r["node"]["post_id"] for r in records] == ["p1", "p2"]
    # last line authoritative -> the appended leg's cursor
    assert read_meta(dest)["last_cursor"] == "c2"


def test_save_roundtrips_via_load_scrape_file(tmp_path):
    r = _make_result()
    path = r.save(str(tmp_path / "out.jsonl"))
    query_meta, records = load_scrape_file(path)
    assert query_meta["endpoint"] == "UserTimeline"
    assert len(records) == 2


def test_load_scrape_file_reads_legacy_envelope(tmp_path):
    """Dual-format: a pre-migration whole-file envelope (with `posts` key) still
    loads through the same loader the CLI uses."""
    legacy = {
        "query": {"endpoint": "UserTimeline", "mode": "hybrid",
                  "query": {"handle": "zuck"}, "params": {}},
        "result": "success",
        "posts": [{"node": {"post_id": "legacy1"}}],
        "time_started": "2024-01-15 00:00:00",
        "time_taken": "0:00:10",
        "last_cursor": "x",
    }
    p = tmp_path / "legacy.json.gz"
    with gzip.open(p, "wt") as f:
        json.dump(legacy, f)
    query_meta, records = load_scrape_file(str(p))
    assert query_meta["endpoint"] == "UserTimeline"
    assert [r["node"]["post_id"] for r in records] == ["legacy1"]


def test_from_outcome_attaches_query_and_spill_fields():
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={},
    )
    outcome = ScrapeOutcome(
        result="success", data=[{"x": 1}],
        time_started=datetime(2025, 1, 1), time_taken=timedelta(seconds=1),
        post_count=7, spill_path="/tmp/spill.jsonl.gz",
    )
    result = ScrapingResult.from_outcome(q, outcome)
    assert result.query is q
    assert result.post_count == 7
    assert result.spill_path == "/tmp/spill.jsonl.gz"
    assert result.num_records == 7


def test_num_records_falls_back_to_len_data():
    r = _make_result()
    assert r.spill_path is None
    assert r.num_records == 2


def test_add_record_appends():
    r = _make_result()
    initial = len(r.data)
    r.add_record({"node": {"post_id": "new"}})
    assert len(r.data) == initial + 1
