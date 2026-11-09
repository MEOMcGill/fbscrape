"""PostMediaSet — headless walk of a multi-photo post's photo set in the media viewer.

Targets an 8-photo post on a Quebec candidate's public page (its Story only carries
the first 5 subattachments). Checks a full walk returns all 8 items in order, then a
continuation from the 5th with the first 5 skipped returns exactly items 6–8 with file
indices continuing at 5. Like all integration tests this hits real Facebook.
"""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration

HANDLE = "IsabelleBlaisPQMaskinonge"
POST_ID = "2049017276045005"
FIRST_FIVE = [
    "2049016942711705", "2049017049378361", "2049017056045027", "2049017129378353", "2049016996045033",
]
ALBUM_SIZE = 8


async def test_full_walk_returns_whole_set(fb_scraper):
    result = await fb_scraper.post_media_set(POST_ID, FIRST_FIVE[0], handle=HANDLE, expected_count=ALBUM_SIZE)
    assert result.result == "success", f"unexpected result: {result.result}"
    parser = FacebookGraphQLParser()
    rows = [parser.flatten(r, "PostMediaSet") for r in result.data]
    assert [r["media_id"] for r in rows][:5] == FIRST_FIVE, "set order must match the Story's subattachments"
    assert len(rows) == ALBUM_SIZE
    assert all(r["post_id"] == POST_ID and r["image_url"] for r in rows)


async def test_continuation_past_the_cap(fb_scraper):
    result = await fb_scraper.post_media_set(
        POST_ID, FIRST_FIVE[-1], handle=HANDLE,
        skip_media_ids=FIRST_FIVE, expected_count=ALBUM_SIZE, media_index_offset=5,
    )
    assert result.result == "success", f"unexpected result: {result.result}"
    parser = FacebookGraphQLParser()
    rows = [parser.flatten(r, "PostMediaSet") for r in result.data]
    assert len(rows) == ALBUM_SIZE - 5
    assert not {r["media_id"] for r in rows} & set(FIRST_FIVE)
    assert [r["file_index"] for r in rows] == [5, 6, 7]
