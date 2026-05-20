"""Integration-tier shared fixtures.

`require_active_account` from tests/conftest.py is reused — every test in
this directory should depend on it so missing-account environments skip
cleanly.

`fb_scraper` is a session-scoped FacebookScraper context manager so we
don't spin up a fresh accounts-pool per test (cheap, but the bigger win is
that all tests share one headless camoufox install and one pool init).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from fbscrape import FacebookScraper
from fbscrape.logger import set_log_level
set_log_level("DEBUG")

@pytest_asyncio.fixture(scope="function")
async def fb_scraper(require_active_account: Path):
    """Function-scoped so each test gets a clean session. Slower than
    session-scoped, but isolates account-rotation side effects."""
    async with FacebookScraper(
        db=str(require_active_account),
        max_browser_sessions=1,
        headless=True,
    ) as scraper:
        yield scraper
