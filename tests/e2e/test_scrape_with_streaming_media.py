"""End-to-end: media collected *during* the scrape, both ways.

Companion to `test_scrape_then_download_media.py` (the post-hoc path). Here the
scrape itself carries the media sinks:

  fbscrape scrape user-timeline zuck --download-media --media-manifest q.jsonl
  fbscrape download-media q.jsonl --from-manifest --out-dir ...

fbcdn URLs are signed with a ~4-5 day TTL (CLAUDE.md Key Design Decision 9), so
the immediate path guarantees a fresh signature, and the manifest path lets a
separate process fetch without slowing the scrape. This test runs both and
confirms the two agree on filenames (the manifest drain reports its items as
`skipped` because the in-scrape download already wrote them).
"""


import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


SCRAPE_TIMEOUT_SEC = 900
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


def test_scrape_streams_media_and_manifest(require_active_account, repo_root: Path,
                                           tmp_path: Path):
    scrape_out = tmp_path / "scrape"
    media_dir = tmp_path / "media"
    manifest = tmp_path / "media_queue.jsonl"

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
            # Both sinks at once: download now, and record what was queued.
            "--download-media",
            "--media-dir", str(media_dir),
            "--media-manifest", str(manifest),
            "--media-concurrency", "4",
        ],
        cwd=repo_root,
        timeout=SCRAPE_TIMEOUT_SEC,
    )
    assert scrape_res.returncode == 0, (
        f"scrape failed:\nstdout:\n{scrape_res.stdout}\nstderr:\n{scrape_res.stderr}"
    )
    assert list(scrape_out.glob("*.json*")), "scrape wrote no output file"

    if not manifest.exists():
        pytest.skip(
            "no media queued — the scrape window may have held no posts with "
            "media. Not a failure of the streaming path."
        )

    records = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    assert records
    for record in records:
        assert record["url"].startswith("http")
        assert record["filename"]
        assert record["queued_at"]
        assert record["endpoint"] == "UserTimeline"
        assert record["label"] == "zuck"

    # Immediate path: files landed under <media-dir>/<handle>/ during the scrape.
    downloaded = {f.name for f in (media_dir / "zuck").glob("*") if f.is_file()}
    assert downloaded, f"--download-media wrote nothing to {media_dir / 'zuck'}"

    # Handoff path: draining the manifest into the same directory is a no-op,
    # which proves both paths compute identical target filenames.
    drain_res = _run_cli(
        [
            "download-media", str(manifest),
            "--from-manifest",
            "--out-dir", str(media_dir / "zuck"),
            "--log-level", "WARNING",
        ],
        cwd=repo_root,
        timeout=DOWNLOAD_TIMEOUT_SEC,
    )
    assert drain_res.returncode == 0, (
        f"manifest drain failed:\nstdout:\n{drain_res.stdout}\nstderr:\n{drain_res.stderr}"
    )
    summary = dict(
        (k, int(v)) for k, v in (
            part.split("=") for part in drain_res.stdout.split()
            if part.split("=")[0] in ("total", "saved", "skipped", "failed")
        )
    )
    # Anything the in-scrape download already wrote must be recognized as
    # present rather than re-fetched — same URL set, same target filenames.
    # (A stale-signature item the scrape failed on may still be retried here,
    # so `saved` is not asserted to be zero.)
    assert summary["skipped"] >= len(downloaded), summary
