"""Query / ENDPOINT_REGISTRY contract tests.

These tests pin the user-facing surface of `Query`: which (endpoint, mode)
pairs exist, which fields/params they require, and how validation behaves.
A change to the registry without updating these tests means a silent break
in the public API.
"""

from __future__ import annotations

import json
import pytest

from fbscrape.models import Query


# (endpoint, mode, sample_query) covering every supported endpoint × mode.
ALL_PAIRS = [
    ("UserTimeline", "manual",  {"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"}),
    ("UserTimeline", "hybrid",  {"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"}),
    ("Search",       "hybrid",  {"query_text": "x", "start_date": "2024-01-01", "end_date": "2024-02-01"}),
    ("PageTransparency",    "hybrid", {"page_id": "20531316728"}),
    ("ProfileAuthenticity", "hybrid", {"user_id": "100044331674441"}),
]


@pytest.mark.parametrize("endpoint, mode, sample_query", ALL_PAIRS)
def test_valid_construction_fills_defaults(endpoint, mode, sample_query):
    """For each registered (endpoint, mode), constructing with the required
    query fields and empty params should succeed and populate all default
    param values from the registry."""
    q = Query(endpoint=endpoint, mode=mode, query=dict(sample_query), params={})

    expected = Query.ENDPOINT_REGISTRY[endpoint]["modes"][mode]["params"]
    for k, default in expected.items():
        assert k in q.params, f"default param {k!r} not filled for ({endpoint}, {mode})"
        assert q.params[k] == default


def test_unknown_endpoint_raises():
    with pytest.raises(ValueError, match="Unsupported endpoint"):
        Query(endpoint="DoesNotExist", mode="hybrid", query={}, params={})


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="Unsupported mode"):
        Query(
            endpoint="UserTimeline", mode="api",
            query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
            params={},
        )


def test_search_has_no_manual_mode():
    """Search is hybrid-only — manual is intentionally absent."""
    with pytest.raises(ValueError, match="Unsupported mode"):
        Query(
            endpoint="Search", mode="manual",
            query={"query_text": "x", "start_date": "2024-01-01", "end_date": "2024-02-01"},
            params={},
        )


@pytest.mark.parametrize("endpoint, mode, full_query", ALL_PAIRS)
def test_missing_required_query_field_raises(endpoint, mode, full_query):
    """For every required field of every endpoint, dropping it must raise."""
    required = Query.ENDPOINT_REGISTRY[endpoint]["query_required"]
    for missing in required:
        partial = {k: v for k, v in full_query.items() if k != missing}
        with pytest.raises(ValueError, match="missing required fields"):
            Query(endpoint=endpoint, mode=mode, query=partial, params={})


def test_unknown_param_raises():
    with pytest.raises(ValueError, match="Unknown params"):
        Query(
            endpoint="UserTimeline", mode="hybrid",
            query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
            params={"this_is_not_a_real_param": 1},
        )


def test_user_supplied_param_overrides_default():
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={"pagination_count": 7},
    )
    assert q.params["pagination_count"] == 7
    # Other defaults still filled.
    assert q.params["max_no_progress_streak"] == 5


def test_date_ordering_enforced():
    with pytest.raises(ValueError, match="must be on or before"):
        Query(
            endpoint="UserTimeline", mode="hybrid",
            query={"handle": "zuck", "start_date": "2024-06-01", "end_date": "2024-01-01"},
            params={},
        )


def test_start_date_in_future_raises():
    with pytest.raises(ValueError, match="must be on or before today"):
        Query(
            endpoint="UserTimeline", mode="hybrid",
            query={"handle": "zuck", "start_date": "2999-01-01", "end_date": "2999-02-01"},
            params={},
        )


def test_end_date_in_future_clamped_to_today():
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2999-01-01"},
        params={},
    )
    # Clamped to today, not the original future date.
    assert q.query["end_date"] != "2999-01-01"


def test_malformed_date_raises():
    with pytest.raises(ValueError, match="must match"):
        Query(
            endpoint="UserTimeline", mode="hybrid",
            query={"handle": "zuck", "start_date": "01-01-2024", "end_date": "2024-02-01"},
            params={},
        )


def test_to_dict_and_to_json_roundtrip():
    q = Query(
        endpoint="UserTimeline", mode="hybrid",
        query={"handle": "zuck", "start_date": "2024-01-01", "end_date": "2024-02-01"},
        params={},
    )
    d = q.to_dict()
    assert d["endpoint"] == "UserTimeline"
    assert d["mode"] == "hybrid"
    assert d["query"]["handle"] == "zuck"
    assert d["params"]["pagination_count"] == 3

    # to_json is a string; re-parsing yields the same dict (tuple defaults
    # would not survive JSON, so we only check the top-level shape).
    parsed = json.loads(q.to_json())
    assert parsed["endpoint"] == "UserTimeline"
    assert parsed["query"] == d["query"]


def test_endpoint_registry_top_level_keys_pinned():
    """Lock the set of supported endpoints — adding one is a user-visible
    change, so the test should explicitly fail and force an update here."""
    assert set(Query.ENDPOINT_REGISTRY.keys()) == {
        "UserTimeline",
        "Search",
        "GroupTimeline",
        "PageTransparency",
        "ProfileAuthenticity",
    }
