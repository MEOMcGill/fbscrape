"""CLI `flatten` command — output routing, format selection, --concat.

Uses Click's CliRunner against the real `flatten` command; we don't mock
the parser or polars. A captured UserTimeline fixture is the input, so the
flatten path itself must produce a non-empty parquet/csv/jsonl.
"""


import gzip
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fbscrape.cli import cli
from tests.conftest import load_fixture_or_skip


@pytest.fixture(scope="module")
def fixture_json_path(tmp_path_factory):
    """Materialize the captured UserTimeline fixture as a .json on disk that
    the CLI can read."""
    data = load_fixture_or_skip("user_timeline_hybrid")
    if not (data.get("data") or data.get("posts")):
        pytest.skip("fixture has no records")
    p = tmp_path_factory.mktemp("flatten_in") / "ut.json"
    p.write_text(json.dumps(data))
    return p


@pytest.fixture(scope="module")
def fixture_dir_with_two_files(tmp_path_factory, fixture_json_path):
    """Directory containing two copies of the fixture — one .json and one
    .json.gz — to exercise the dir-input path."""
    d = tmp_path_factory.mktemp("flatten_dir_in")
    plain = d / "a.json"
    plain.write_text(fixture_json_path.read_text())

    gz = d / "b.json.gz"
    with gzip.open(gz, "wt") as f:
        f.write(fixture_json_path.read_text())
    return d


def test_file_in_file_out_default_csv(fixture_json_path, tmp_path):
    runner = CliRunner()
    out = tmp_path / "out.csv"
    result = runner.invoke(cli, ["flatten", str(fixture_json_path), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists() and out.stat().st_size > 0


def test_file_in_file_out_parquet(fixture_json_path, tmp_path):
    runner = CliRunner()
    out = tmp_path / "out.parquet"
    result = runner.invoke(cli, [
        "flatten", str(fixture_json_path), "--output", str(out), "--format", "parquet",
    ])
    assert result.exit_code == 0, result.output
    assert out.exists() and out.stat().st_size > 0

    import polars as pl
    df = pl.read_parquet(out)
    assert df.height > 0


def test_file_in_no_output_writes_sibling(fixture_json_path):
    """When --output is omitted, the flattened file lands next to the input
    with a derived name (`<stem>_flat.<ext>`)."""
    runner = CliRunner()
    result = runner.invoke(cli, [
        "flatten", str(fixture_json_path), "--format", "parquet",
    ])
    assert result.exit_code == 0, result.output

    # Auto-derived parquet filenames carry the explicit .parquet.zstd suffix.
    sibling = fixture_json_path.with_name(
        fixture_json_path.stem + "_flat.parquet.zstd"
    )
    assert sibling.exists()


def test_dir_in_dir_out(fixture_dir_with_two_files, tmp_path):
    runner = CliRunner()
    out_dir = tmp_path / "flat_out"
    result = runner.invoke(cli, [
        "flatten", str(fixture_dir_with_two_files),
        "--output", str(out_dir), "--format", "parquet",
    ])
    assert result.exit_code == 0, result.output
    assert out_dir.is_dir()
    files = sorted(out_dir.iterdir())
    # Two inputs → two outputs.
    assert len(files) == 2
    assert all(f.name.endswith(".parquet.zstd") for f in files)


def test_dir_in_file_out_requires_concat(fixture_dir_with_two_files, tmp_path):
    """Without --concat, a directory input + file output is a user error."""
    runner = CliRunner()
    out = tmp_path / "merged.parquet"
    result = runner.invoke(cli, [
        "flatten", str(fixture_dir_with_two_files),
        "--output", str(out), "--format", "parquet",
    ])
    assert result.exit_code != 0
    assert "must be a folder unless --concat" in result.output


def test_dir_in_concat_merges_to_single_file(fixture_dir_with_two_files, tmp_path):
    runner = CliRunner()
    out = tmp_path / "merged.parquet"
    result = runner.invoke(cli, [
        "flatten", str(fixture_dir_with_two_files),
        "--output", str(out), "--format", "parquet", "--concat",
    ])
    assert result.exit_code == 0, result.output
    assert out.exists()

    import polars as pl
    df = pl.read_parquet(out)
    assert df.height > 0


def test_format_all_writes_three_files(fixture_json_path, tmp_path):
    """--format all with --output as a folder writes one of each format."""
    runner = CliRunner()
    out_dir = tmp_path / "all_out"
    result = runner.invoke(cli, [
        "flatten", str(fixture_json_path),
        "--output", str(out_dir), "--format", "all",
    ])
    assert result.exit_code == 0, result.output
    suffixes = {f.suffix for f in out_dir.iterdir() if f.is_file()}
    # parquet writes .zstd; csv and jsonl are direct.
    assert ".csv" in suffixes
    assert ".jsonl" in suffixes
    assert ".zstd" in suffixes  # from .parquet.zstd


def test_csv_output_has_expected_columns(fixture_json_path, tmp_path):
    """Spot-check that the flat CSV carries the load-bearing columns
    (post_id, created_at, author_name)."""
    runner = CliRunner()
    out = tmp_path / "out.csv"
    result = runner.invoke(cli, [
        "flatten", str(fixture_json_path), "--output", str(out), "--format", "csv",
    ])
    assert result.exit_code == 0, result.output

    import polars as pl
    df = pl.read_csv(out)
    for col in ("post_id", "created_at", "author_name", "url"):
        assert col in df.columns


def test_endpoint_override_honored(fixture_json_path, tmp_path):
    """--endpoint UserTimeline against the same fixture (which already
    declares UserTimeline) is a no-op pass — the override path must not
    crash even when redundant."""
    runner = CliRunner()
    out = tmp_path / "out.csv"
    result = runner.invoke(cli, [
        "flatten", str(fixture_json_path),
        "--output", str(out), "--endpoint", "UserTimeline",
    ])
    assert result.exit_code == 0, result.output
