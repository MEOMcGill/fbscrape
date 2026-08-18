"""Flatten CommentsList records against a captured fixture.

The CommentsListComponentsPaginationQuery response shape is distinct from
the Story-based timeline endpoints: each record is a Comment node (not a
Story), reactions ship without `localized_name` (only an id), and the
parent post's feedback id lives at the response top-level rather than in
each record. The unit tests exercise the orchestrator + each
`_extract_comment_*` helper indirectly through the flattened row schema.
"""


import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "comments_list_hybrid"


EXPECTED_KEYS = {
    # ids + url
    "comment_id", "comment_id_b64", "comment_feedback_id_b64",
    "post_feedback_id", "post_feedback_id_b64",
    "depth", "parent_comment_id_b64", "url",
    # times
    "created_at", "created_at_utc",
    # author
    "author_id", "author_name", "author_url", "author_type",
    # body + entities + translation
    "text", "hashtags", "mentions", "external_urls",
    "translated_text", "translation_type", "is_disabled",
    # reactions
    "reactions", "like", "love", "haha", "wow", "sad", "angry", "care",
    "reactions_other",
    # replies
    "replies_total_count", "replies_count",
    # composites
    "attachments",
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
    rows = [PARSER.flatten(r, "CommentsList") for r in records]
    rows = [r for r in rows if r is not None]
    if not rows:
        pytest.skip("no records flattened from fixture — likely a parser/shape change")
    return rows


def test_every_record_flattens(records, flat_rows):
    pct = len(flat_rows) / len(records)
    assert len(flat_rows) == len(records), (
        f"only {len(flat_rows)}/{len(records)} ({pct:.0%}) records flattened — "
        f"check for unrecognized comment shapes"
    )


def test_every_row_has_full_key_set(flat_rows):
    for i, row in enumerate(flat_rows):
        missing = EXPECTED_KEYS - row.keys()
        assert not missing, f"row {i} missing keys: {missing}"


def test_top_level_only_in_v1(flat_rows):
    """v1 scrapes top-level comments only — every row should be depth=0."""
    depths = {row["depth"] for row in flat_rows}
    assert depths == {0}, f"expected only depth=0 rows, got {depths}"


def test_required_ids_present(flat_rows):
    """Every row needs at least: a numeric comment_id and a numeric parent post_feedback_id.

    These are the contract that downstream pipelines join on.
    """
    for i, row in enumerate(flat_rows):
        assert row["comment_id"], f"row {i} missing comment_id"
        assert row["comment_id"].isdigit(), f"row {i} comment_id {row['comment_id']!r} not numeric"
        assert row["post_feedback_id"], f"row {i} missing post_feedback_id"
        assert row["post_feedback_id"].isdigit(), f"row {i} post_feedback_id not numeric"
        # All comments on one post should share the same parent feedback id.
    parent_ids = {row["post_feedback_id"] for row in flat_rows}
    assert len(parent_ids) == 1, (
        f"expected all comments to share one parent post; got {parent_ids}"
    )


def test_reaction_ids_mapped(flat_rows):
    """At least one row should have a non-zero `like` count.

    The canonical reaction-id → name lookup table is hardcoded in the
    flattener since FB doesn't ship `localized_name` on this endpoint.
    Any FB reaction-id change would silently zero out the breakdown,
    which this test guards against.
    """
    # Find a row with at least one mapped reaction.
    total_likes = sum(row["like"] or 0 for row in flat_rows)
    assert total_likes > 0, (
        "no rows had a non-zero `like` count — the reaction-id lookup "
        "may have drifted (FB rotated canonical ids), or the fixture has "
        "zero likes total (recapture against a more-engaged post)"
    )


def test_reactions_total_equals_breakdown_sum(flat_rows):
    """The aggregate `reactions` count should match the sum of the named
    reaction breakdown (plus the catch-all `reactions_other`) — the flattener
    falls back to sum when FB doesn't ship `reaction_count.count` on this
    endpoint, so a mismatch indicates a synthesis bug.
    """
    for i, row in enumerate(flat_rows):
        named_sum = sum(
            (row[k] or 0)
            for k in ("like", "love", "haha", "wow", "sad", "angry", "care")
        )
        other_sum = sum((row["reactions_other"] or {}).values())
        breakdown_total = named_sum + other_sum
        if breakdown_total == 0:
            # FB ships `reactions = None` when nothing's reacted; that's fine.
            assert row["reactions"] in (None, 0)
        else:
            assert row["reactions"] == breakdown_total, (
                f"row {i}: reactions={row['reactions']} but breakdown sums to "
                f"{breakdown_total} (named={named_sum} + other={other_sum})"
            )


def test_replies_total_count_is_int_or_none(flat_rows):
    """`replies_total_count` is the signal callers use to decide whether to
    drill into a separate reply-fetching endpoint. Must always be an int
    (FB ships 0 for no-reply comments, not None)."""
    for row in flat_rows:
        rtc = row["replies_total_count"]
        assert isinstance(rtc, int), (
            f"replies_total_count should be int, got {type(rtc).__name__}"
        )
