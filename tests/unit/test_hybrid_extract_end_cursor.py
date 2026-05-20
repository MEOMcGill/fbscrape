"""Unit tests for `BrowserSession._hybrid_extract_end_cursor`.

Regression coverage for the May 2026 GroupTimeline cursor-theft bug.

Background. FB's @stream/@defer pagination responses are JSONL streams of
patches. The page-level pagination cursor for the requested connection lives
in one chunk; nested attachments (Reels mini-feed, etc.) emit their own
`page_info.end_cursor` values in deeper chunks. The old extractor returned
the FIRST `end_cursor` it found in DFS order, which — when a post in the
batch had a Reels attachment — silently picked the Reels sub-stream cursor
instead of the group-feed cursor. Sending that back as `variables.cursor`
on the next replay triggered `field_exception` server-side and killed the
scrape. The new extractor selects the cursor from the chunk with the
shortest `path` — the page-level connection's `page_info` — making it
robust to any sub-stream cursors at deeper paths.
"""
from __future__ import annotations

import json

from fbscrape.browser_session import BrowserSession


def _make_grouptimeline_response_with_reels(
    page_level_cursor: str = "PAGE_LEVEL_528_CHAR_CURSOR_STUB",
    reels_cursor: str = "REELS_90_CHAR_CURSOR_STUB",
) -> str:
    """Build a minimal JSONL response that reproduces the cursor-theft shape.

    Mirrors the real iter-155 response from
    `tmp/hybrid/graphql_error/2235625136712757/20260519T230058Z/window.jsonl`:
    the initial chunk carries the first post + has no `end_cursor`; a Reels
    attachment deferred chunk carries a 90-char `end_cursor` at a 12-deep
    `path`; the group-feed `page_info` deferred chunk carries the real
    `end_cursor` at `['node','group_feed']` (path-length 2). FB's stream
    order places the Reels chunk BEFORE the page_info chunk, which is what
    made the first-match extractor pick the wrong one.
    """
    initial = {
        "data": {
            "node": {
                "__typename": "Group",
                "group_feed": {"edges": [{"node": {"post_id": "p0"}}]},
                "id": "g0",
            }
        },
        "extensions": {"is_final": False},
    }
    reels_chunk = {
        "label": "CometFeedStoryFBReelsAttachment_story$defer$FBReelsFeedbackBar",
        "path": [
            "node", "group_feed", "edges", 0, "node",
            "attachments", 0, "styles", "attachment", "style_infos", 0,
            "fb_shorts_story",
        ],
        "data": {"page_info": {"end_cursor": reels_cursor, "has_next_page": True}},
        "extensions": {"is_final": True},
    }
    page_info_chunk = {
        "label": (
            "GroupsCometFeedRegularStories_paginationGroup$defer$"
            "GroupsCometFeedRegularStories_group_group_feed$page_info"
        ),
        "path": ["node", "group_feed"],
        "data": {"page_info": {"end_cursor": page_level_cursor, "has_next_page": True}},
        "extensions": {"is_final": True},
    }
    return "\n".join(json.dumps(d) for d in [initial, reels_chunk, page_info_chunk])


def test_picks_page_level_over_reels_substream():
    """Regression: with a Reels attachment present, return the group-feed
    cursor, not the Reels mini-stream cursor."""
    body = _make_grouptimeline_response_with_reels(
        page_level_cursor="PAGE_LEVEL",
        reels_cursor="REELS",
    )
    assert BrowserSession._hybrid_extract_end_cursor(body) == "PAGE_LEVEL"


def test_picks_page_level_when_stream_order_puts_reels_first():
    """Independent of stream order: even if FB ships the Reels chunk before
    the page_info chunk (which is what happens in real responses), we still
    pick the shortest-path cursor."""
    initial = {"data": {"node": {"group_feed": {"edges": []}}}}
    reels_chunk = {
        "label": "CometFeedStoryFBReelsAttachment_story$defer$...",
        "path": ["node", "group_feed", "edges", 0, "node", "attachments", 0,
                 "styles", "attachment", "style_infos", 0, "fb_shorts_story"],
        "data": {"page_info": {"end_cursor": "REELS"}},
    }
    page_info_chunk = {
        "label": "GroupsCometFeedRegularStories_paginationGroup$defer$page_info",
        "path": ["node", "group_feed"],
        "data": {"page_info": {"end_cursor": "PAGE_LEVEL"}},
    }
    # Reels chunk shipped FIRST (the failure-mode stream order from real dumps).
    body = "\n".join(json.dumps(d) for d in [initial, reels_chunk, page_info_chunk])
    assert BrowserSession._hybrid_extract_end_cursor(body) == "PAGE_LEVEL"
    # Reels chunk shipped LAST. Still picks PAGE_LEVEL.
    body = "\n".join(json.dumps(d) for d in [initial, page_info_chunk, reels_chunk])
    assert BrowserSession._hybrid_extract_end_cursor(body) == "PAGE_LEVEL"


def test_initial_non_deferred_chunk_wins_over_any_deferred():
    """An `end_cursor` in the initial chunk (no `path`, treated as length 0)
    wins over any deferred chunk's cursor."""
    initial = {
        "data": {
            "node": {
                "group_feed": {
                    "edges": [{"node": {"post_id": "p0"}}],
                    "page_info": {"end_cursor": "INITIAL_CURSOR"},
                }
            }
        }
    }
    deferred = {
        "label": "anything$defer$something",
        "path": ["node", "group_feed"],
        "data": {"page_info": {"end_cursor": "DEFERRED_CURSOR"}},
    }
    body = "\n".join(json.dumps(d) for d in [initial, deferred])
    assert BrowserSession._hybrid_extract_end_cursor(body) == "INITIAL_CURSOR"


def test_end_of_feed_null_cursor_at_shortest_path_returns_none():
    """When the page-level cursor is null/missing (genuine end-of-feed), we
    return None even if a deeper sub-stream still has a cursor. EndOfFeed
    stop condition relies on this signal to terminate the loop."""
    initial = {"data": {"node": {"group_feed": {"edges": []}}}}
    reels_chunk = {
        "label": "CometFeedStoryFBReelsAttachment_story$defer$...",
        "path": ["node", "group_feed", "edges", 0, "node", "attachments", 0,
                 "styles", "attachment", "style_infos", 0, "fb_shorts_story"],
        "data": {"page_info": {"end_cursor": "STILL_HAS_A_REELS_CURSOR"}},
    }
    page_info_chunk = {
        "label": "GroupsCometFeedRegularStories_paginationGroup$defer$page_info",
        "path": ["node", "group_feed"],
        "data": {"page_info": {"end_cursor": None, "has_next_page": False}},
    }
    body = "\n".join(json.dumps(d) for d in [initial, reels_chunk, page_info_chunk])
    assert BrowserSession._hybrid_extract_end_cursor(body) is None


def test_no_cursors_anywhere_returns_none():
    body = json.dumps({"data": {"node": {"group_feed": {"edges": []}}}})
    assert BrowserSession._hybrid_extract_end_cursor(body) is None


def test_empty_or_malformed_returns_none():
    assert BrowserSession._hybrid_extract_end_cursor("") is None
    assert BrowserSession._hybrid_extract_end_cursor("not json at all") is None


def test_single_json_object_still_works():
    """Backwards-compat: a single non-JSONL JSON object with an end_cursor
    in `page_info` returns that cursor (path-length 0 — initial-style chunk)."""
    body = json.dumps({
        "data": {"node": {"timeline_list_feed_units": {
            "edges": [],
            "page_info": {"end_cursor": "ONLY_CURSOR"},
        }}}
    })
    assert BrowserSession._hybrid_extract_end_cursor(body) == "ONLY_CURSOR"


def test_endcursor_camelcase_alias():
    """The extractor accepts both `end_cursor` (FB's wire format) and
    `endCursor` (the Relay/JS camelCase alias) — preserves existing API."""
    body = json.dumps({"data": {"page_info": {"endCursor": "CAMEL_CASE"}}})
    assert BrowserSession._hybrid_extract_end_cursor(body) == "CAMEL_CASE"


def test_aliased_reels_paths_both_deeper_than_page_level():
    """Real responses sometimes deliver the SAME Reels attachment via two
    aliased paths (a different deferred fragment uses a different parent
    route to the same node). Both are deeper than the page-level chunk, so
    neither wins. Regression: don't accidentally tiebreak in a way that
    surfaces a deeper alias."""
    initial = {"data": {"node": {"group_feed": {"edges": []}}}}
    reels_a = {
        "label": "CometFeedStoryFBReelsAttachment_story$defer$...",
        "path": ["node", "group_feed", "edges", 0, "node", "attachments", 0,
                 "styles", "attachment", "style_infos", 0, "fb_shorts_story"],
        "data": {"page_info": {"end_cursor": "REELS"}},
    }
    reels_b = {
        "label": "CometFeedStoryFBReelsAttachment_story$defer$...",
        "path": ["node", "group_feed", "edges", 0, "node",
                 "comet_sections", "content", "story",
                 "attachments", 0, "styles", "attachment", "style_infos", 0,
                 "fb_shorts_story"],
        "data": {"page_info": {"end_cursor": "REELS"}},
    }
    page_info = {
        "label": "GroupsCometFeedRegularStories_paginationGroup$defer$page_info",
        "path": ["node", "group_feed"],
        "data": {"page_info": {"end_cursor": "PAGE_LEVEL"}},
    }
    body = "\n".join(json.dumps(d) for d in [initial, reels_a, reels_b, page_info])
    assert BrowserSession._hybrid_extract_end_cursor(body) == "PAGE_LEVEL"
