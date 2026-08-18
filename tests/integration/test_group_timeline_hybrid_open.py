"""GroupTimeline hybrid — open-ended (no dates) scrape.

Exercises the date-free path: both `start_date` and `end_date` omitted.
Date-bounded stop conditions (`OldestInBatchBelowStartDate`,
`ConsecutiveOutOfRange`) should no-op via their existing None guards, and
the scrape should terminate via `MaxPostsReached` (driven by `max_posts`).
"""


import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


GROUP_HANDLE = "392585550772135"


async def test_group_timeline_open_terminates_on_max_posts(fb_scraper):
    """No dates, modest max_posts cap. Should bail on `max_posts_reached`
    without ever touching date-bounded stops."""
    result = await fb_scraper.group_timeline(
        GROUP_HANDLE,
        start_date=None,
        end_date=None,
        sorting_setting="TOP_POSTS",
        max_posts=10,
        max_consecutive_out_of_range=-1,  # explicitly disable to remove ambiguity
    )

    # The saved Query absorbs the None values rather than dropping the keys.
    assert result.query.query.get("start_date") is None
    assert result.query.query.get("end_date") is None

    # Acceptable terminal results: max_posts hit, or natural end-of-feed /
    # no-new-posts if the group is small.
    assert result.result in {
        "max_posts_reached",
        "no_new_posts_streak",
        "scraped until user-specified starting date was reached",  # EndOfFeed string
        "success",
    }, f"unexpected terminal result: {result.result!r}"

    # max_posts is batch-boundary enforced — overshoot up to pagination_count - 1.
    pc = result.query.params.get("pagination_count", 3)
    assert 0 < len(result.data) <= 10 + (pc - 1)

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "GroupTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened — Story shape may have drifted"
    for r in flattened:
        assert r["post_id"]
