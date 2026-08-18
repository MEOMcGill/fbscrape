"""Flatten ProfileAuthenticity record against captured fixture (LamoureuxTwins)."""


import copy

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "profile_authenticity"

EXPECTED_KEYS = {
    "user_id", "name", "delegate_page_id",
    "profile_join_date", "profile_updated_since", "category",
    "transparency_present",
    "is_meta_verified", "meta_verified_headline", "meta_verified_body",
    "header_description", "about_title",
    "about_fields", "header_fields",
    "section_token", "collection_token",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_user_id_matches_target(record):
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    assert flat["user_id"] == "100044331674441"


def test_name_populated(record):
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    assert flat["name"], "LamoureuxTwins' profile should have a name field"


def test_header_field_dispatch(record):
    """At least one of the four documented `profile_field_type` strategies
    (PROFILE_JOIN_DATE, PROFILE_UPDATED_SINCE, CATEGORY, TRANSPARENCY) should
    populate — they're the load-bearing dispatched fields."""
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    dispatched_count = sum(
        1 for v in [
            flat["profile_join_date"],
            flat["profile_updated_since"],
            flat["category"],
        ] if v is not None
    )
    # transparency_present is a bool flag, not a value field — but should be True
    # since the FB UI always renders the transparency link on public profiles.
    assert dispatched_count >= 1 or flat["transparency_present"], (
        "no header_field strategy dispatched — check _flatten_profile_authenticity_record"
    )


def test_missing_header_field_does_not_crash(record):
    """Robustness: a record with header_fields[] stripped should still flatten
    cleanly with the dispatched keys all None."""
    stripped = copy.deepcopy(record)
    modal = stripped.get("profile_directory_authenticity_modal", {})
    modal["header_fields"] = []

    flat = PARSER.flatten(stripped, "ProfileAuthenticity")
    assert flat is not None
    assert flat["profile_join_date"] is None
    assert flat["profile_updated_since"] is None
    assert flat["category"] is None
    assert flat["transparency_present"] is False


def test_about_fields_and_header_fields_are_lists(record):
    flat = PARSER.flatten(record, "ProfileAuthenticity")
    assert isinstance(flat["about_fields"], list)
    assert isinstance(flat["header_fields"], list)


def test_returns_none_on_shape_mismatch():
    assert PARSER.flatten({}, "ProfileAuthenticity") is None
    assert PARSER.flatten({"not_id": "x"}, "ProfileAuthenticity") is None
