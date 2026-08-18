"""Search hybrid — headless query against a stable, recently-active term.

Search results are noisier than timelines (FB filters less stable, rate
limits hit harder) — we assert the call returns *some* data and that what
comes back flattens through the Search flattener.
"""


import pytest
import fbscrape
from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_search_returns_results(fb_scraper):
    result = await fb_scraper.search(
        "carney",
        filters={
            "recent_posts": {},
            "creation_time": {"start": "2026-01-01", "end": "2026-12-31"},
        },
    )
    assert result.result in {
        "success",
        "scraped until user-specified starting date was reached",
    }
    assert result.num_records > 0

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "Search") for r in result.iter_posts()]
    flattened = [r for r in flattened if r is not None]
    assert len(flattened) > 0, "no Search records flattened — shape may have drifted"
