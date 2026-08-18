"""PostDetail single-shot — headless against a public group-post permalink.

Fetches one post by its permalink and confirms the server-rendered Story is
extracted and flattens. Uses a public Alberta-politics group post (the same
target the fixture was captured from). Like all integration tests this hits
real Facebook, so it can fail on shape drift or if the post is removed — that
signal is the point of the integration tier.
"""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration

HANDLE = "albertansunitedtostoptheucp"
POST_ID = "27209929835285847"


async def test_post_detail_group_post(fb_scraper):
    result = await fb_scraper.post_detail(HANDLE, POST_ID, is_group=True)
    assert result.result == "success", f"unexpected result: {result.result}"
    assert len(result.data) == 1, "PostDetail should return exactly one record"

    parser = FacebookGraphQLParser()
    flat = parser.flatten(result.data[0], "PostDetail")
    assert flat is not None
    assert flat["post_id"] == POST_ID
    assert flat["author_name"]        # a real post has an author
    assert flat["created_at_utc"]     # ...and a creation timestamp
