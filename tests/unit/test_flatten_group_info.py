"""Flatten GroupInfo record against captured fixture (a public group)."""


import copy

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "group_info"

EXPECTED_KEYS = {
    "group_id", "name", "url", "handle",
    "privacy_label", "privacy_description",
    "member_count", "viewer_join_state",
    "cover_photo_url", "content_views",
}


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    flat = PARSER.flatten(record, "GroupInfo")
    assert flat is not None


def test_full_key_set(record):
    flat = PARSER.flatten(record, "GroupInfo")
    missing = EXPECTED_KEYS - flat.keys()
    assert not missing, f"missing keys: {missing}"


def test_group_id_matches_target(record):
    flat = PARSER.flatten(record, "GroupInfo")
    assert flat["group_id"] == "787909081545196"


def test_name_populated(record):
    flat = PARSER.flatten(record, "GroupInfo")
    assert flat["name"], "group should have a name field"


def test_member_count_populated(record):
    """Member count only ships as an FB-formatted abbreviated string
    (e.g. '120.4K members') — parsed into an approximate int for
    sorting/comparison, never an exact integer on this surface."""
    flat = PARSER.flatten(record, "GroupInfo")
    assert flat["member_count"] and flat["member_count"] > 0


def test_privacy_label_populated(record):
    """The header's `privacy_info` only carries a single display string
    (e.g. "Public group") — GroupAbout's About page carries a richer
    split label/description (see test_flatten_group_about)."""
    flat = PARSER.flatten(record, "GroupInfo")
    assert flat["privacy_label"]
    assert flat["privacy_description"] is None


def test_content_views_is_directory(record):
    flat = PARSER.flatten(record, "GroupInfo")
    assert isinstance(flat["content_views"], dict)
    assert "ABOUT" in flat["content_views"]


def test_missing_privacy_info_does_not_crash(record):
    """Robustness: a record with privacy_info stripped should still
    flatten cleanly with the dispatched keys None."""
    stripped = copy.deepcopy(record)
    stripped["privacy_info"] = {}

    flat = PARSER.flatten(stripped, "GroupInfo")
    assert flat is not None
    assert flat["privacy_label"] is None


def test_missing_content_views_does_not_crash(record):
    """Robustness: a record with group_content_views stripped should
    still flatten cleanly with an empty directory."""
    stripped = copy.deepcopy(record)
    stripped["group_content_views"] = {}

    flat = PARSER.flatten(stripped, "GroupInfo")
    assert flat is not None
    assert flat["content_views"] == {}


def test_returns_none_on_shape_mismatch():
    assert PARSER.flatten({}, "GroupInfo") is None
    assert PARSER.flatten({"not_id": "x"}, "GroupInfo") is None
