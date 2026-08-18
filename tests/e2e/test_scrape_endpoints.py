"""End-to-end, one case per endpoint: `fbscrape scrape <endpoint> ...` -> `fbscrape flatten ...`.

Runs the installed CLI via subprocess (script entry point, argument parsing,
async loop, live scrape, on-disk save), then flattens the saved output to
parquet as a second black-box command and asserts the table is non-empty and
carries the endpoint's identifying column.

The per-endpoint wire-format contract: whatever `scrape` writes, `flatten` must
read. Opt-in via FBSCRAPE_RUN_E2E=1 (see conftest) and skips without an account.

Targets mirror tests/_capture_fixtures.py. NOTE: the CommentsList target is
still `MarkJCarney2025` pending a working CommentsList capture (its fixture is
not yet regenerated) — keep it in sync with _capture_fixtures.py.
"""


import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e

SCRAPE_TIMEOUT_SEC = 600
FLATTEN_TIMEOUT_SEC = 60

_COMMENTS_TARGET = (
    "MarkJCarney2025:"
    "pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl"
)


@dataclass(frozen=True)
class EndpointE2E:
    id: str
    scrape_args: tuple    # subcommand + endpoint-specific flags
    id_columns: tuple     # columns the flattened parquet must carry


ENDPOINTS = [
    EndpointE2E("UserTimeline",
                ("user-timeline", "zuck", "--start-date", "2025-01-01",
                 "--end-date", "2025-07-01", "--max-paginations", "3"),
                ("post_id",)),
    EndpointE2E("Search",
                ("search", "mark zuckerberg",
                 "--filter", "creation_time.start=2025-06-01",
                 "--filter", "creation_time.end=2025-07-01", "--max-paginations", "3"),
                ("post_id",)),
    EndpointE2E("GroupTimeline",
                ("group-timeline", "392585550772135", "--start-date", "2026-04-01",
                 "--end-date", "2026-05-14", "--max-paginations", "3"),
                ("post_id",)),
    EndpointE2E("CommentsList",
                ("comments-list", _COMMENTS_TARGET, "--max-results", "30"),
                ("comment_id",)),
    EndpointE2E("PageTransparency",
                ("page-transparency", "20531316728"),
                ("page_id",)),
    EndpointE2E("ProfileAuthenticity",
                ("profile-authenticity", "100044331674441"),
                ("user_id",)),
    EndpointE2E("PostDetail",
                ("post-detail", "albertansunitedtostoptheucp:27209929835285847", "--group"),
                ("post_id",)),
    EndpointE2E("ProfileInfo",
                ("profile-info", "zuck"),
                ("profile_id",)),
    EndpointE2E("ProfileAbout",
                ("profile-about", "61582991935083"),
                ("profile_id",)),
    EndpointE2E("GroupInfo",
                ("group-info", "392585550772135"),
                ("group_id",)),
    EndpointE2E("GroupAbout",
                ("group-about", "392585550772135"),
                ("group_id",)),
]


def _run_cli(args: list[str], *, cwd: Path, timeout: int):
    import os
    return subprocess.run(
        [sys.executable, "-m", "fbscrape.cli", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


@pytest.mark.parametrize("spec", ENDPOINTS, ids=lambda s: s.id)
def test_scrape_then_flatten_endpoint(
    spec: EndpointE2E, require_active_account, repo_root: Path, tmp_path: Path
):
    scrape_out = tmp_path / "scrape"
    scrape_res = _run_cli(
        ["--db", str(require_active_account), "scrape", *spec.scrape_args,
         "--output-dir", str(scrape_out), "--max-sessions", "1",
         "--headless", "--log-level", "WARNING"],
        cwd=repo_root, timeout=SCRAPE_TIMEOUT_SEC,
    )
    assert scrape_res.returncode == 0, (
        f"{spec.id} scrape failed:\nstdout:\n{scrape_res.stdout}\nstderr:\n{scrape_res.stderr}"
    )

    saved = list(scrape_out.glob("*.json*"))
    assert saved, f"{spec.id}: no scrape output written to {scrape_out}"

    flat_out = tmp_path / "out.parquet"
    flat_res = _run_cli(
        ["flatten", str(saved[0]), "--output", str(flat_out), "--format", "parquet"],
        cwd=repo_root, timeout=FLATTEN_TIMEOUT_SEC,
    )
    assert flat_res.returncode == 0, (
        f"{spec.id} flatten failed:\nstdout:\n{flat_res.stdout}\nstderr:\n{flat_res.stderr}"
    )
    assert flat_out.exists() and flat_out.stat().st_size > 0

    import polars as pl
    df = pl.read_parquet(flat_out)
    assert df.height > 0, f"{spec.id}: flatten produced 0 rows"
    for col in spec.id_columns:
        assert col in df.columns, f"{spec.id}: missing column {col!r} (have {df.columns})"
