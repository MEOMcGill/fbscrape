"""PostMediaSet: media-viewer response parsing, the set walk, record shape, file naming
and flattening — without a browser.

The viewer's CometPhotoRootContentQuery body is JSONL: line 0 carries
`data.currMedia` (the item), later @defer chunks carry `data.nextMediaAfterNodeId`
/ `data.prevMediaBeforeNodeId` (neighbours in the *requested* set). The synthetic
bodies below copy that shape (as captured on 2026-11-09), including the decoy
`currMedia.default_mediaset.nextMedia` (the page's general album) the parser must
ignore. The set wraps: the last item's next is the first.
"""
import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from fbscrape.browser_session import BrowserSession
from fbscrape.downloaders import extract_media_from_post, media_filename
from fbscrape.response import FacebookGraphQLParser, ResponseInterceptor

PARSER = FacebookGraphQLParser()
POST_ID = "2049017276045005"
TOKEN = f"pcb.{POST_ID}"


def _viewer_body(media_id: str, next_id: str | None, prev_id: str | None, typename: str = "Photo") -> str:
    curr = {
        "__typename": typename,
        "id": media_id,
        "owner": {"__typename": "User", "id": "100000000000001"},
        "accessibility_caption": f"caption {media_id}",
        "created_time": 1759000000,
        "default_mediaset": {  # page album: must NOT drive the walk
            "__typename": "Album",
            "nextMedia": {"edges": [{"node": {"id": "999999"}}]},
            "prevMedia": {"edges": [{"node": {"id": "888888"}}]},
        },
    }
    if typename == "Photo":
        curr["image"] = {"uri": f"https://scontent.example/v/{media_id}_n.jpg?x=1", "width": 2048, "height": 1536}
    lines = [
        {"data": {"currMedia": curr, "mediaset": {"__typename": "MediaContainerMediaSet", "id": "bWVkaWFzZXQ="}}},
        {"label": "CometNoCursorMediaSetViewerRenderer_mediaset$defer$CometNoCursorMediaSetViewerPrevMediaFetcher_mediaset",
         "path": ["mediaset"], "data": {"prevMediaBeforeNodeId": {"__typename": "Photo", "id": prev_id} if prev_id else None}},
        {"label": "CometNoCursorMediaSetViewerRenderer_mediaset$defer$CometNoCursorMediaSetViewerNextMediaFetcher_mediaset",
         "path": ["mediaset"], "data": {"nextMediaAfterNodeId": {"__typename": "Photo", "id": next_id} if next_id else None}},
    ]
    return "\n".join(json.dumps(x) for x in lines)


def _album(ids: list[str], types: dict | None = None) -> dict[str, str]:
    """nodeID -> body for a wrapping set."""
    types = types or {}
    n = len(ids)
    return {
        mid: _viewer_body(mid, ids[(i + 1) % n], ids[(i - 1) % n], types.get(mid, "Photo"))
        for i, mid in enumerate(ids)
    }


def _session(bodies: dict[str, str]) -> tuple[BrowserSession, list[str]]:
    s = BrowserSession(account=MagicMock(identifier="acct"), pool=MagicMock())
    s.response_interceptor = ResponseInterceptor()
    s.response_interceptor.extract_posts = False
    s.endpoint = "PostMediaSet"
    s.record_scroll = AsyncMock()
    requested: list[str] = []

    async def fake_replay(handle, body, template_headers, request_timeout_ms, operation_timeout_seconds):
        variables = json.loads(urllib.parse.parse_qs(body)["variables"][0])
        assert variables["mediasetToken"] == TOKEN
        requested.append(variables["nodeID"])
        return SimpleNamespace(status=200), bodies[variables["nodeID"]], None

    s._hybrid_send_replay = fake_replay
    return s, requested


TEMPLATE = {
    "form": {"doc_id": "1", "variables": json.dumps({"isMediaset": True, "nodeID": "0", "mediasetToken": TOKEN})},
    "headers": {},
}
IDS = [f"20490169{i:08d}" for i in range(1, 9)]   # an 8-photo album, first 5 already collected


async def _walk(s, **kw):
    args = dict(label=POST_ID, post_id=POST_ID, mediaset_token=TOKEN, template=TEMPLATE,
                skip_media_ids=None, expected_count=-1, max_media=-1, media_index_offset=0,
                pagination_sleep_mean=0.0, pagination_sleep_std=0.0,
                request_timeout_ms=1000, operation_timeout_seconds=10)
    args.update(kw)
    return await s._hybrid_media_set_loop(**args)


def test_parse_reads_requested_set_not_page_album():
    parsed = PARSER.parse_media_viewer_response(_viewer_body("2", "3", "1").encode())
    assert parsed["media"]["id"] == "2"
    assert parsed["next_media_id"] == "3"   # not default_mediaset's 999999
    assert parsed["prev_media_id"] == "1"


def test_parse_end_of_set_and_missing_media():
    assert PARSER.parse_media_viewer_response(_viewer_body("9", None, "8").encode())["next_media_id"] is None
    parsed = PARSER.parse_media_viewer_response(json.dumps({"data": {}}).encode())
    assert parsed["media"] is None


async def test_continue_past_cap_skips_known_and_stops_on_wrap():
    s, requested = _session(_album(IDS))
    result = await _walk(s, start_media_id=IDS[4], skip_media_ids=IDS[:5], media_index_offset=5)
    assert result == "success"
    recs = s.response_interceptor.get_posts()
    assert [r["node"]["media_set_item"]["media_id"] for r in recs] == IDS[5:]
    assert [r["node"]["media_set_item"]["file_index"] for r in recs] == [5, 6, 7]
    assert requested == IDS[4:]            # the start item, then 6..8; never re-fetches 1..5


async def test_full_walk_from_first_stops_at_wrap():
    s, requested = _session(_album(IDS))
    assert await _walk(s, start_media_id=IDS[0]) == "success"
    recs = s.response_interceptor.get_posts()
    assert [r["node"]["media_set_item"]["media_id"] for r in recs] == IDS
    assert [r["node"]["media_set_item"]["position"] for r in recs] == list(range(8))
    assert requested == IDS


async def test_expected_count_and_max_media():
    s, requested = _session(_album(IDS))
    assert await _walk(s, start_media_id=IDS[0], expected_count=3) == "success"
    assert requested == IDS[:3]
    s, _ = _session(_album(IDS))
    assert await _walk(s, start_media_id=IDS[0], max_media=2) == "max_media_reached"
    assert len(s.response_interceptor.get_posts()) == 2


async def test_video_item_gets_no_file_index():
    s, _ = _session(_album(IDS, types={IDS[6]: "Video"}))
    await _walk(s, start_media_id=IDS[4], skip_media_ids=IDS[:5], media_index_offset=5)
    recs = s.response_interceptor.get_posts()
    assert [r["node"]["media_set_item"]["file_index"] for r in recs] == [5, None, 6]


async def test_missing_curr_media_is_shape_error():
    bodies = _album(IDS)
    bodies[IDS[1]] = json.dumps({"data": {}})
    s, _ = _session(bodies)
    assert await _walk(s, start_media_id=IDS[0]) == "response_shape_error"


def test_record_media_continues_post_numbering():
    media = PARSER.parse_media_viewer_response(_viewer_body("777", "778", "776").encode())["media"]
    rec = PARSER.build_media_set_record(POST_ID, TOKEN, media, position=5, file_index=5)
    entries = extract_media_from_post(rec)
    assert len(entries) == 1
    assert entries[0]["post_id"] == POST_ID
    assert media_filename(entries[0]) == f"{POST_ID}_img_05.jpg"
    assert rec["post_id"] == "777"         # dedup key = the item, not the parent post


def test_video_record_produces_no_file():
    media = PARSER.parse_media_viewer_response(_viewer_body("778", None, "777", "Video").encode())["media"]
    rec = PARSER.build_media_set_record(POST_ID, TOKEN, media, position=6, file_index=None)
    assert extract_media_from_post(rec) == []


def test_flatten_row():
    media = PARSER.parse_media_viewer_response(_viewer_body("777", "778", "776").encode())["media"]
    flat = PARSER.flatten(PARSER.build_media_set_record(POST_ID, TOKEN, media, 5, 5), "PostMediaSet")
    assert flat["post_id"] == POST_ID and flat["media_id"] == "777"
    assert flat["media_type"] == "photo" and flat["file_index"] == 5 and flat["position"] == 5
    assert flat["mediaset_token"] == TOKEN
    assert flat["image_url"].startswith("https://") and flat["width"] == 2048
    assert flat["created_at_utc"].endswith("UTC")


def test_story_without_offset_numbers_from_zero():
    """media_index_offset is opt-in: ordinary Stories keep idx 0.."""
    media = PARSER.parse_media_viewer_response(_viewer_body("777", None, None).encode())["media"]
    rec = PARSER.build_media_set_record(POST_ID, TOKEN, media, 0, 0)
    rec["node"].pop("media_index_offset")
    assert extract_media_from_post(rec)[0]["idx"] == 0


# ---- captured fixture (tests/_capture_fixtures.py --only post_media_set) ----

from tests.conftest import load_fixture_or_skip  # noqa: E402

FIXTURE_FIRST_FIVE = [  # the Story's 5 album subattachments, in order
    "2049016942711705", "2049017049378361", "2049017056045027", "2049017129378353", "2049016996045033",
]


def test_fixture_full_walk_matches_story_order():
    data = load_fixture_or_skip("post_media_set")
    rows = [PARSER.flatten(r, "PostMediaSet") for r in data["data"]]
    assert data["result"] == "success"
    assert len(rows) == data["query"]["params"]["expected_count"] == 8
    assert [r["media_id"] for r in rows][:5] == FIXTURE_FIRST_FIVE
    assert [r["file_index"] for r in rows] == list(range(8))
    assert all(r["post_id"] == data["query"]["query"]["post_id"] for r in rows)
    assert all(r["image_url"] and r["width"] for r in rows)


def test_fixture_records_feed_media_pipeline():
    data = load_fixture_or_skip("post_media_set")
    names = [media_filename(e) for r in data["data"] for e in extract_media_from_post(r)]
    assert names == [f"2049017276045005_img_{i:02d}.jpg" for i in range(8)]
