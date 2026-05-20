"""ScrapingResult save/load round-trip + legacy compatibility.

Covers the on-disk contract: callers depend on `result.save(path)` producing
a JSON file that's later loadable by the CLI's `flatten` / `download-media`
commands (which still accept the pre-rename `"posts": [...]` key).
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta

import pytest

from fbscrape.models import Query, ScrapeOutcome, ScrapingResult
from fbscrape.cli import _open_scrape_input


def _make_result() -> ScrapingResult:
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={},
    )
    return ScrapingResult(
        query=q,
        result="success",
        data=[{"node": {"post_id": "abc"}}, {"node": {"post_id": "def"}}],
        time_started=datetime(2025, 1, 1, 12, 0, 0),
        time_taken=timedelta(seconds=42),
    )


def test_save_plain_json_roundtrips(tmp_path):
    r = _make_result()
    path = r.save(str(tmp_path / "out.json"))
    assert path == str(tmp_path / "out.json")

    with open(path) as f:
        loaded = json.load(f)

    assert loaded["result"] == "success"
    assert loaded["query"]["endpoint"] == "UserTimeline"
    assert loaded["query"]["query"]["handle"] == "zuck"
    assert len(loaded["data"]) == 2


def test_save_compressed_appends_gz(tmp_path):
    """Saving with compress=True must auto-append .gz when missing
    (otherwise gzip bytes would land in a .json-named file)."""
    r = _make_result()
    requested = str(tmp_path / "out.json")
    actual = r.save(requested, compress=True)
    assert actual == requested + ".gz"

    with gzip.open(actual, "rt") as f:
        loaded = json.load(f)
    assert loaded["result"] == "success"


def test_save_already_gz_path_does_not_double_extend(tmp_path):
    r = _make_result()
    path = r.save(str(tmp_path / "out.json.gz"), compress=True)
    assert path.endswith(".json.gz")
    assert not path.endswith(".gz.gz")


def test_open_scrape_input_sniffs_gzip_regardless_of_extension(tmp_path):
    """A gzip-bytes file with a `.json` extension (pre-auto-extension saves)
    should still decode via the CLI loader."""
    r = _make_result()
    p = tmp_path / "out.json"
    # Manually gzip-write to a .json-named path.
    with gzip.open(p, "wt") as f:
        json.dump(r.to_dict(), f)

    with _open_scrape_input(str(p)) as f:
        loaded = json.load(f)
    assert loaded["result"] == "success"


def test_open_scrape_input_handles_plain_json(tmp_path):
    r = _make_result()
    p = str(tmp_path / "out.json")
    r.save(p)
    with _open_scrape_input(p) as f:
        loaded = json.load(f)
    assert loaded["result"] == "success"


def test_legacy_posts_key_still_loadable_through_cli_loader(tmp_path):
    """Pre-rename saves used `"posts": [...]` instead of `"data": [...]`.
    The CLI flatten/download paths both check `data.get('data') or
    data.get('posts')`, so the fixture below must round-trip cleanly through
    that fallback."""
    legacy = {
        "query": {
            "endpoint": "UserTimeline",
            "mode": "hybrid",
            "query": {"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
            "params": {},
        },
        "result": "success",
        "posts": [{"node": {"post_id": "legacy1"}}],
        "time_started": "2024-01-15 00:00:00",
        "time_taken": "0:00:10",
    }
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(legacy))

    with _open_scrape_input(str(p)) as f:
        loaded = json.load(f)
    records = loaded.get("data") or loaded.get("posts") or []
    assert len(records) == 1
    assert records[0]["node"]["post_id"] == "legacy1"


def test_from_outcome_attaches_query():
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={},
    )
    outcome = ScrapeOutcome(
        result="success",
        data=[{"x": 1}],
        time_started=datetime(2025, 1, 1),
        time_taken=timedelta(seconds=1),
    )
    result = ScrapingResult.from_outcome(q, outcome)
    assert result.query is q
    assert result.result == "success"
    assert result.data == [{"x": 1}]


def test_add_record_appends():
    r = _make_result()
    initial = len(r.data)
    r.add_record({"node": {"post_id": "new"}})
    assert len(r.data) == initial + 1
    assert r.data[-1]["node"]["post_id"] == "new"
