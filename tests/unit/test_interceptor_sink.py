"""ResponseInterceptor.add_posts dedup + write-on-parse sink routing + the
per-batch `on_new_posts` streaming hook.

`add_posts` dedups on post_id (node.post_id || post_id) and routes each *new*
post either to `self.posts` (default — single-shot endpoints) or to a
configured `post_sink` (write-on-parse — paginated hybrid streams to disk). It
returns the posts it actually added, which is what the scrape loops hand to
`fire_new_posts` (the media / caller streaming sinks). `post_count` tracks total
added regardless of route; `flush` resets all of it.
"""

import asyncio

from fbscrape.response import ResponseInterceptor


def test_add_posts_default_appends_to_list_and_counts():
    ri = ResponseInterceptor()
    added = ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}])
    assert len(added) == 2
    ids = [p.get("post_id") or p.get("node", {}).get("post_id") for p in ri.posts]
    assert ids == ["a", "b"]
    assert ri.post_count == 2


def test_add_posts_dedups_on_post_id_node_precedence():
    ri = ResponseInterceptor()
    ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}])
    # re-serve the same ids (top-level + node form) -> deduped
    added = ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}, {"post_id": "c"}])
    assert added == [{"post_id": "c"}]     # only the new one comes back
    assert ri.post_count == 3
    assert len(ri.posts) == 3


def test_add_posts_routes_to_sink_when_set():
    ri = ResponseInterceptor()
    sunk = []
    ri.post_sink = sunk.append
    added = ri.add_posts([{"post_id": "a"}, {"post_id": "b"}, {"post_id": "a"}])  # dup a
    assert len(added) == 2
    assert [p["post_id"] for p in sunk] == ["a", "b"]
    assert ri.posts == []          # nothing accumulated in RAM
    assert ri.post_count == 2


def test_flush_resets_sink_hook_and_count():
    ri = ResponseInterceptor()
    ri.post_sink = lambda p: None
    ri.on_new_posts = lambda batch: None
    ri.add_posts([{"post_id": "a"}])
    ri.flush()
    assert ri.post_sink is None
    assert ri.on_new_posts is None
    assert ri.post_count == 0
    assert ri.posts == []
    assert ri.seen_post_ids == set()


# ---- fire_new_posts: the streaming-hook firing point --------------------------

def test_fire_new_posts_awaits_async_hook_with_deduped_batch():
    ri = ResponseInterceptor()
    seen: list[list[dict]] = []

    async def hook(batch):
        seen.append(batch)

    ri.on_new_posts = hook
    asyncio.run(ri.fire_new_posts(ri.add_posts([{"post_id": "a"}, {"post_id": "b"}])))
    # a re-served post is deduped away, so the hook sees only what's new
    asyncio.run(ri.fire_new_posts(ri.add_posts([{"post_id": "a"}, {"post_id": "c"}])))

    assert [[p["post_id"] for p in b] for b in seen] == [["a", "b"], ["c"]]


def test_fire_new_posts_accepts_sync_hook_and_skips_empty_batch():
    ri = ResponseInterceptor()
    calls = []
    ri.on_new_posts = calls.append

    asyncio.run(ri.fire_new_posts([{"post_id": "a"}]))
    asyncio.run(ri.fire_new_posts([]))          # nothing new -> no call

    assert calls == [[{"post_id": "a"}]]


def test_fire_new_posts_swallows_hook_exception():
    """A broken sink must never take the scrape down with it."""
    ri = ResponseInterceptor()

    def boom(batch):
        raise RuntimeError("sink exploded")

    ri.on_new_posts = boom
    asyncio.run(ri.fire_new_posts([{"post_id": "a"}]))   # does not raise


def test_fire_new_posts_noop_without_hook():
    ri = ResponseInterceptor()
    asyncio.run(ri.fire_new_posts([{"post_id": "a"}]))   # no hook installed
