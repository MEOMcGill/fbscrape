"""Flatten PageTransparency record against captured fixture (Meta page)."""


import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "page_transparency"

EXPECTED_KEYS = {
    "page_id", "name", "page_type_name_for_content", "is_viewer_admin",
    "verification_status", "profile_picture_url",
    "page_transparency_settings_uri", "should_show_responsible_for_org_content",
    "category_text", "delegate_id",
    "transparency_id", "transparency_title", "is_person_profile",
    "is_profile_action_report", "linked_profile_id", "should_use_page_rename",
    "genai_chatbot_disclosure", "enabled_features",
    "has_active_ads", "has_run_political_ads", "page_id_for_admin",
    "state_media_country_label", "confirmed_page_owner_consumer",
    "confirmed_page_partner_names", "creation_event_time",
    "name_changes", "admin_country_counts",
    "admin_num_opt_out", "admin_num_unknown",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "PageTransparency")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "PageTransparency")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_page_id_matches_target(record):
    """Capture script asks for page_id=20531316728 (Meta)."""
    flat = PARSER.flatten(record, "PageTransparency")
    assert flat["page_id"] == "20531316728"


def test_name_populated(record):
    flat = PARSER.flatten(record, "PageTransparency")
    assert flat["name"], "Meta page should have a name field"


def test_list_fields_are_lists(record):
    flat = PARSER.flatten(record, "PageTransparency")
    for k in ("enabled_features", "confirmed_page_partner_names",
              "name_changes", "admin_country_counts"):
        assert isinstance(flat[k], list), f"{k} should always be a list (possibly empty)"


def test_returns_none_on_shape_mismatch():
    """Records without an `id` field are unrecognized — flatten returns None."""
    assert PARSER.flatten({}, "PageTransparency") is None
    assert PARSER.flatten({"not_id": "x"}, "PageTransparency") is None
