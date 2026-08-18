"""UserTimeline hybrid — open-ended (no start_date) scrape.

Exercises the date-free path on UserTimeline. Unlike GroupTimeline, FB's
UserTimeline API has a server-side `beforeTime` filter, so the high-level
scraper signature still accepts `end_date` (the CLI layer auto-fills today
to mirror FB's UI fingerprint). Direct API callers may pass `end_date=None`
to skip `beforeTime` injection entirely.

This test mirrors what the CLI produces when neither `--start-date` nor
`--end-date` is passed: start=None, end=today.
"""


import datetime

import pytest

from fbscrape.response import FacebookGraphQLParser


pytestmark = pytest.mark.integration


HANDLE = "zuck"  # public, never disappears


async def test_user_timeline_open_terminates_on_max_posts(fb_scraper):
    """No start_date, end auto-filled to today (mirrors CLI). Should bail
    on `max_posts_reached` without touching `OldestInBatchBelowStartDate`."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    result = await fb_scraper.user_timeline(
        HANDLE,
        start_date=None,
        end_date=today,
        max_posts=10,
    )

    assert result.query.query.get("start_date") is None
    assert result.query.query.get("end_date") == today

    assert result.result in {
        "max_posts_reached",
        "no_new_posts_streak",
        "scraped until user-specified starting date was reached",
        "success",
    }, f"unexpected terminal result: {result.result!r}"

    pc = result.query.params.get("pagination_count", 3)
    assert 0 < len(result.data) <= 10 + (pc - 1)

    parser = FacebookGraphQLParser()
    flattened = [parser.flatten(r, "UserTimeline") for r in result.data]
    flattened = [r for r in flattened if r is not None]
    assert flattened, "no records flattened — Story shape may have drifted"
    for r in flattened:
        assert r["post_id"]
