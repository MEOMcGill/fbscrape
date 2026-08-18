"""ProfileAuthenticity single-shot — headless against zuck's profile."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_profile_authenticity_zuck(fb_scraper):
    result = await fb_scraper.profile_authenticity("100044331674441")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "ProfileAuthenticity should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "ProfileAuthenticity")
    assert flat is not None
    assert flat["user_id"] == "100044331674441"
    assert flat["name"]
