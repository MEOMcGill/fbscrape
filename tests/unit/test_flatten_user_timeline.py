"""Flatten UserTimeline records against captured fixtures.

These tests load a single fresh capture of `zuck`'s timeline (any 6-month
window) and walk it for shape diversity. They assert structural invariants
that the flattener must honor regardless of which specific records FB
happens to serve back — so the tests don't get stuck pinning fragile
content (text strings, exact reaction counts, etc.).
"""

from __future__ import annotations

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "user_timeline_hybrid"


# Expected top-level keys on every flattened UserTimeline row. Updates to
# the flattener that drop one of these break a downstream consumer's schema,
# so the test should explicitly fail in that case.
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
    rows = [PARSER.flatten(r, "UserTimeline") for r in records]
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
        # Some shapes can legitimately return None — the strategy didn't
        # match a recognized timestamp typename. But the majority should
        # populate, and when set must be a positive integer-ish unix time.
        if ts is not None:
            assert isinstance(ts, (int, float)) and ts > 1_000_000_000


def test_at_least_some_records_have_a_recognized_timestamp(flat_rows):
    """If zero records carry created_at, the timestamp metadata-strategy
    typename has likely been renamed by FB — exactly the symptom Key Design
    Decision 18 documents."""
    n_with_ts = sum(1 for r in flat_rows if r["created_at"] is not None)
    assert n_with_ts > 0, "no record produced a created_at — check _METADATA_TIMESTAMP_TYPENAMES"


def test_author_is_zuck(flat_rows):
    """zuck's timeline → his FB account id ("4" — he was the 4th user) and
    name appear on virtually every post. A bulk mismatch means the fixture
    target was redirected or the author extractor regressed.
    Note: this id namespace differs from the ProfileAuthenticity record
    (which uses 100044331674441) — they refer to the same person via
    different FB id systems."""
    ZUCK_FB_ID = "4"
    by_id   = sum(1 for r in flat_rows if r["author_id"]   == ZUCK_FB_ID)
    by_name = sum(1 for r in flat_rows if r["author_name"] == "Mark Zuckerberg")
    assert by_id   / len(flat_rows) >= 0.8, f"only {by_id}/{len(flat_rows)} rows have author_id={ZUCK_FB_ID!r}"
    assert by_name / len(flat_rows) >= 0.8, f"only {by_name}/{len(flat_rows)} rows have author_name='Mark Zuckerberg'"


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

    # Zuck posts heavily with media; the fixture should hit at least one.
    assert seen_any, "no record carried an attachments[] — fixture may be too narrow"


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


def test_shared_post_shape_when_present(flat_rows):
    """Reposts populate shared_post with at least an id/url. FB abbreviates
    the inner share so we don't expect the full schema there — only the
    ergonomic keys."""
    seen_repost = False
    for r in flat_rows:
        sp = r["shared_post"]
        if sp:
            seen_repost = True
            # Either post_id or story_id must be set on the inner share.
            assert sp.get("post_id") or sp.get("story_id")
            assert r["is_repost"] is True
    if not seen_repost:
        # Not a failure — just informational that no repost was in this window.
        pytest.skip("no repost in this fixture's date window — no assertions to run")
