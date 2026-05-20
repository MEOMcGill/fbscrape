"""PageTransparency single-shot — headless against the Meta page."""

from __future__ import annotations

import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_page_transparency_meta(fb_scraper):
    result = await fb_scraper.page_transparency("20531316728")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "PageTransparency should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "PageTransparency")
    assert flat is not None
    assert flat["page_id"] == "20531316728"
    assert flat["name"]  # Meta page must carry a non-empty name
