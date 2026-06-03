"""ResponseInterceptor.add_posts dedup + write-on-parse sink routing.

`add_posts` dedups on post_id (node.post_id || post_id) and routes each *new*
post either to `self.posts` (default — manual mode / single-shot) or to a
configured `post_sink` (write-on-parse — paginated hybrid streams to disk).
`post_count` tracks total added regardless of route; `flush` resets both.
"""
from __future__ import annotations

from fbscrape.response import ResponseInterceptor


def test_add_posts_default_appends_to_list_and_counts():
    ri = ResponseInterceptor()
    added = ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}])
    assert added == 2
    ids = [p.get("post_id") or p.get("node", {}).get("post_id") for p in ri.posts]
    assert ids == ["a", "b"]
    assert ri.post_count == 2


def test_add_posts_dedups_on_post_id_node_precedence():
    ri = ResponseInterceptor()
    ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}])
    # re-serve the same ids (top-level + node form) -> deduped
    added = ri.add_posts([{"post_id": "a"}, {"node": {"post_id": "b"}}, {"post_id": "c"}])
    assert added == 1
    assert ri.post_count == 3
    assert len(ri.posts) == 3


def test_add_posts_routes_to_sink_when_set():
    ri = ResponseInterceptor()
    sunk = []
    ri.post_sink = sunk.append
    added = ri.add_posts([{"post_id": "a"}, {"post_id": "b"}, {"post_id": "a"}])  # dup a
    assert added == 2
    assert [p["post_id"] for p in sunk] == ["a", "b"]
    assert ri.posts == []          # nothing accumulated in RAM
    assert ri.post_count == 2


def test_flush_resets_sink_and_count():
    ri = ResponseInterceptor()
    ri.post_sink = lambda p: None
    ri.add_posts([{"post_id": "a"}])
    ri.flush()
    assert ri.post_sink is None
    assert ri.post_count == 0
    assert ri.posts == []
    assert ri.seen_post_ids == set()
