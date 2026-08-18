"""GroupAbout single-shot — headless against a public group."""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


async def test_group_about(fb_scraper):
    result = await fb_scraper.group_about("392585550772135")
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "GroupAbout should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "GroupAbout")
    assert flat is not None
    assert flat["name"]
    assert flat["description"] or flat["rules"]
