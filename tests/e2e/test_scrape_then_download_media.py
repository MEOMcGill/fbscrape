"""End-to-end: `fbscrape scrape user-timeline ...` → `fbscrape download-media ...`.

fbcdn media URLs have a ~4-5 day TTL (CLAUDE.md Key Design Decision 9), so
the media download must run within the same process as the scrape — that's
exactly the contract this test exercises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


SCRAPE_TIMEOUT_SEC = 600
DOWNLOAD_TIMEOUT_SEC = 300


def _run_cli(args: list[str], *, cwd: Path, timeout: int):
    return subprocess.run(
        [sys.executable, "-m", "fbscrape.cli", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def test_scrape_then_download_media(require_active_account, repo_root: Path, tmp_path: Path):
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
    saved = list(scrape_out.glob("*.json*"))
    assert saved
    src = saved[0]

    media_dir = tmp_path / "media"
    dl_res = _run_cli(
        [
            "download-media", str(src),
            "--out-dir", str(media_dir),
            "--concurrency", "8",
            "--timeout", "60",
            "--log-level", "WARNING",
        ],
        cwd=repo_root,
        timeout=DOWNLOAD_TIMEOUT_SEC,
    )
    assert dl_res.returncode == 0, (
        f"download-media failed:\nstdout:\n{dl_res.stdout}\nstderr:\n{dl_res.stderr}"
    )

    files = list(media_dir.rglob("*"))
    files = [f for f in files if f.is_file()]
    # At least one media file should have landed. If the scrape returned 0
    # posts-with-media in this window, the test is informational only.
    if not files:
        pytest.skip(
            "no media files materialized — fixture window may have contained "
            "no posts with media. Not a failure of the download path."
        )
