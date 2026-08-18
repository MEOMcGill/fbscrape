"""Unit tests for CLI helpers around optional-date scraping.

Covers `_resolve_targets` per-endpoint policy flags (`require_start`,
`require_end`, `default_end_to_today`) and the new `_build_stem` /
`_existing_output_for_stem` helpers that replaced ad-hoc date-suffixed
file-name construction.
"""


import os

import click
import pytest

from fbscrape.cli import (
    _build_stem,
    _existing_output_for_stem,
    _resolve_targets,
)
from fbscrape.utils import utc


def test_build_stem_no_dates():
    assert _build_stem("zuck", "UserTimeline", "hybrid") == "zuck_UserTimeline_hybrid"
    assert _build_stem("392585550772135", "GroupTimeline", "hybrid") == \
        "392585550772135_GroupTimeline_hybrid"


def test_build_stem_sanitizes_dots():
    """Dots in handles are flattened to underscores so filenames don't get
    parsed as multi-extension paths by downstream tooling."""
    assert _build_stem("foo.bar", "UserTimeline", "hybrid") == "foo_bar_UserTimeline_hybrid"


def test_existing_output_prefers_gz(tmp_path):
    """When both `.json` and `.json.gz` exist, the helper prefers `.json.gz`."""
    stem = "zuck_UserTimeline_hybrid"
    (tmp_path / f"{stem}.json").write_text("{}")
    (tmp_path / f"{stem}.json.gz").write_bytes(b"\x1f\x8b\x08")  # gzip magic
    found = _existing_output_for_stem(str(tmp_path), stem)
    assert found and found.endswith(".json.gz")


def test_existing_output_returns_none_when_missing(tmp_path):
    assert _existing_output_for_stem(str(tmp_path), "no_such_stem") is None


# ---------------------------------------------------------------------------
# _resolve_targets: per-endpoint policy matrix
# ---------------------------------------------------------------------------

def test_resolve_targets_grouptimeline_no_dates_allowed():
    """GroupTimeline: both dates optional, no auto-today."""
    out = _resolve_targets(
        keys=["392585550772135"], input_file=None,
        start_date=None, end_date=None,
        require_start=False, require_end=False, default_end_to_today=False,
    )
    assert out == [{
        "handle": "392585550772135",
        "start_date": None,
        "end_date": None,
    }]


def test_resolve_targets_usertimeline_no_dates_auto_today():
    """UserTimeline: no start, end auto-fills to today (UTC) — mirrors FB UI."""
    out = _resolve_targets(
        keys=["zuck"], input_file=None,
        start_date=None, end_date=None,
        require_start=False, require_end=False, default_end_to_today=True,
    )
    today = utc.now().strftime("%Y-%m-%d")
    assert out == [{
        "handle": "zuck",
        "start_date": None,
        "end_date": today,
    }]


def test_resolve_targets_usertimeline_only_start_flag():
    """UserTimeline with only --start-date: end still auto-fills to today."""
    out = _resolve_targets(
        keys=["zuck"], input_file=None,
        start_date="2024-01-01", end_date=None,
        require_start=False, require_end=False, default_end_to_today=True,
    )
    today = utc.now().strftime("%Y-%m-%d")
    assert out[0]["start_date"] == "2024-01-01"
    assert out[0]["end_date"] == today


def test_resolve_targets_usertimeline_only_end_flag():
    """UserTimeline with only --end-date: start stays None, end honored."""
    out = _resolve_targets(
        keys=["zuck"], input_file=None,
        start_date=None, end_date="2025-01-01",
        require_start=False, require_end=False, default_end_to_today=True,
    )
    assert out[0] == {"handle": "zuck", "start_date": None, "end_date": "2025-01-01"}



def test_resolve_targets_both_dates_passed_through():
    out = _resolve_targets(
        keys=["zuck"], input_file=None,
        start_date="2024-01-01", end_date="2024-12-31",
        require_start=False, require_end=False, default_end_to_today=True,
    )
    assert out[0] == {
        "handle": "zuck",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    }


def test_resolve_targets_input_file_and_flag_conflict(tmp_path):
    """If file supplies start_date AND --start-date is also set → reject."""
    import csv
    p = tmp_path / "targets.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["handle", "start_date"])
        w.writeheader()
        w.writerow({"handle": "zuck", "start_date": "2024-01-01"})
    with pytest.raises(click.UsageError, match="Input file supplies start_date"):
        _resolve_targets(
            keys=[], input_file=str(p),
            start_date="2024-06-01", end_date=None,
            require_start=False, require_end=False, default_end_to_today=False,
        )


def test_resolve_targets_neither_positional_nor_file():
    with pytest.raises(click.UsageError, match="Must provide either"):
        _resolve_targets(
            keys=[], input_file=None,
            start_date=None, end_date=None,
            require_start=False, require_end=False, default_end_to_today=False,
        )


def test_resolve_targets_positional_and_file_conflict(tmp_path):
    p = tmp_path / "targets.csv"
    p.write_text("handle\nzuck\n")
    with pytest.raises(click.UsageError, match="Cannot use both"):
        _resolve_targets(
            keys=["meta"], input_file=str(p),
            start_date=None, end_date=None,
            require_start=False, require_end=False, default_end_to_today=False,
        )
