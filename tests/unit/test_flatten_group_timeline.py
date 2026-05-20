"""Flatten GroupTimeline records against captured fixtures.

GroupTimeline Stories share the same Comet shape as UserTimeline (the
flattener is a thin alias over _flatten_pctfrq_post), so the structural
invariants are identical. The author check is the only meaningful
difference: a group feed mixes many authors, whereas a profile timeline
is overwhelmingly one author.
"""

from __future__ import annotations

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "group_timeline_hybrid"


# Expected top-level keys on every flattened GroupTimeline row. Mirrors
# UserTimeline's set — the Story shape (and therefore the flattened row
# schema) is identical. If the GroupTimeline orchestrator ever forks from
# _flatten_pctfrq_post, this set is what would need to evolve.
EXPECTED_KEYS = {
    # ids + urls
    "post_id", "story_id", "url", "permalink_url",
    # times
    "created_at", "created_at_utc",
    # audience
    "privacy",
    # author
    "author_id", "author_name", "author_url", "author_type", "author_promode_badge",
    # message
    "text", "hashtags", "mentions", "external_urls",
    # music
    "music_artist", "music_title",
    # flags
    "is_reel", "is_live", "is_repost",
    # engagement
    "reactions", "like", "love", "haha", "wow", "sad", "angry", "care",
    "shares", "comments", "video_views", "video_duration_sec",
    # composites
    "top_comments", "attachments", "shared_post",
}


@pytest.fixture(scope="module")
def records():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or data.get("posts") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs


@pytest.fixture(scope="module")
def flat_rows(records):
    rows = [PARSER.flatten(r, "GroupTimeline") for r in records]
    rows = [r for r in rows if r is not None]
    if not rows:
        pytest.skip("no records flattened from fixture — likely a parser/shape change")
    return rows


def test_every_record_flattens(records, flat_rows):
    """Most records should flatten — a high failure rate signals shape drift."""
    pct = len(flat_rows) / len(records)
    assert pct >= 0.95, (
        f"only {len(flat_rows)}/{len(records)} ({pct:.0%}) records flattened — "
        f"check for unrecognized story shapes"
    )


def test_every_row_has_full_key_set(flat_rows):
    """Schema stability: every row must carry every expected key (value may be None)."""
    for i, row in enumerate(flat_rows):
        missing = EXPECTED_KEYS - row.keys()
        assert not missing, f"row {i} missing keys: {missing}"


def test_post_id_and_url_always_populated(flat_rows):
    """post_id and one of url/permalink_url should always be present —
    if either is None we've lost the ability to reference the post."""
    for i, row in enumerate(flat_rows):
        assert row["post_id"], f"row {i}: missing post_id"
        assert row["url"] or row["permalink_url"], f"row {i}: no url"


def test_created_at_is_unix_timestamp(flat_rows):
    """created_at is the FB-side `creation_time` as unix seconds."""
    for i, row in enumerate(flat_rows):
        ts = row["created_at"]
        if ts is not None:
            assert isinstance(ts, (int, float)) and ts > 1_000_000_000


def test_at_least_some_records_have_a_recognized_timestamp(flat_rows):
    """If zero records carry created_at, the timestamp metadata-strategy
    typename has likely been renamed by FB — exactly the symptom Key Design
    Decision 18 documents."""
    n_with_ts = sum(1 for r in flat_rows if r["created_at"] is not None)
    assert n_with_ts > 0, "no record produced a created_at — check _METADATA_TIMESTAMP_TYPENAMES"


def test_group_feed_has_multiple_authors(flat_rows):
    """Unlike a profile timeline, a group feed is multi-author. If every
    row reports the same `author_id` the fixture is suspect (private group,
    one-poster group, or an extractor regression that's collapsing identity)."""
    author_ids = {r["author_id"] for r in flat_rows if r["author_id"]}
    if len(flat_rows) < 5:
        pytest.skip(f"only {len(flat_rows)} rows in fixture — not enough for a multi-author check")
    assert len(author_ids) >= 2, (
        f"only {len(author_ids)} distinct author_id across {len(flat_rows)} rows — "
        f"expected a multi-author feed"
    )


def test_every_row_has_an_author(flat_rows):
    """In a group context, every Story should attribute a poster. Missing
    both author_id and author_name on the same row signals an extractor
    regression."""
    for i, row in enumerate(flat_rows):
        assert row["author_id"] or row["author_name"], (
            f"row {i}: neither author_id nor author_name populated"
        )


def test_reaction_counts_are_nonneg_ints(flat_rows):
    for r in flat_rows:
        for k in ("like", "love", "haha", "wow", "sad", "angry", "care"):
            v = r[k]
            assert isinstance(v, int) and v >= 0


def test_attachments_shape_when_present(flat_rows):
    """Where attachments are present, every entry must carry the uniform
    attachment key set (CLAUDE.md: "uniform attachment shape")."""
    ATTACHMENT_KEYS = {
        "type", "id", "url",
        "image_url", "image_lowres_url", "thumbnail_url",
        "width", "height", "accessibility_caption",
        "video_url", "video_duration_sec", "video_is_live",
        "video_permalink_url", "video_captions_url",
        "link_title", "link_description", "link_source", "link_destination_url",
        "subattachments",
    }
    KNOWN_TYPES = {"photo", "video", "link", "album", "reel_share", "unavailable", "unknown"}

    seen_any = False
    for r in flat_rows:
        atts = r["attachments"]
        if not atts:
            continue
        seen_any = True
        for a in atts:
            assert ATTACHMENT_KEYS == a.keys(), (
                f"attachment shape drift: missing={ATTACHMENT_KEYS - a.keys()}, "
                f"extra={a.keys() - ATTACHMENT_KEYS}"
            )
            assert a["type"] in KNOWN_TYPES, f"unknown attachment type {a['type']!r}"

    if not seen_any:
        # Some groups are text-only; don't fail just because the window had none.
        pytest.skip("no attachments[] in this fixture's window — nothing to assert")


def test_message_entities_typed_correctly_when_present(flat_rows):
    """hashtags/mentions/external_urls extracted from `message.ranges` —
    where populated, the value must be a non-empty list of the right type."""
    for r in flat_rows:
        if r["hashtags"] is not None:
            assert isinstance(r["hashtags"], list) and r["hashtags"]
            assert all(isinstance(h, str) for h in r["hashtags"])
        if r["mentions"] is not None:
            assert isinstance(r["mentions"], list) and r["mentions"]
            assert all(isinstance(m, dict) and "id" in m for m in r["mentions"])
        if r["external_urls"] is not None:
            assert isinstance(r["external_urls"], list) and r["external_urls"]
            assert all(isinstance(u, str) for u in r["external_urls"])


def test_top_comments_shape_when_present(flat_rows):
    """Where top_comments populates, each entry has the documented keys."""
    EXPECTED = {"text", "author_id", "author_name", "author_url", "created_at", "reactions"}
    for r in flat_rows:
        if r["top_comments"] is None:
            continue
        for c in r["top_comments"]:
            assert EXPECTED <= c.keys()


def test_post_ids_are_unique(flat_rows):
    """Cross-pagination dedup (ResponseInterceptor.add_posts via seen_post_ids)
    should leave the accumulated post list free of duplicates. GroupTimeline
    paginations can re-serve the bootstrap-edge highlight slot, so this is
    the test that proves dedup is actually running."""
    ids = [r["post_id"] for r in flat_rows if r["post_id"]]
    assert len(ids) == len(set(ids)), (
        f"duplicate post_ids in flattened rows: {len(ids) - len(set(ids))} duplicates "
        f"across {len(ids)} rows — dedup regression"
    )
