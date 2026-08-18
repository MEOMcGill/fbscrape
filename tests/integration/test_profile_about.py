"""ProfileAbout single-shot — headless against a public Page."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_profile_about_page(fb_scraper):
    result = await fb_scraper.profile_about("61582991935083")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "ProfileAbout should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "ProfileAbout")
    assert flat is not None
    assert flat["name"]
    assert flat["phone"] or flat["email"] or flat["website"]
