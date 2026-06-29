"""GroupTimeline hybrid — TOP_POSTS sort + ConsecutiveOutOfRange stop.

Parallels `test_group_timeline_hybrid.py` but exercises the non-chronological
path: FB's UI default sort (`TOP_POSTS`) with `ConsecutiveOutOfRange` as the
primary date-tail stop. Under TOP_POSTS, posts arrive non-monotonically by
creation_time — the date-stop framework cannot rely on `oldest_in_batch <
start_unix`, so the run terminates via `consecutive_out_of_range`,
`no_new_posts_streak`, or the natural end-of-feed.

Verifies the new default works end-to-end against a real group without
depending on FB returning posts in any particular order.
"""


import datetime

import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


GROUP_HANDLE = "albertaseparatism"
# Tight window — under TOP_POSTS, FB may return mostly out-of-range posts,
# which lets `consecutive_out_of_range` actually fire instead of the run
# completing without exercising the new stop.
end_date = datetime.date.today() - datetime.timedelta(days=30)
start_date = end_date - datetime.timedelta(days=7)
end_date_str = end_date.strftime("%Y-%m-%d")
start_date_str = start_date.strftime("%Y-%m-%d")


async def test_group_timeline_top_posts_terminates_cleanly(fb_scraper):
    """TOP_POSTS scrape with a tight, recent-but-not-current window. Should
    terminate via one of the non-chronological-friendly stops without
    crashing."""
    result = await fb_scraper.group_timeline(
        GROUP_HANDLE,
        start_date_str,
        end_date_str,
        sorting_setting="TOP_POSTS",
        max_posts=30,
        # Low cap so even if FB serves mostly stale posts, the test
        # terminates promptly via consecutive_out_of_range.
        max_consecutive_out_of_range=10,
    )
    # `consecutive_out_of_range` is the headline outcome under TOP_POSTS;
    # the run may also terminate via other clean stops depending on what
    # FB serves on the day the test runs.
    assert result.result in {
        "consecutive_out_of_range",
        "scraped until user-specified starting date was reached",
        "no_new_posts_streak",
        "max_posts_reached",
        "success",
    }, f"unexpected terminal result: {result.result!r}"

    # The scrape's `sorting_setting` was carried into the saved Query.
    assert result.query.params["sorting_setting"] == "TOP_POSTS"
    assert result.query.params["max_consecutive_out_of_range"] == 10

    # Some posts should have been collected — even under TOP_POSTS, FB
    # serves at least the bootstrap batch.
    assert len(result.data) > 0

    # Sanity: every flattened record carries the basics.
    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "GroupTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened — Story shape may have drifted under TOP_POSTS"
    for r in flattened:
        assert r["post_id"]
        assert r["url"] or r["permalink_url"]

    # Dedup invariant survives the non-chronological path.
    post_ids = [r["post_id"] for r in flattened]
    assert len(post_ids) == len(set(post_ids)), (
        f"duplicate post_ids across paginations: {len(post_ids) - len(set(post_ids))} dupes"
    )


async def test_group_timeline_default_sort_is_top_posts(fb_scraper):
    """Verify the registry default carries through end-to-end."""
    result = await fb_scraper.group_timeline(
        GROUP_HANDLE,
        start_date_str,
        end_date_str,
        max_posts=15,
    )
    assert result.query.params["sorting_setting"] == "TOP_POSTS"
    assert result.query.params["max_consecutive_out_of_range"] == 20
