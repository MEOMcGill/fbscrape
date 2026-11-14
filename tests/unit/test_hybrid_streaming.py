"""Write-on-parse streaming durability.

`group_timeline_hybrid(stream_to_path=...)` routes each deduped post through the
interceptor's `post_sink` into a `JsonlPostWriter(autoflush=True)`, so a scrape
cancelled or killed mid-run (e.g. an outer wall-clock guard firing after hours)
still leaves every already-parsed post on disk. These tests exercise that path
without a live browser: feed posts through `add_posts` (the same chokepoint the
group pagination loop uses) and assert the file is durable pre-close and reads
back exactly as the downstream collate step parses it.
"""
import json

from fbscrape.response import ResponseInterceptor
from fbscrape.jsonl_store import JsonlPostWriter


def _fake_post(i: int) -> dict:
    # GroupTimeline record shape: dedup key on top-level or node.post_id;
    # downstream collate reads `.data.node`.
    return {"post_id": f"p{i}", "node": {"post_id": f"p{i}", "text": f"post {i} 🤮"}}


def _collate_read(path: str) -> list[str]:
    """Mirror the consumer's collate line-parsing (data -> node -> post_id)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line).get("data")
            if isinstance(rec, dict) and rec.get("node"):
                out.append(rec["node"]["post_id"])
    return out


def test_streamed_posts_durable_before_close(tmp_path):
    """Posts are on disk before finalize/close — the cancel/kill safety property.
    The writer buffers one post to stamp the terminal line, so N adds leave N-1
    durable pre-close and all N after finalize."""
    path = str(tmp_path / "g.json")
    ri = ResponseInterceptor()
    ri.extract_posts = False
    w = JsonlPostWriter(path, {"endpoint": "GroupTimeline"}, "2026-07-21",
                        append=False, compress=False, autoflush=True)
    ri.post_sink = w.write_post
    for b in range(3):
        assert len(ri.add_posts([_fake_post(b * 2), _fake_post(b * 2 + 1)])) == 2

    assert _collate_read(path) == [f"p{i}" for i in range(5)]  # 1 still buffered
    w.finalize("success", None, last_cursor="CUR")
    assert _collate_read(path) == [f"p{i}" for i in range(6)]


def test_streamed_dedup_and_emoji_roundtrip(tmp_path):
    """post_sink honors dedup (add_posts drops seen ids) and emoji survive the
    json.dumps ascii-escape round-trip."""
    path = str(tmp_path / "g.json")
    ri = ResponseInterceptor()
    ri.extract_posts = False
    w = JsonlPostWriter(path, {"endpoint": "GroupTimeline"}, "2026-07-21",
                        append=False, compress=False, autoflush=True)
    ri.post_sink = w.write_post
    ri.add_posts([_fake_post(0), _fake_post(1)])
    assert len(ri.add_posts([_fake_post(1), _fake_post(2)])) == 1  # p1 deduped
    w.finalize("success", None)

    with open(path, encoding="utf-8") as f:
        recs = [json.loads(ln).get("data") for ln in f if ln.strip()]
    nodes = [r["node"] for r in recs if isinstance(r, dict) and r.get("node")]
    assert [n["post_id"] for n in nodes] == ["p0", "p1", "p2"]
    assert any("🤮" in n["text"] for n in nodes)


def test_post_count_is_the_only_valid_progress_signal_under_streaming():
    """The hybrid loops must measure progress with `post_count`, not `len(get_posts())`.

    `add_posts` routes to `post_sink` *instead of* `self.posts` when a sink is
    installed (an intended, separately-tested contract). A loop that derives
    `new_posts_in_iter` from `len(get_posts())` therefore sees zero progress on
    every pagination of a streamed scrape, never resets `no_progress_streak`,
    and bails at `max_no_progress_streak * pagination_count` posts — 90 for
    GroupTimeline, which is exactly what a live run produced (87/89/90/90)
    before this was fixed.
    """
    ri = ResponseInterceptor()
    ri.extract_posts = False
    sunk = []
    ri.post_sink = sunk.append

    progress_by_count, progress_by_list = [], []
    previous_count, previous_len = ri.post_count, len(ri.get_posts())
    for batch in range(3):
        ri.add_posts([_fake_post(batch * 2), _fake_post(batch * 2 + 1)])
        progress_by_count.append(ri.post_count - previous_count)
        progress_by_list.append(len(ri.get_posts()) - previous_len)
        previous_count, previous_len = ri.post_count, len(ri.get_posts())

    assert progress_by_count == [2, 2, 2]   # what the loop now uses — real progress
    assert progress_by_list == [0, 0, 0]    # what it used to use — permanently stalled
    assert len(sunk) == 6
