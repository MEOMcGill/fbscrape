"""Search hybrid — headless query against a stable, recently-active term.

Search results are noisier than timelines (FB filters less stable, rate
limits hit harder) — we assert the call returns *some* data and that what
comes back flattens through the UserTimeline flattener (Search records share
the Story shape).
"""

from __future__ import annotations

import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_search_returns_results(fb_scraper):
    result = await fb_scraper.search(
        "mark zuckerberg", "2025-06-01", "2025-07-01",
    )
    assert result.result in {
        "success",
        "scraped until user-specified starting date was reached",
    }
    assert len(result.data) > 0

    # Search posts share the PCTFRQ Story shape — flatten with UserTimeline.
    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "UserTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    # Don't pin a precise % — search results can be heterogeneous (some shapes
    # the UserTimeline flattener won't recognize). Just require at least one.
    assert len(flattened) > 0, "no search records flattened — shape may have drifted"
