"""Essential tests for Search URL filter construction.

Covers the encoding contract (`_build_search_url` + `_SEARCH_FILTER_REGISTRY`)
and the CLI filter parser (`_parse_filter_options`). The round-trip test is the
golden lock: build → base64-decode → JSON-parse → assert blob structure. If FB's
filter encoding drifts, or a registry edit changes the wire format, it trips here
rather than silently returning default-ranked results from live FB.
"""

import base64
import json
from urllib.parse import urlparse, parse_qs

import pytest

from fbscrape.browser_session import BrowserSession


def _decode_filters(url: str) -> dict:
    """Pull the `filters=` param off a search URL and decode it to the
    outer dict of {outer_key: {"name", "args"}}."""
    qs = parse_qs(urlparse(url).query)
    blob = qs["filters"][0]
    outer = json.loads(base64.b64decode(blob).decode())
    return {k: json.loads(v) for k, v in outer.items()}


def test_no_filters_omits_filters_param():
    url = BrowserSession._build_search_url("carney", None)
    assert "filters=" not in url
    assert parse_qs(urlparse(url).query)["q"] == ["carney"]

    # Empty dict behaves the same as None.
    assert "filters=" not in BrowserSession._build_search_url("carney", {})


def test_round_trip_recent_posts_and_creation_time():
    url = BrowserSession._build_search_url(
        "carney",
        {
            "recent_posts": {},
            "creation_time": {"start": "2025-01-01", "end": "2025-12-31"},
        },
    )
    decoded = _decode_filters(url)

    assert decoded["recent_posts:0"] == {"name": "recent_posts", "args": ""}

    ct = decoded["rp_creation_time:0"]
    assert ct["name"] == "creation_time"
    # args is a JSON string of the (non-zero-padded) date components.
    args = json.loads(ct["args"])
    assert args == {
        "start_year": "2025", "start_month": "2025-1", "start_day": "2025-1-1",
        "end_year": "2025", "end_month": "2025-12", "end_day": "2025-12-31",
    }


def test_creation_time_one_sided_bound():
    url = BrowserSession._build_search_url("carney", {"creation_time": {"start": "2025-06-01"}})
    args = json.loads(_decode_filters(url)["rp_creation_time:0"]["args"])
    assert args == {"start_year": "2025", "start_month": "2025-6", "start_day": "2025-6-1"}


@pytest.mark.parametrize("source, expected_name", [
    ("public", "merged_public_posts"),
    ("me", "author_me"),
    ("friends", "author_friends_feed"),
    ("groups_and_pages", "my_groups_and_pages_posts"),
])
def test_posts_from_source_maps_to_inner_name(source, expected_name):
    url = BrowserSession._build_search_url("carney", {"posts_from": {"source": source}})
    entry = _decode_filters(url)["rp_author:0"]
    assert entry == {"name": expected_name, "args": ""}


def test_raw_passthrough_key_with_colon_is_verbatim():
    """A key containing ':' is the explicit raw-passthrough signal — written
    verbatim as the outer key + value."""
    url = BrowserSession._build_search_url(
        "carney",
        {"city:0": {"name": "city", "args": '{"city_id":"123"}'}},
    )
    assert _decode_filters(url)["city:0"] == {"name": "city", "args": '{"city_id":"123"}'}


# --- error paths ---

def test_unknown_bare_key_raises():
    """A bare unknown key (no ':') is treated as a typo'd known filter, not a
    raw entry — raise rather than silently produce results FB ignores."""
    with pytest.raises(ValueError, match="Unknown search filter 'recent_post'"):
        BrowserSession._build_search_url("carney", {"recent_post": {}})


def test_posts_from_invalid_source_raises():
    with pytest.raises(ValueError, match="posts_from source 'nope' is not valid"):
        BrowserSession._build_search_url("carney", {"posts_from": {"source": "nope"}})


# --- CLI parser ---

def test_cli_parse_dot_notation_and_no_arg():
    from fbscrape.cli import _parse_filter_options
    filters = _parse_filter_options(
        ("recent_posts", "creation_time.start=2025-01-01", "creation_time.end=2025-12-31"),
        (),
    )
    assert filters == {
        "recent_posts": {},
        "creation_time": {"start": "2025-01-01", "end": "2025-12-31"},
    }


def test_cli_parse_raw_filter():
    from fbscrape.cli import _parse_filter_options
    filters = _parse_filter_options((), (("city:0", "city", '{"city_id":"123"}'),))
    assert filters == {"city:0": {"name": "city", "args": '{"city_id":"123"}'}}


def test_cli_parse_no_filters_returns_none():
    from fbscrape.cli import _parse_filter_options
    assert _parse_filter_options((), ()) is None
