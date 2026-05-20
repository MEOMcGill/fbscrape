"""CLI --input-file parsing (csv / parquet / json / jsonl / yaml).

Tests `_load_scrape_targets` directly — it's the single dispatch point the
CLI uses across `scrape user-timeline`, `scrape search`,
`scrape page-transparency`, `scrape profile-authenticity`. All five formats
should produce identical normalized rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from fbscrape.cli import _load_scrape_targets


ROWS = [
    {"handle": "zuck",  "start_date": "2024-01-01", "end_date": "2024-02-01"},
    {"handle": "meta",  "start_date": "2024-01-15", "end_date": "2024-03-01"},
]


def _write_csv(path: Path, rows):
    import csv
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_json(path: Path, rows):
    path.write_text(json.dumps(rows))


def _write_jsonl(path: Path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_yaml(path: Path, rows):
    import yaml
    path.write_text(yaml.safe_dump(rows))


def _write_parquet(path: Path, rows):
    import pandas as pd
    pd.DataFrame(rows).to_parquet(path)


WRITERS = {
    ".csv":     _write_csv,
    ".json":    _write_json,
    ".jsonl":   _write_jsonl,
    ".yaml":    _write_yaml,
    ".parquet": _write_parquet,
}


@pytest.mark.parametrize("ext", list(WRITERS.keys()))
def test_all_formats_parse_to_same_rows(tmp_path, ext):
    path = tmp_path / f"targets{ext}"
    WRITERS[ext](path, ROWS)
    targets = _load_scrape_targets(str(path), key_field="handle")
    assert len(targets) == 2
    handles = [t["handle"] for t in targets]
    assert handles == ["zuck", "meta"]
    for t in targets:
        assert t["start_date"]
        assert t["end_date"]


def test_unrecognized_columns_dropped(tmp_path):
    """Extra columns beyond (key_field, *extra_keys) are silently dropped."""
    p = tmp_path / "x.csv"
    _write_csv(p, [
        {"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01",
         "notes": "ignore me", "priority": "high"},
    ])
    targets = _load_scrape_targets(str(p), key_field="handle")
    assert "notes" not in targets[0]
    assert "priority" not in targets[0]
    assert targets[0]["handle"] == "zuck"


def test_missing_required_key_raises(tmp_path):
    """A row missing the required identifier field must raise UsageError."""
    p = tmp_path / "bad.csv"
    _write_csv(p, [
        {"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        {"handle": "",     "start_date": "2024-01-01", "end_date": "2024-02-01"},
    ])
    with pytest.raises(click.UsageError, match="missing or empty `handle`"):
        _load_scrape_targets(str(p), key_field="handle")


def test_optional_fields_can_be_missing(tmp_path):
    """start_date / end_date are optional at the file level — they fall back
    to the CLI flag values in _resolve_targets."""
    p = tmp_path / "x.csv"
    _write_csv(p, [{"handle": "zuck"}])
    targets = _load_scrape_targets(str(p), key_field="handle")
    assert targets == [{"handle": "zuck"}]


def test_empty_file_raises(tmp_path):
    p = tmp_path / "empty.csv"
    _write_csv(p, [{"handle": ""}])
    # the empty handle triggers the missing-key error first; for a literally
    # zero-row file we'd need an empty CSV — write a header-only one.
    p.write_text("handle,start_date,end_date\n")
    with pytest.raises(click.UsageError, match="no rows found"):
        _load_scrape_targets(str(p), key_field="handle")


def test_unsupported_extension_raises(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("zuck")
    with pytest.raises(click.UsageError, match="Unsupported --input-file extension"):
        _load_scrape_targets(str(p), key_field="handle")


def test_page_transparency_key_field(tmp_path):
    """Single-shot endpoints override key_field. page-transparency uses
    `page_id`; date fields are not recognized for this endpoint."""
    p = tmp_path / "pages.csv"
    _write_csv(p, [
        {"page_id": "20531316728", "handle": "Meta"},
        {"page_id": "100044331674441", "handle": ""},
    ])
    targets = _load_scrape_targets(
        str(p), key_field="page_id", extra_keys=("handle",),
    )
    assert len(targets) == 2
    assert targets[0]["page_id"] == "20531316728"
    assert targets[0].get("handle") == "Meta"
    # Empty handle treated as not-supplied, doesn't crash since it's optional.
    assert "handle" not in targets[1]


def test_profile_authenticity_key_field(tmp_path):
    p = tmp_path / "profiles.csv"
    _write_csv(p, [{"user_id": "100044331674441"}])
    targets = _load_scrape_targets(
        str(p), key_field="user_id", extra_keys=(),
    )
    assert targets == [{"user_id": "100044331674441"}]


def test_yaml_single_object_is_wrapped(tmp_path):
    """A YAML file containing a single mapping (not a list) is treated as
    a one-row file."""
    import yaml
    p = tmp_path / "single.yaml"
    p.write_text(yaml.safe_dump({"handle": "zuck", "start_date": "2024-01-01"}))
    targets = _load_scrape_targets(str(p), key_field="handle")
    assert targets == [{"handle": "zuck", "start_date": "2024-01-01"}]
