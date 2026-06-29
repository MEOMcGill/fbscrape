"""GroupTimeline hybrid — headless scrape of a public group.

Mirrors `test_user_timeline_hybrid.py`. Differences specific to GroupTimeline:
  - No server-side date filter, so `--end-date` is advisory; termination
    relies on client-side `creation_time < start_unix`.
  - `cursor_reset` is terminal here (no multi-leg resume), so it counts as
    a successful end-state alongside the usual ones.
  - Flatten routes through "GroupTimeline" (alias over the UserTimeline
    flattener, but routing through the registry keeps the wiring honest).
"""


import datetime

import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


# Public group; substitute if it goes dark. Keep in sync with the fixture
# target in tests/_capture_fixtures.py.
GROUP_HANDLE = "albertaseparatism"
end_date = datetime.date.today()
start_date = end_date - datetime.timedelta(days=7)

end_date_str = end_date.strftime("%Y-%m-%d")
start_date_str = start_date.strftime("%Y-%m-%d")

async def test_group_timeline_hybrid_returns_posts(fb_scraper):
    result = await fb_scraper.group_timeline(
        GROUP_HANDLE, start_date_str, end_date_str, max_posts=20
    )
    assert result.result in {
        "success",
        "scraped until user-specified starting date was reached",
        # cursor_reset is terminal for GroupTimeline (no server-side date
        # filter to advance), so it's a valid end-state with partial data.
        "cursor_reset",
        # max_posts cap was hit at a batch boundary — clean termination
        # with up to `pagination_count - 1` over-delivery vs. the cap.
        "max_posts_reached",
    }
    assert len(result.data) > 0

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "GroupTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened from a real group scrape — shape may have drifted"

    # Every flattened post must have a post_id and either url or permalink_url.
    for r in flattened:
        assert r["post_id"]
        assert r["url"] or r["permalink_url"]

    # post_id dedup invariant — proves ResponseInterceptor.add_posts is
    # actually filtering re-served bootstrap-edge posts across paginations.
    post_ids = [r["post_id"] for r in flattened]
    assert len(post_ids) == len(set(post_ids)), (
        f"duplicate post_ids across paginations: {len(post_ids) - len(set(post_ids))} dupes"
    )
