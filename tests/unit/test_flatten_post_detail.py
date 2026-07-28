"""Flatten a PostDetail record against a captured fixture (a group-post
permalink). PostDetail Stories are the same Comet shape as GroupTimeline /
UserTimeline posts, so they route through `_flatten_pctfrq_post` and carry the
same schema — this test pins that a permalink-extracted Story flattens cleanly."""


import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()
FIXTURE_NAME = "post_detail"

# Capture target (see tests/_capture_fixtures.py): a public Alberta-politics
# group post cited by Google's AI Overview.
EXPECTED_POST_ID = "27209929835285847"


@pytest.fixture(scope="module")
def record():
    data = load_fixture_or_skip(FIXTURE_NAME)
    recs = data.get("data") or []
    if not recs:
        pytest.skip(f"{FIXTURE_NAME} fixture has no records — recapture")
    return recs[0]


def test_record_flattens(record):
    assert PARSER.flatten(record, "PostDetail") is not None


def test_post_id_matches_target(record):
    flat = PARSER.flatten(record, "PostDetail")
    assert flat["post_id"] == EXPECTED_POST_ID


def test_core_fields_populated(record):
    flat = PARSER.flatten(record, "PostDetail")
    assert flat["author_name"], "post should carry an author name"
    assert flat["created_at_utc"], "post should carry a creation timestamp"
    assert flat["text"], "this captured post has body text"
    assert flat["permalink_url"] and EXPECTED_POST_ID in flat["permalink_url"]


def test_schema_matches_group_timeline(record):
    """PostDetail reuses the timeline flattener, so its row schema must match a
    GroupTimeline post's — guards against the two silently diverging."""
    gt = load_fixture_or_skip("group_timeline")
    gt_recs = gt.get("data") or []
    if not gt_recs:
        pytest.skip("group_timeline fixture has no records — recapture")
    pd_keys = set(PARSER.flatten(record, "PostDetail").keys())
    gt_keys = set(PARSER.flatten(gt_recs[0], "GroupTimeline").keys())
    assert pd_keys == gt_keys, f"schema drift: {pd_keys ^ gt_keys}"


def test_extract_permalink_story_from_html():
    """The document-extraction path: a minimal HTML shell embedding the Story
    in a data-sjs script must yield the same record the fixture holds."""
    import json
    data = load_fixture_or_skip(FIXTURE_NAME)
    story = data["data"][0]["node"]
    payload = {"require": [["ScheduledServerJS", "handle", [{"__bbox": {
        "result": {"data": {"node_v2": story}}}}]]]}
    html = (
        '<html><body>'
        '<script type="application/json" data-sjs>' + json.dumps(payload) + '</script>'
        '</body></html>'
    )
    entry = PARSER.extract_permalink_story(html, EXPECTED_POST_ID)
    assert entry is not None
    flat = PARSER.flatten(entry, "PostDetail")
    assert flat["post_id"] == EXPECTED_POST_ID


def test_extract_permalink_story_missing_returns_none():
    entry = PARSER.extract_permalink_story("<html><body>no json</body></html>", "123")
    assert entry is None
