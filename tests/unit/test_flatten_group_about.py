"""Flatten GroupAbout record against captured fixture (a public group)."""


import copy

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "group_about"

# Superset of GroupInfo's EXPECTED_KEYS (reused via _flatten_group_info_record)
# plus the About-specific fields.
EXPECTED_KEYS = {
    "group_id", "name", "url", "handle",
    "privacy_label", "privacy_description",
    "member_count", "viewer_join_state",
    "cover_photo_url", "content_views",
    "description", "discoverability_label", "discoverability_description",
    "created_time", "history_summary", "locations", "about_info_items",
    "posts_last_day", "posts_last_month", "total_members_text",
    "new_members_text", "admin_and_moderator_count", "rules",
    "friend_member_count", "admin_profiles",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "GroupAbout")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_includes_group_info_fields(record):
    """GroupAbout reuses _flatten_group_info_record for the header — a
    GroupAbout row should carry the same identity fields GroupInfo
    returns, since the About page renders the header for free."""
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["group_id"]
    assert flat["name"]
    assert flat["member_count"] and flat["member_count"] > 0


def test_privacy_dispatch_overrides_header(record):
    """The About page's XFBPrivacyGroupsAboutInfoItem is richer than the
    header's single "Public group" string — a split label ("Public") +
    description — and should be promoted over the header value."""
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["privacy_label"] == "Public"
    assert flat["privacy_description"]


def test_description_populated(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["description"]


def test_discoverability_dispatched(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["discoverability_label"]
    assert flat["discoverability_description"]


def test_history_dispatched(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["created_time"] == 1335200684
    assert flat["history_summary"]


def test_location_dispatched(record):
    # The current fixture group ("Children of da KoRn") has no location set, so
    # this only verifies the flattener returns an empty list (not None/error).
    # TODO: swap in a group with a location to exercise location dispatch.
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["locations"] == []


def test_about_info_items_is_nonempty_list(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert isinstance(flat["about_info_items"], list)
    assert len(flat["about_info_items"]) > 0
    for item in flat["about_info_items"]:
        assert "type" in item
        assert "group" in item


def test_activity_card_populated(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert isinstance(flat["posts_last_day"], int)
    assert isinstance(flat["posts_last_month"], int)
    assert flat["total_members_text"]
    assert flat["new_members_text"]


def test_rules_dispatched(record):
    flat = PARSER.flatten(record, "GroupAbout")
    assert flat["admin_and_moderator_count"] and flat["admin_and_moderator_count"] > 0
    assert isinstance(flat["rules"], list)
    assert len(flat["rules"]) > 0
    for r in flat["rules"]:
        assert "id" in r
        assert "title" in r
        assert "description" in r


def test_admin_profiles_dispatched(record):
    """Best-effort: this facepile may be shorter than
    admin_and_moderator_count for groups with many admins/moderators — see
    _flatten_group_about_record's docstring."""
    flat = PARSER.flatten(record, "GroupAbout")
    assert isinstance(flat["admin_profiles"], list)
    assert len(flat["admin_profiles"]) > 0
    assert len(flat["admin_profiles"]) <= flat["admin_and_moderator_count"]
    for a in flat["admin_profiles"]:
        assert a["id"]
        assert a["name"]
        assert a["url"]


def test_missing_cards_does_not_crash(record):
    """Robustness: a record with cards stripped (e.g. shape drift) should
    still flatten cleanly with the About-specific keys empty/None, but
    header fields intact."""
    stripped = copy.deepcopy(record)
    stripped["cards"] = []

    flat = PARSER.flatten(stripped, "GroupAbout")
    assert flat is not None
    assert flat["name"]
    assert flat["description"] is None
    assert flat["rules"] == []
    assert flat["admin_profiles"] == []


def test_missing_group_returns_none():
    """No group node at all (e.g. header extraction itself failed) means
    there's nothing to build a row around."""
    assert PARSER.flatten({"group": None, "cards": []}, "GroupAbout") is None


def test_returns_none_on_shape_mismatch():
    assert PARSER.flatten({}, "GroupAbout") is None
    assert PARSER.flatten({"cards": []}, "GroupAbout") is None
