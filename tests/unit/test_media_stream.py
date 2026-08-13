"""In-scrape media streaming: hook composition, manifest handoff, entry naming.

Covers the pieces of the "download media as you scrape" path that are pure
functions — no network, no browser:

- `extract_media_from_post` on Story-shaped and Comment-shaped records
- `media_filename` determinism (the immediate and handoff paths must agree)
- `append_media_manifest` / `iter_media_manifest` round-trip (plain + gzip)
- `build_media_stream_hook` validation + what each sink does with a batch
- `combine_post_hooks` fan-out, sync/async support, and exception isolation
- `_stream_runtime_options` / `Query.runtime_options` plumbing rules

The network-touching halves (`download_media_entries`,
`download_media_from_manifest`) are exercised by tests/e2e.
"""

import asyncio
import gzip
import json
import os

import pytest

from fbscrape.downloaders import (
    append_media_manifest,
    build_media_stream_hook,
    combine_post_hooks,
    extract_media_from_post,
    iter_media_manifest,
    media_filename,
)
from fbscrape.models import Query
from fbscrape.scraper import _stream_runtime_options


def _photo_attachment(uri: str) -> dict:
    """Photo attachment as FB ships it — high-res URL on `media.photo_image.uri`."""
    return {
        "styles": {
            "__typename": "StoryAttachmentPhotoStyleRenderer",
            "attachment": {"media": {"id": "p1", "photo_image": {"uri": uri}}},
        },
    }


def _story_post(post_id="111", image_url="https://scontent.example/photo.jpg?oh=x&oe=Y"):
    """Minimal Story-shaped record: what a timeline / search / group batch holds."""
    return {"node": {"post_id": post_id, "attachments": [_photo_attachment(image_url)]}}


def _video_story_post(post_id="222"):
    return {
        "node": {
            "post_id": post_id,
            "attachments": [{
                "styles": {
                    "__typename": "StoryAttachmentVideoStyleRenderer",
                    "attachment": {"media": {
                        "id": "v1",
                        "videoDeliveryResponseFragment": {
                            "videoDeliveryResponseResult": {
                                "progressive_urls": [
                                    {"progressive_url": "https://video.example/v.mp4?oh=x"},
                                ],
                            },
                        },
                        "preferred_thumbnail": {"image": {"uri": "https://scontent.example/t.jpg"}},
                    }},
                },
            }],
        }
    }


def _comment_record(comment_id="c_1"):
    """Comment-shaped record (CommentsList): no post_id, same attachments shape.
    FB ships the numeric id as `legacy_fbid` next to a base64 `id`."""
    return {
        "node": {
            "id": "Y29tbWVudDoxMjNfNDU2",       # comment:123_456
            "legacy_fbid": comment_id,
            "attachments": [_photo_attachment("https://scontent.example/c.jpg")],
        }
    }


# ---- extraction ---------------------------------------------------------------

def test_extract_media_from_story_post():
    entries = extract_media_from_post(_story_post())
    assert [e["kind"] for e in entries] == ["image"]
    assert entries[0]["post_id"] == "111"
    assert entries[0]["ext"] == "jpg"


def test_extract_media_thumbnails_are_opt_in():
    assert [e["kind"] for e in extract_media_from_post(_video_story_post())] == ["video"]
    kinds = [e["kind"] for e in extract_media_from_post(_video_story_post(), include_thumbnails=True)]
    assert kinds == ["video", "video_thumb"]


def test_extract_media_from_comment_record_keys_off_comment_id():
    """Comment attachments flow through the same path, named by comment id —
    `_resolve_story` can't resolve them (no post_id), so the fallback matters."""
    entries = extract_media_from_post(_comment_record())
    assert len(entries) == 1
    assert entries[0]["post_id"] == "c_1"
    assert media_filename(entries[0]) == "c_1_img_00.jpg"


def test_extract_media_from_comment_falls_back_to_decoded_b64_id():
    """No `legacy_fbid`: the numeric tail of the base64 `comment:<post>_<id>`
    keeps filenames filesystem-safe (raw base64 carries '/' and '=')."""
    record = _comment_record()
    del record["node"]["legacy_fbid"]
    entries = extract_media_from_post(record)
    assert entries[0]["post_id"] == "123_456"
    assert "/" not in media_filename(entries[0])


def test_extract_media_returns_empty_for_media_free_records():
    assert extract_media_from_post({"node": {"post_id": "1", "attachments": []}}) == []
    assert extract_media_from_post({}) == []
    assert extract_media_from_post({"node": {"id": "x"}}) == []


def test_media_filename_is_deterministic_per_kind():
    entry = {"post_id": "9", "kind": "video", "idx": 3, "ext": "mp4"}
    assert media_filename(entry) == "9_vid_03.mp4"
    assert media_filename(dict(entry, kind="video_thumb", ext="jpg")) == "9_thumb_03.jpg"


# ---- manifest handoff ---------------------------------------------------------

def test_manifest_round_trip_adds_filename_timestamp_and_context(tmp_path):
    path = tmp_path / "queue.jsonl"
    entries = extract_media_from_post(_story_post(post_id="777"))
    written = append_media_manifest(entries, path, context={"endpoint": "GroupTimeline",
                                                           "label": "somegroup"})
    assert written == 1

    (record,) = list(iter_media_manifest(path))
    assert record["filename"] == "777_img_00.jpg"
    assert record["url"] == entries[0]["url"]
    assert record["endpoint"] == "GroupTimeline"
    assert record["label"] == "somegroup"
    assert record["queued_at"]                       # fbcdn TTL runway marker


def test_manifest_appends_across_calls(tmp_path):
    path = tmp_path / "queue.jsonl"
    append_media_manifest(extract_media_from_post(_story_post("a")), path)
    append_media_manifest(extract_media_from_post(_story_post("b")), path)
    assert [r["post_id"] for r in iter_media_manifest(path)] == ["a", "b"]


def test_manifest_empty_batch_writes_nothing(tmp_path):
    path = tmp_path / "queue.jsonl"
    assert append_media_manifest([], path) == 0
    assert not path.exists()


def test_manifest_supports_gzip_and_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "queue.jsonl.gz"
    append_media_manifest(extract_media_from_post(_story_post("g")), path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["post_id"] == "g"
    assert [r["post_id"] for r in iter_media_manifest(path)] == ["g"]


def test_manifest_reader_skips_blank_and_malformed_lines(tmp_path):
    """A consumer may drain a manifest a scrape is still appending to."""
    path = tmp_path / "queue.jsonl"
    append_media_manifest(extract_media_from_post(_story_post("ok")), path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n{\"post_id\": \"trunc\", \"url\":\n")     # torn final line
    assert [r["post_id"] for r in iter_media_manifest(path)] == ["ok"]


# ---- hook construction --------------------------------------------------------

def test_build_media_stream_hook_returns_none_when_no_sink_enabled():
    assert build_media_stream_hook() is None


def test_build_media_stream_hook_requires_media_dir_for_download():
    with pytest.raises(ValueError, match="media_dir"):
        build_media_stream_hook(download_media=True)


def test_media_hook_writes_manifest_without_downloading(tmp_path):
    """Handoff-only: no media_dir needed, nothing fetched."""
    manifest = tmp_path / "queue.jsonl"
    hook = build_media_stream_hook(media_manifest=manifest, context={"label": "zuck"})
    asyncio.run(hook([_story_post("m1"), _comment_record("m2")]))

    records = list(iter_media_manifest(manifest))
    assert [r["post_id"] for r in records] == ["m1", "m2"]
    assert {r["label"] for r in records} == {"zuck"}


def test_media_hook_noop_on_batch_without_media(tmp_path):
    manifest = tmp_path / "queue.jsonl"
    hook = build_media_stream_hook(media_manifest=manifest)
    asyncio.run(hook([{"node": {"post_id": "x", "attachments": []}}]))
    assert not manifest.exists()


# ---- hook composition ---------------------------------------------------------

def test_combine_post_hooks_none_when_empty():
    assert combine_post_hooks([None, None]) is None


def test_combine_post_hooks_fans_out_to_sync_and_async_sinks():
    seen_sync, seen_async = [], []

    async def async_sink(batch):
        seen_async.append(len(batch))

    hook = combine_post_hooks([seen_sync.append, async_sink])
    asyncio.run(hook([{"post_id": "a"}, {"post_id": "b"}]))

    assert seen_sync == [[{"post_id": "a"}, {"post_id": "b"}]]
    assert seen_async == [2]


def test_combine_post_hooks_isolates_a_raising_sink():
    """One broken sink must not stop the others, nor reach the scrape loop."""
    ran = []

    def boom(batch):
        raise RuntimeError("sink exploded")

    hook = combine_post_hooks([boom, ran.append])
    asyncio.run(hook([{"post_id": "a"}]))          # does not raise
    assert ran == [[{"post_id": "a"}]]


# ---- BrowserSession wiring ----------------------------------------------------

def _bare_session(endpoint: str):
    """A BrowserSession with only the interceptor wired — `_install_stream_hook`
    and `fire_new_posts` need nothing else (no browser, no account)."""
    from fbscrape.browser_session import BrowserSession
    from fbscrape.response import ResponseInterceptor

    session = BrowserSession.__new__(BrowserSession)
    session.endpoint = endpoint
    session.response_interceptor = ResponseInterceptor()
    return session


def test_install_stream_hook_arms_interceptor_and_records_context(tmp_path):
    manifest = tmp_path / "queue.jsonl"
    session = _bare_session("GroupTimeline")
    session._install_stream_hook(label="somegroup", media_manifest=manifest)
    assert session.response_interceptor.on_new_posts is not None

    ri = session.response_interceptor
    asyncio.run(ri.fire_new_posts(ri.add_posts([_story_post("s1")])))

    (record,) = list(iter_media_manifest(manifest))
    assert record["endpoint"] == "GroupTimeline"      # context comes from the session
    assert record["label"] == "somegroup"
    assert record["post_id"] == "s1"


def test_install_stream_hook_leaves_interceptor_untouched_when_no_sinks():
    session = _bare_session("UserTimeline")
    session._install_stream_hook(label="zuck")
    assert session.response_interceptor.on_new_posts is None


def test_install_stream_hook_runs_caller_hook_alongside_manifest(tmp_path):
    manifest = tmp_path / "queue.jsonl"
    seen = []
    session = _bare_session("UserTimeline")
    session._install_stream_hook(
        label="zuck", media_manifest=manifest, on_new_posts=seen.append,
    )

    ri = session.response_interceptor
    asyncio.run(ri.fire_new_posts(ri.add_posts([_story_post("s2")])))

    assert [p["node"]["post_id"] for batch in seen for p in batch] == ["s2"]
    assert len(list(iter_media_manifest(manifest))) == 1


# ---- CLI flag translation -----------------------------------------------------

def test_cli_media_kwargs_empty_when_no_flags():
    from fbscrape.cli import _media_runtime_kwargs
    assert _media_runtime_kwargs("out", "zuck", False, None, None, 8, False) == {}


def test_cli_media_dir_implies_download_and_nests_per_target():
    from fbscrape.cli import _media_runtime_kwargs
    kwargs = _media_runtime_kwargs("out", "zuck", False, "/data/media", None, 8, False)
    assert kwargs["download_media"] is True
    assert kwargs["media_dir"] == os.path.join("/data/media", "zuck")


def test_cli_download_media_defaults_dir_under_output_dir():
    from fbscrape.cli import _media_runtime_kwargs
    kwargs = _media_runtime_kwargs("out", "somegroup", True, None, None, 4, True)
    assert kwargs["media_dir"] == os.path.join("out", "media", "somegroup")
    assert kwargs["media_concurrency"] == 4
    assert kwargs["include_thumbnails"] is True


def test_cli_manifest_only_leaves_media_dir_unset():
    """Handoff without downloading: nothing to write locally, so no directory."""
    from fbscrape.cli import _media_runtime_kwargs
    kwargs = _media_runtime_kwargs("out", "zuck", False, None, "q.jsonl", 8, False)
    assert kwargs["download_media"] is False
    assert kwargs["media_dir"] is None
    assert kwargs["media_manifest"] == "q.jsonl"


# ---- scraper / Query plumbing -------------------------------------------------

def test_stream_runtime_options_none_when_nothing_enabled():
    assert _stream_runtime_options() is None


def test_stream_runtime_options_validates_download_needs_dir():
    with pytest.raises(ValueError, match="media_dir"):
        _stream_runtime_options(download_media=True)


def test_stream_runtime_options_bundles_enabled_sinks(tmp_path):
    opts = _stream_runtime_options(media_manifest=str(tmp_path / "q.jsonl"))
    assert opts["download_media"] is False
    assert opts["media_manifest"].endswith("q.jsonl")
    assert set(opts) == {
        "on_new_posts", "download_media", "media_dir", "media_manifest",
        "media_concurrency", "include_thumbnails",
    }


def test_runtime_options_excluded_from_query_serialization_and_equality():
    """Streaming sinks are per-call machinery, not part of the reproducible
    scrape spec — a saved ScrapingResult must not carry (or choke on) them."""
    base = dict(endpoint="GroupTimeline", mode="hybrid", query={"handle": "g"})
    plain = Query(**base, params={})
    streaming = Query(**base, params={}, runtime_options={
        "download_media": True, "media_dir": "/tmp/media", "on_new_posts": print,
    })

    assert "runtime_options" not in streaming.to_dict()
    assert json.loads(streaming.to_json()) == json.loads(plain.to_json())
    assert streaming == plain                     # compare=False on the field
    assert "runtime_options" not in repr(streaming)


def test_worker_spreads_runtime_options_onto_the_session_method(monkeypatch):
    """The end of the plumbing: Worker hands runtime_options to the scrape method
    as kwargs, next to query + params. Without this the sinks silently never arm."""
    from datetime import datetime, timedelta, timezone

    import fbscrape.worker as worker_mod
    from fbscrape.models import ScrapeOutcome
    from fbscrape.worker import Worker

    captured: dict = {}

    class _FakeSession:
        scrolls_recorded = 0

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def group_timeline_hybrid(self, **kwargs):
            captured.update(kwargs)
            return ScrapeOutcome(
                result="success", data=[],
                time_started=datetime.now(timezone.utc),
                time_taken=timedelta(seconds=1),
            )

    monkeypatch.setattr(worker_mod, "BrowserSession", _FakeSession)

    class _Acct:
        identifier = "a@example.com"
        display_name = "a@example.com"

    worker = Worker(id="w0", pool=object())
    worker.current_account = _Acct()

    query = Query(
        endpoint="GroupTimeline", mode="hybrid", query={"handle": "g"}, params={},
        runtime_options={"download_media": True, "media_dir": "/tmp/media",
                         "media_manifest": None, "media_concurrency": 4,
                         "include_thumbnails": True, "on_new_posts": None},
    )
    result = asyncio.run(worker.execute_task(query))

    assert result.result == "success"
    assert captured["download_media"] is True
    assert captured["media_dir"] == "/tmp/media"
    assert captured["media_concurrency"] == 4
    assert captured["include_thumbnails"] is True
    assert captured["handle"] == "g"                 # query fields still arrive
    assert captured["pagination_count"] == 3         # and registry params


def test_runtime_option_keys_match_browser_session_signature():
    """Worker spreads runtime_options as kwargs onto the BrowserSession method,
    so every key must be a real parameter on each media-capable scrape method."""
    import inspect

    from fbscrape.browser_session import BrowserSession

    keys = set(_stream_runtime_options(media_manifest="q.jsonl"))
    for method_name in (
        "user_timeline_manual", "user_timeline_hybrid", "search_hybrid",
        "group_timeline_hybrid", "comments_list_hybrid", "post_detail_hybrid",
    ):
        params = set(inspect.signature(getattr(BrowserSession, method_name)).parameters)
        assert keys <= params, f"{method_name} is missing {keys - params}"
