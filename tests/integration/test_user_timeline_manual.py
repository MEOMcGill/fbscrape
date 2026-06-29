"""UserTimeline manual (scroll-driven) — headless scrape of zuck.

Narrower window than hybrid so the test doesn't take 10+ minutes — manual
scrolls one page-height at a time and only terminates on start_date reached
or stall watchdog.
"""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_zuck_manual_returns_posts(fb_scraper):
    result = await fb_scraper.user_timeline(
        "zuck", "2025-06-01", "2025-07-01", mode="manual",
    )
    assert result.result in {
        "success",
        "scraped until user-specified starting date was reached",
    }
    assert len(result.data) > 0

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "UserTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened
