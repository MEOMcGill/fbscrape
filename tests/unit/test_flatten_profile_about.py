"""Flatten ProfileAbout record against captured fixture (a public Page)."""


import copy

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "profile_about"

# Superset of ProfileInfo's EXPECTED_KEYS (reused via _flatten_profile_info_record)
# plus the About-specific fields.
EXPECTED_KEYS = {
    "profile_id", "name", "url", "gender", "username_for_profile",
    "is_verified", "is_viewer_friend", "is_memorialized",
    "follower_count", "followers_url", "following_count",
    "friend_count", "friends_url", "reaction_count", "bio",
    "category", "intro_card_fields",
    "cover_photo_url", "profile_picture_url",
    "phone", "email", "messenger_url",
    "address", "address_map_url", "hours", "rating_text",
    "website", "website_url", "current_city", "hometown", "relationship", "about_fields",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_includes_profile_info_fields(record):
    """ProfileAbout reuses _flatten_profile_info_record for the header —
    a ProfileAbout row should carry the same identity fields ProfileInfo
    returns, since the About landing page renders the header for free."""
    flat = PARSER.flatten(record, "ProfileAbout")
    assert flat["profile_id"]
    assert flat["name"]
    assert flat["follower_count"] == 22_000
    assert flat["following_count"] == 1


def test_contact_info_dispatched(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    assert flat["phone"]
    assert flat["email"]
    assert flat["messenger_url"]


def test_basic_info_dispatched(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    assert flat["address"]
    assert flat["hours"]
    assert flat["rating_text"]


def test_links_dispatched(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    assert flat["website"]
    assert flat["website_url"]


def test_about_fields_is_nonempty_list(record):
    flat = PARSER.flatten(record, "ProfileAbout")
    assert isinstance(flat["about_fields"], list)
    assert len(flat["about_fields"]) > 0
    for f in flat["about_fields"]:
        assert "field_section_type" in f
        assert "field_type" in f
        assert "text" in f
        assert "detail" in f
        assert "link_url" in f


def test_missing_sections_does_not_crash(record):
    """Robustness: a record with sections stripped (e.g. a personal profile
    with none of the requested sub-tabs present) should still flatten
    cleanly with the About-specific keys all None, but header fields intact."""
    stripped = copy.deepcopy(record)
    stripped["sections"] = []

    flat = PARSER.flatten(stripped, "ProfileAbout")
    assert flat is not None
    assert flat["name"]
    assert flat["phone"] is None
    assert flat["email"] is None
    assert flat["about_fields"] == []


def _field_section(section_type, fields):
    return {
        "field_section_type": section_type,
        "profile_fields": {"nodes": [
            {"field_type": ft, "title": {"text": text}, "link_url": None}
            for ft, text in fields
        ]},
    }


def test_places_dispatched_preferring_places_subtab(record):
    """Personal profiles: the landing overview says "Lives in X" / "From Y";
    the about_places sub-tab (appended later) has the bare place name, which
    should win."""
    injected = copy.deepcopy(record)
    injected["sections"] = [
        _field_section("overview", [
            ("current_city", "Lives in Winder, Georgia"),
            ("hometown", "From Franklin, North Carolina"),
        ]),
        _field_section("places_lived", [
            ("current_city", "Winder, Georgia"),
            ("hometown", "Franklin, North Carolina"),
        ]),
    ]

    flat = PARSER.flatten(injected, "ProfileAbout")
    assert flat["current_city"] == "Winder, Georgia"
    assert flat["hometown"] == "Franklin, North Carolina"


def test_relationship_value_from_text_content(record):
    """Relationship's title is just the label; the value is in text_content."""
    injected = copy.deepcopy(record)
    injected["sections"] = [{
        "field_section_type": "relationship",
        "profile_fields": {"nodes": [{
            "field_type": "relationship",
            "title": {"text": "Relationship"},
            "renderer": {"field": {"text_content": {"text": "Single"}}},
            "link_url": None,
        }]},
    }]

    flat = PARSER.flatten(injected, "ProfileAbout")
    assert flat["relationship"] == "Single"
    assert flat["about_fields"][0]["detail"] == "Single"


def test_category_falls_back_to_about_field(record):
    """Directory-style About layouts list the category only as an About field."""
    injected = copy.deepcopy(record)
    header_top_row = injected["profile"].get("header_top_row") or {}
    (header_top_row.get("profile_user") or {})["profile_intro_card"] = {}
    injected["sections"] = [_field_section("directory_category", [("category", "Digital creator")])]

    flat = PARSER.flatten(injected, "ProfileAbout")
    assert flat["category"] == "Digital creator"


def test_places_from_overview_only(record):
    injected = copy.deepcopy(record)
    injected["sections"] = [
        _field_section("overview", [("current_city", "Lives in Winder, Georgia")]),
    ]

    flat = PARSER.flatten(injected, "ProfileAbout")
    assert flat["current_city"] == "Lives in Winder, Georgia"
    assert flat["hometown"] is None


def test_missing_profile_returns_none():
    """No profile node at all (e.g. header extraction itself failed) means
    there's nothing to build a row around."""
    assert PARSER.flatten({"profile": None, "sections": []}, "ProfileAbout") is None


def test_returns_none_on_shape_mismatch():
    assert PARSER.flatten({}, "ProfileAbout") is None
    assert PARSER.flatten({"sections": []}, "ProfileAbout") is None
