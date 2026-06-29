"""Flatten UserTimeline records against captured fixtures — endpoint-specific
behavior only.

The structural invariants shared with GroupTimeline and Search (key set,
post_id/url, timestamps, attachments/entities/top-comments shape, dedup) live
in `test_flatten_comet_contract.py`, which runs them once per Comet endpoint.
What remains here is genuinely UserTimeline-specific and does not generalize:
  - the `_extract_engagement` variant A/B unit tests (synthetic stories);
  - the curated `variant_b` fixture suite (engagement-only-in-renderers shape);
  - the author-is-zuck identity pin and the reactions-total invariant;
  - the shared_post (repost) shape check.

These tests load a single fresh capture of `zuck`'s timeline (any 6-month
window) and walk it for shape diversity, asserting invariants the flattener
must honor regardless of which specific records FB serves back.
"""


import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip
from tests.unit.test_flatten_comet_contract import COMET_KEYS

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "user_timeline_hybrid"

# The flattened-row key set is shared across all Comet endpoints; the variant_b
# suite below reuses it.
EXPECTED_KEYS = COMET_KEYS


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


def test_reactions_total_matches_per_type_sum_when_present(flat_rows):
    """`reactions` is the post's total reaction count. When the per-type
    breakdown is non-zero, the total must equal their sum — FB exposes
    exactly 7 reaction types so the sum is exhaustive. If the dedicated
    `feedback.reaction_count.count` field is absent (~60% of responses),
    the extractor falls back to summing the per-type edges; this test
    locks down that invariant from both sides."""
    PER_TYPE = ("like", "love", "haha", "wow", "sad", "angry", "care")
    for r in flat_rows:
        per_type_sum = sum(r[k] for k in PER_TYPE)
        if per_type_sum > 0:
            assert r["reactions"] == per_type_sum, (
                f"post {r['post_id']}: reactions={r['reactions']} != "
                f"sum(per-type)={per_type_sum}"
            )


def test_reactions_fallback_to_per_type_sum():
    """Unit test for the fallback path: a synthetic Story with
    `top_reactions.edges` populated but no `reaction_count.count` should
    still produce a `reactions` total."""
    story = {
        "comet_sections": {
            "feedback": {"story": {"story_ufi_container": {"story": {
                "feedback_context": {"feedback_target_with_context": {
                    "comet_ufi_summary_and_actions_renderer": {"feedback": {
                        # Note: NO `reaction_count` key — only edges.
                        "top_reactions": {"edges": [
                            {"node": {"localized_name": "Like"},  "reaction_count": 100},
                            {"node": {"localized_name": "Love"},  "reaction_count": 25},
                            {"node": {"localized_name": "Haha"},  "reaction_count": 3},
                        ]},
                    }},
                }},
            }}}},
        },
    }
    out = PARSER._extract_engagement(story)
    assert out["reactions"] == 128
    assert out["like"] == 100
    assert out["love"] == 25
    assert out["haha"] == 3
    for k in ("wow", "sad", "angry", "care"):
        assert out[k] == 0


def test_reactions_prefers_explicit_total_when_present():
    """When `reaction_count.count` is present, the extractor uses it
    verbatim — the fallback only kicks in when it's None."""
    story = {
        "comet_sections": {
            "feedback": {"story": {"story_ufi_container": {"story": {
                "feedback_context": {"feedback_target_with_context": {
                    "comet_ufi_summary_and_actions_renderer": {"feedback": {
                        "reaction_count": {"count": 500},
                        "top_reactions": {"edges": [
                            {"node": {"localized_name": "Like"}, "reaction_count": 100},
                        ]},
                    }},
                }},
            }}}},
        },
    }
    out = PARSER._extract_engagement(story)
    assert out["reactions"] == 500  # not 100 (the per-type sum)


def test_reactions_none_when_no_feedback_at_all():
    """A Story with no feedback object yields reactions=None (not 0),
    distinguishing 'feedback missing' from 'feedback present, 0 reactions'."""
    story = {"comet_sections": {}}
    out = PARSER._extract_engagement(story)
    assert out["reactions"] is None
    for k in ("like", "love", "haha", "wow", "sad", "angry", "care"):
        assert out[k] == 0


def _story_with_summary_feedback(feedback_dict: dict) -> dict:
    """Build the deeply-nested skeleton FB ships, with `feedback_dict` plugged
    in at `comet_ufi_summary_and_actions_renderer.feedback`."""
    return {
        "comet_sections": {
            "feedback": {"story": {"story_ufi_container": {"story": {
                "feedback_context": {"feedback_target_with_context": {
                    "comet_ufi_summary_and_actions_renderer": {"feedback": feedback_dict},
                }},
            }}}},
        },
    }


def test_engagement_variant_b_adaptive_renderers():
    """Variant B (~60% of real responses): top-level totals are absent and
    reactions/comments/shares only live inside `adaptive_ufi_action_renderers[]`,
    dispatched by `__typename`. The extractor must walk the renderer list
    and pull the totals from there."""
    sf = {
        # NO top-level reaction_count, share_count, or comments_count_summary_renderer.
        "top_reactions": {"edges": [
            {"node": {"localized_name": "Like"}, "reaction_count": 135},
            {"node": {"localized_name": "Love"}, "reaction_count": 21},
        ]},
        "adaptive_ufi_action_renderers": [
            {"__typename": "UFIStoryReactActionRenderer",
             "feedback": {"reaction_count": {"count": 165}}},
            {"__typename": "UFICommentActionRenderer",
             "feedback": {"comment_rendering_instance": {"comments": {"total_count": 24}}}},
            {"__typename": "XFBUFIAdaptiveShareActionRenderer",
             "feedback": {"share_count": {"count": 6}}},
        ],
    }
    out = PARSER._extract_engagement(_story_with_summary_feedback(sf))
    assert out["reactions"] == 165   # NOT 156 (per-type sum) — renderer wins
    assert out["like"]      == 135
    assert out["love"]      == 21
    assert out["comments"]  == 24
    assert out["shares"]    == 6


def test_engagement_variant_a_takes_priority_over_renderers():
    """When both top-level fields AND adaptive renderers are present (Variant A
    shape — FB ships both), the top-level values are used. Locks down the
    intended precedence so a future Variant B regression doesn't silently
    override a known-good Variant A value."""
    sf = {
        "reaction_count": {"count": 500},
        "share_count":    {"count": 50},
        "comments_count_summary_renderer": {"feedback": {
            "comment_rendering_instance": {"comments": {"total_count": 12}},
        }},
        "adaptive_ufi_action_renderers": [
            # Wrong values that must NOT win.
            {"__typename": "UFIStoryReactActionRenderer",
             "feedback": {"reaction_count": {"count": 999}}},
            {"__typename": "XFBUFIAdaptiveShareActionRenderer",
             "feedback": {"share_count": {"count": 999}}},
        ],
    }
    out = PARSER._extract_engagement(_story_with_summary_feedback(sf))
    assert out["reactions"] == 500
    assert out["shares"]    == 50
    assert out["comments"]  == 12


def test_engagement_variant_b_partial_renderers():
    """A Variant B response that's missing a specific renderer (e.g. shares
    disabled) yields None for that field — not a crash, not a wrong value."""
    sf = {
        "adaptive_ufi_action_renderers": [
            {"__typename": "UFIStoryReactActionRenderer",
             "feedback": {"reaction_count": {"count": 7}}},
            # No comment renderer, no share renderer.
        ],
    }
    out = PARSER._extract_engagement(_story_with_summary_feedback(sf))
    assert out["reactions"] == 7
    assert out["comments"] is None
    assert out["shares"] is None


# ---------------------------------------------------------------------------
# Variant B coverage from the curated fixture
# ---------------------------------------------------------------------------
#
# `user_timeline_hybrid_variant_b.json` is a hand-curated capture of 6 records
# from the "canadian_fb_slop" dataset, picked for structural diversity that
# `zuck`'s timeline doesn't expose:
#   - all 6 are Variant B (adaptive_ufi_action_renderers shape) — the engagement
#     bug at the top of this file shipped because the zuck fixture is 100% A;
#   - 1 text-only post with high reactions, 1 photo with top_comments +
#     external_urls, 1 with hashtags, 1 video (non-reel), 1 album, 1 reel;
#   - 5 distinct author_ids.
#
# These records can't be auto-recaptured (they aren't on a stable public
# profile), so the file is committed verbatim. If FB drops support for one of
# the shapes we want to keep testing against, replace from raw saves rather
# than re-running `_capture_fixtures.py`.

VARIANT_B_FIXTURE = "user_timeline_hybrid_variant_b"


@pytest.fixture(scope="module")
def records_variant_b():
    data = load_fixture_or_skip(VARIANT_B_FIXTURE)
    return data.get("data") or data.get("posts") or []


@pytest.fixture(scope="module")
def flat_rows_variant_b(records_variant_b):
    rows = [PARSER.flatten(r, "UserTimeline") for r in records_variant_b]
    rows = [r for r in rows if r is not None]
    if not rows:
        pytest.skip("no records flattened from variant_b fixture")
    return rows


def test_variant_b_all_records_flatten(records_variant_b, flat_rows_variant_b):
    assert len(flat_rows_variant_b) == len(records_variant_b)


def test_variant_b_every_row_has_full_key_set(flat_rows_variant_b):
    for i, row in enumerate(flat_rows_variant_b):
        missing = EXPECTED_KEYS - row.keys()
        assert not missing, f"row {i} missing keys: {missing}"


def test_variant_b_engagement_populated_for_all(flat_rows_variant_b):
    """The whole point of this fixture: every record must produce non-null
    `reactions` / `shares` / `comments`. If any are null, the adaptive-
    renderer dispatch in `_extract_engagement` has regressed."""
    for r in flat_rows_variant_b:
        assert r["reactions"] is not None, f"reactions null for post {r['post_id']}"
        assert r["shares"]    is not None, f"shares null for post {r['post_id']}"
        assert r["comments"]  is not None, f"comments null for post {r['post_id']}"


def test_variant_b_records_are_actually_variant_b(records_variant_b):
    """Tripwire: if a future fixture refresh accidentally swaps in Variant A
    records, this test catches it before `test_variant_b_engagement_populated_for_all`
    starts passing for the wrong reason."""
    for i, rec in enumerate(records_variant_b):
        try:
            story = rec["node"]["timeline_list_feed_units"]["edges"][0]["node"]
        except (KeyError, IndexError, TypeError):
            story = rec.get("node")
        sf = PARSER._summary_feedback(story or {})
        assert "reaction_count" not in sf, (
            f"record {i} has top-level reaction_count — it's Variant A, not B. "
            f"Replace with a record where engagement only lives inside "
            f"adaptive_ufi_action_renderers[]."
        )
        assert sf.get("adaptive_ufi_action_renderers"), (
            f"record {i} has no adaptive_ufi_action_renderers — not Variant B."
        )


def test_variant_b_covers_structural_diversity(flat_rows_variant_b):
    """The curated set is supposed to span multiple post types. If a future
    fixture trim drops all the diversity, downstream tests would silently
    cover less ground — fail loudly here so the diversity is preserved."""
    has_reel        = any(r["is_reel"]                          for r in flat_rows_variant_b)
    has_hashtags    = any(r["hashtags"]                         for r in flat_rows_variant_b)
    has_top_comm    = any(r["top_comments"]                     for r in flat_rows_variant_b)
    has_photo       = any(any(a["type"] == "photo" for a in (r["attachments"] or []))
                          for r in flat_rows_variant_b)
    has_album       = any(any(a["type"] == "album" for a in (r["attachments"] or []))
                          for r in flat_rows_variant_b)
    has_video       = any(any(a["type"] == "video" for a in (r["attachments"] or []))
                          for r in flat_rows_variant_b)
    distinct_authors = len({r["author_id"] for r in flat_rows_variant_b})

    assert has_reel,     "fixture lost: is_reel record"
    assert has_hashtags, "fixture lost: hashtags record"
    assert has_top_comm, "fixture lost: top_comments record"
    assert has_photo,    "fixture lost: photo attachment"
    assert has_album,    "fixture lost: album attachment"
    assert has_video,    "fixture lost: video attachment"
    assert distinct_authors >= 4, f"fixture lost author diversity: only {distinct_authors} authors"


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
