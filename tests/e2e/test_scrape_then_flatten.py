"""End-to-end: `fbscrape scrape user-timeline ...` → `fbscrape flatten ...`.

Runs the installed CLI via subprocess (so we exercise the script entry
point, argument parsing, async loop, all of it) and asserts the output
parquet is readable + carries the expected columns.
"""


import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


SCRAPE_TIMEOUT_SEC = 600  # generous — manual + cold camoufox can be slow
FLATTEN_TIMEOUT_SEC = 60


def _run_cli(args: list[str], *, cwd: Path, timeout: int):
    """Run the fbscrape CLI as a subprocess via `python -m fbscrape.cli`
    so we don't depend on `pip install -e .` having registered the script."""
    return subprocess.run(
        [sys.executable, "-m", "fbscrape.cli", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def test_scrape_then_flatten(require_active_account, repo_root: Path, tmp_path: Path):
    scrape_out = tmp_path / "scrape"
    scrape_res = _run_cli(
        [
            "--db", str(require_active_account),
            "scrape", "user-timeline", "zuck",
            "--start-date", "2025-06-01",
            "--end-date", "2025-07-01",
            "--output-dir", str(scrape_out),
            "--max-sessions", "1",
            "--headless",
            "--log-level", "WARNING",
        ],
        cwd=repo_root,
        timeout=SCRAPE_TIMEOUT_SEC,
    )
    assert scrape_res.returncode == 0, (
        f"scrape failed:\nstdout:\n{scrape_res.stdout}\nstderr:\n{scrape_res.stderr}"
    )

    # CLI saves at least one JSON in scrape_out — find it.
    saved = list(scrape_out.glob("*.json*"))
    assert saved, f"no scrape output in {scrape_out}"
    # Sanity-check the saved file shape.
    src = saved[0]
    raw = src.read_text() if src.suffix == ".json" else None
    if raw:
        doc = json.loads(raw)
        assert (doc.get("data") or doc.get("posts")), "saved scrape has no records"

    # Now flatten into a parquet file.
    flat_out = tmp_path / "out.parquet"
    flat_res = _run_cli(
        ["flatten", str(src), "--output", str(flat_out), "--format", "parquet"],
        cwd=repo_root,
        timeout=FLATTEN_TIMEOUT_SEC,
    )
    assert flat_res.returncode == 0, (
        f"flatten failed:\nstdout:\n{flat_res.stdout}\nstderr:\n{flat_res.stderr}"
    )
    assert flat_out.exists() and flat_out.stat().st_size > 0

    import polars as pl
    df = pl.read_parquet(flat_out)
    assert df.height > 0
    for col in ("post_id", "created_at", "author_name", "url"):
        assert col in df.columns
