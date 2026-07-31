"""ProfileInfo single-shot — headless against zuck's profile."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_profile_info_zuck(fb_scraper):
    result = await fb_scraper.profile_info("zuck")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "ProfileInfo should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "ProfileInfo")
    assert flat is not None
    assert flat["name"]
    assert flat["follower_count_text"]
