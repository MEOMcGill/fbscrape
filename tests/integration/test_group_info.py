"""GroupInfo single-shot — headless against a public group."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_group_info(fb_scraper):
    result = await fb_scraper.group_info("albertaseparatism")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "GroupInfo should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "GroupInfo")
    assert flat is not None
    assert flat["name"]
    assert flat["member_count"]
