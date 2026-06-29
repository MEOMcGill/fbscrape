"""Shared pytest fixtures and helpers.

- `scraping_results_dir` — path to captured fixture JSONs.
- `require_fixture(name)` — load a captured ScrapingResult JSON by stem
  (e.g. "user_timeline_hybrid"), skip the test if the capture script hasn't
  been run yet.
- `accounts_db_path` — path to db/accounts.db; skips integration/e2e tests
  when no active account exists.
"""


import json
import os
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SCRAPING_RESULTS_DIR = FIXTURES_DIR / "scraping_results"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def scraping_results_dir() -> Path:
    return SCRAPING_RESULTS_DIR


def load_fixture_or_skip(name: str) -> dict:
    """Load tests/fixtures/scraping_results/<name>.json or skip the test.

    Skips with a clear hint so a contributor without captured fixtures sees
    what to run rather than a cryptic FileNotFoundError.
    """
    path = SCRAPING_RESULTS_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(
            f"missing fixture {path.relative_to(REPO_ROOT)} — "
            f"run `python tests/_capture_fixtures.py --only {name}` first."
        )
    with open(path) as f:
        return json.load(f)


@pytest.fixture
def load_fixture():
    """Function fixture so tests can grab any capture by name."""
    return load_fixture_or_skip


@pytest.fixture(scope="session")
def accounts_db_path() -> Path:
    # Absolute path: fbscrape.db.DB prepends `<repo_root>/db/` to whatever
    # path it's given, so a relative one would be doubled. Returning the
    # absolute form here keeps downstream callers immune to that quirk.
    return (REPO_ROOT / "db" / "accounts.db").resolve()


@pytest.fixture
def require_active_account(accounts_db_path: Path):
    """Skip the test unless db/accounts.db has at least one active, not-in-use account."""
    if not accounts_db_path.exists():
        pytest.skip(f"no accounts DB at {accounts_db_path}")
    try:
        con = sqlite3.connect(str(accounts_db_path))
        cur = con.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE active=1 AND in_use=0"
        ).fetchone()[0]
        con.close()
    except sqlite3.Error as e:
        pytest.skip(f"accounts DB unreadable ({e})")
    if n < 1:
        pytest.skip("no active, available accounts in db/accounts.db")
    return accounts_db_path
