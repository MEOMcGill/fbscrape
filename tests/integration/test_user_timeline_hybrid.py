"""UserTimeline hybrid (default) — headless scrape of zuck."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_zuck_hybrid_returns_posts(fb_scraper):
    result = await fb_scraper.user_timeline(
        "zuck", "2025-04-01", "2025-07-01", mode="hybrid",
    )
    assert result.result in {"success", "scraped until user-specified starting date was reached"}
    assert len(result.data) > 0

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "UserTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened from a real scrape — shape may have drifted"

    # Every flattened post must have a post_id and either url or permalink_url.
    for r in flattened:
        assert r["post_id"]
        assert r["url"] or r["permalink_url"]
