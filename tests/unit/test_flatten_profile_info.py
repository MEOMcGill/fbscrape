"""Flatten ProfileInfo record against captured fixture (zuck)."""


import copy

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "profile_info"

EXPECTED_KEYS = {
    "profile_id", "name", "url", "gender", "username_for_profile",
    "is_verified", "is_viewer_friend", "is_memorialized",
    "follower_count_text", "followers_url", "following_count_text", "bio",
    "category", "intro_card_fields",
    "cover_photo_url", "profile_picture_url",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_profile_id_matches_target(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat["profile_id"] == "4"


def test_name_populated(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat["name"], "zuck's profile should have a name field"


def test_follower_count_text_populated(record):
    """Follower count only ships as an FB-formatted abbreviated string
    (e.g. '121M followers') — never an exact integer on this surface."""
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat["follower_count_text"]
    assert "follower" in flat["follower_count_text"].lower()


def test_bio_populated(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat["bio"], "zuck's profile should have a bio/status text"


def test_category_dispatched(record):
    """`category` (profile_field_type == "category") is the one intro-card
    field consistently present across public profiles."""
    flat = PARSER.flatten(record, "ProfileInfo")
    assert flat["category"]


def test_missing_intro_card_does_not_crash(record):
    """Robustness: a record with profile_intro_card stripped should still
    flatten cleanly with the dispatched keys all None/empty."""
    stripped = copy.deepcopy(record)
    header_top_row = stripped.get("header_top_row") or {}
    profile_user = header_top_row.get("profile_user") or {}
    profile_user["profile_intro_card"] = {}

    flat = PARSER.flatten(stripped, "ProfileInfo")
    assert flat is not None
    assert flat["category"] is None
    assert flat["intro_card_fields"] == []


def test_missing_social_context_does_not_crash(record):
    """Robustness: a record with profile_social_context stripped should
    still flatten cleanly with follower fields None."""
    stripped = copy.deepcopy(record)
    stripped["profile_social_context"] = {}

    flat = PARSER.flatten(stripped, "ProfileInfo")
    assert flat is not None
    assert flat["follower_count_text"] is None
    assert flat["followers_url"] is None
    assert flat["following_count_text"] is None


def test_intro_card_fields_is_list(record):
    flat = PARSER.flatten(record, "ProfileInfo")
    assert isinstance(flat["intro_card_fields"], list)
    assert len(flat["intro_card_fields"]) > 0


def test_returns_none_on_shape_mismatch():
    assert PARSER.flatten({}, "ProfileInfo") is None
    assert PARSER.flatten({"not_id": "x"}, "ProfileInfo") is None
