"""CommentsList hybrid — headless scrape of a public post's top-level comments.

Mirrors `test_group_timeline_hybrid.py`. Differences specific to CommentsList:
  - Identifier is (handle, post_id), not a vanity handle alone.
  - No date filtering — termination is exhaustion + max_results cap.
  - Records are Comment-shaped, not Story-shaped; flatten routes through
    "CommentsList".
"""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


# Public post; substitute if it goes dark. Keep in sync with the fixture
# target in tests/_capture_fixtures.py.
HANDLE = "MarkJCarney2025"
POST_ID = (
    "pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl"
)


async def test_comments_list_hybrid_returns_comments(fb_scraper):
    # Cap at ~30 comments so the test stays bounded — `max_results` overshoots
    # by up to ~one page, so 30 → ~30–40 actual records.
    result = await fb_scraper.comments_list(
        HANDLE, POST_ID, max_results=30,
    )
    assert result.result in {
        "success",
        # Natural exhaustion (FB returned has_next_page=false before the cap).
        "scraped until user-specified starting date was reached",
        # Cap fired at a batch boundary.
        "max_posts_reached",
    }
    assert len(result.data) > 0

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "CommentsList") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened from a real comments scrape — shape may have drifted"

    # Every comment must have a numeric comment_id + parent post_feedback_id.
    parent_ids = set()
    for r in flattened:
        assert r["comment_id"]
        assert r["post_feedback_id"]
        parent_ids.add(r["post_feedback_id"])

    # All comments in a single-post scrape should share the same parent.
    assert len(parent_ids) == 1, (
        f"expected one parent post; got {parent_ids}"
    )

    # comment_id dedup invariant — proves add_posts is filtering re-served
    # comments across paginations (FB occasionally repeats edges).
    comment_ids = [r["comment_id"] for r in flattened]
    assert len(comment_ids) == len(set(comment_ids)), (
        f"duplicate comment_ids across paginations: "
        f"{len(comment_ids) - len(set(comment_ids))} dupes"
    )
