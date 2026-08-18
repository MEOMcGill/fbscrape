"""Shared 'Comet Story' flatten contract — parametrized across every endpoint
whose flattener produces the Comet row schema (UserTimeline, GroupTimeline,
Search).

These three endpoints all route through `_flatten_pctfrq_post` (or a thin
alias), so the *structural* invariants are identical. Rather than copy the same
~10 assertions into three `test_flatten_<endpoint>.py` files, the contract runs
once here, parametrized by a small per-endpoint spec. The few things that
genuinely differ between endpoints (flatten-rate floor, single- vs multi-author,
whether attachments are expected, how the fixture is loaded) are expressed as
*data* on `CometEndpoint`, not as duplicated test bodies.

Endpoint-specific behavior that does NOT generalize stays in its own file:
  - UserTimeline: `_extract_engagement` variant A/B unit tests + the curated
    variant_b fixture suite + author-is-zuck + shared_post  ->
    `test_flatten_user_timeline.py`.
  - CommentsList / PageTransparency / ProfileAuthenticity are not Comet Stories
    and keep their own flatten tests.

`COMET_KEYS` is the single source of truth for the flattened row schema; it was
previously copy-pasted into three files.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from fbscrape.response import FacebookGraphQLParser
from tests.conftest import load_fixture_or_skip

PARSER = FacebookGraphQLParser()

# The Comet row schema. Single source of truth — imported by
# test_flatten_user_timeline.py for its variant_b key-set check too.
COMET_KEYS = {
    # ids + urls
    "post_id", "story_id", "url", "permalink_url",
    # times
    "created_at", "created_at_utc",
    # audience
    "privacy",
    # author
    "author_id", "author_name", "author_url", "author_type", "author_promode_badge",
    # message
    "text", "hashtags", "mentions", "external_urls",
    # music
    "music_artist", "music_title",
    # flags
    "is_reel", "is_live", "is_repost",
    # engagement
    "reactions", "like", "love", "haha", "wow", "sad", "angry", "care",
    "shares", "comments", "video_views", "video_duration_sec",
    # composites
    "top_comments", "attachments", "shared_post",
}


@dataclass(frozen=True)
class CometEndpoint:
    """Per-endpoint knobs for the shared contract. The few real differences
    between UserTimeline / GroupTimeline / Search live here as data, not as
    duplicated test bodies."""
    id: str                       # pytest param id + flatten() endpoint label
    loader: Callable[[], list]    # returns the raw record list (or skips)
    min_flatten_rate: float       # zuck=0.95 (noisy), group/search=1.0
    multi_author: bool            # profile=False, group/search=True
    attachments_required: bool    # zuck posts media heavily; groups may be text-only


def _fixture_loader(name: str) -> Callable[[], list]:
    def load():
        data = load_fixture_or_skip(name)
        recs = data.get("data") or data.get("posts") or []
        if not recs:
            pytest.skip(f"{name} fixture has no records — recapture")
        return recs
    return load


def _search_capture_loader() -> list:
    """Search isn't a checked-in fixture — it loads the raw JSONL capture in
    tmp/ and runs it through parse_search_response. Skipped when absent so
    contributors without the capture don't fail CI."""
    root = Path(__file__).resolve().parent.parent.parent
    cap = root / "tmp" / "endpoint_additions" / "Search" / "response_first.jsonl"
    if not cap.exists():
        pytest.skip(
            f"missing capture {cap.relative_to(root)} — "
            f"add the file to run this test (see tmp/endpoint_additions/Search/)"
        )
    parsed = PARSER.parse_search_response(
        cap.read_bytes(), "https://www.facebook.com/api/graphql/"
    )
    recs = (parsed or {}).get("posts") or []
    if not recs:
        pytest.skip("parse_search_response returned no records — response shape may have changed")
    return recs


ENDPOINTS = [
    CometEndpoint("UserTimeline",  _fixture_loader("user_timeline_hybrid"),
                  min_flatten_rate=0.95, multi_author=False, attachments_required=True),
    CometEndpoint("GroupTimeline", _fixture_loader("group_timeline"),
                  min_flatten_rate=1.0,  multi_author=True,  attachments_required=False),
    CometEndpoint("Search",        _search_capture_loader,
                  min_flatten_rate=1.0,  multi_author=True,  attachments_required=False),
]


@pytest.fixture(scope="module", params=ENDPOINTS, ids=lambda e: e.id)
def spec(request):
    return request.param


@pytest.fixture(scope="module")
def records(spec):
    return spec.loader()


@pytest.fixture(scope="module")
def flat_rows(spec, records):
    rows = [r for r in (PARSER.flatten(rec, spec.id) for rec in records) if r is not None]
    if not rows:
        pytest.skip(f"{spec.id}: no records flattened — likely a parser/shape change")
    return rows


# --- the contract: one body each, run once per endpoint ---------------------

def test_flatten_rate(spec, records, flat_rows):
    """Most records should flatten — a high failure rate signals shape drift.
    The floor is per-endpoint (zuck's live timeline is noisier than the curated
    group/search captures)."""
    pct = len(flat_rows) / len(records)
    assert pct >= spec.min_flatten_rate, (
        f"{spec.id}: only {len(flat_rows)}/{len(records)} ({pct:.0%}) flattened "
        f"(floor {spec.min_flatten_rate:.0%}) — check for unrecognized story shapes"
    )


def test_full_key_set(flat_rows):
    """Schema stability: every row must carry every Comet key (value may be None)."""
    for i, row in enumerate(flat_rows):
        missing = COMET_KEYS - row.keys()
        assert not missing, f"row {i} missing keys: {missing}"


def test_post_id_and_url_always_populated(flat_rows):
    """post_id and one of url/permalink_url must always be present — if either
    is None we've lost the ability to reference the post."""
    for i, row in enumerate(flat_rows):
        assert row["post_id"], f"row {i}: missing post_id"
        assert row["url"] or row["permalink_url"], f"row {i}: no url"


def test_created_at_is_unix_timestamp(flat_rows):
    """created_at is the FB-side `creation_time` as unix seconds."""
    for i, row in enumerate(flat_rows):
        ts = row["created_at"]
        if ts is not None:
            assert isinstance(ts, (int, float)) and ts > 1_000_000_000, f"row {i}: {ts!r}"


def test_some_records_have_a_recognized_timestamp(flat_rows):
    """If zero records carry created_at, the timestamp metadata-strategy typename
    has likely been renamed by FB — the symptom Key Design Decision 18 documents."""
    assert sum(1 for r in flat_rows if r["created_at"] is not None) > 0, (
        "no record produced a created_at — check _METADATA_TIMESTAMP_TYPENAMES"
    )


def test_author_diversity(spec, flat_rows):
    """Group feeds and search results are multi-author; a profile timeline is
    overwhelmingly one author. (The profile case's identity pin lives in the
    endpoint-specific test_author_is_zuck.)"""
    if len(flat_rows) < 5:
        pytest.skip(f"only {len(flat_rows)} rows in fixture — not enough for a diversity check")
    n = len({r["author_id"] for r in flat_rows if r["author_id"]})
    if spec.multi_author:
        assert n >= 2, (
            f"{spec.id}: only {n} distinct author_id across {len(flat_rows)} rows — "
            f"expected a multi-author feed"
        )
    else:
        assert n >= 1


def test_every_row_has_an_author(flat_rows):
    """Every Story should attribute a poster. Missing both author_id and
    author_name on the same row signals an extractor regression."""
    for i, row in enumerate(flat_rows):
        assert row["author_id"] or row["author_name"], (
            f"row {i}: neither author_id nor author_name populated"
        )


def test_reaction_counts_are_nonneg_ints(flat_rows):
    for r in flat_rows:
        for k in ("like", "love", "haha", "wow", "sad", "angry", "care"):
            v = r[k]
            assert isinstance(v, int) and v >= 0


def test_post_ids_are_unique(flat_rows):
    """Cross-pagination dedup (ResponseInterceptor.add_posts via seen_post_ids)
    should leave the accumulated post list free of duplicates."""
    ids = [r["post_id"] for r in flat_rows if r["post_id"]]
    assert len(ids) == len(set(ids)), (
        f"duplicate post_ids in flattened rows: {len(ids) - len(set(ids))} duplicates "
        f"across {len(ids)} rows — dedup regression"
    )


def test_attachments_shape_when_present(spec, flat_rows):
    """Where attachments are present, every entry must carry the uniform
    attachment key set (CLAUDE.md: "uniform attachment shape")."""
    ATTACHMENT_KEYS = {
        "type", "id", "url",
        "image_url", "image_lowres_url", "thumbnail_url",
        "width", "height", "accessibility_caption",
        "video_url", "video_duration_sec", "video_is_live",
        "video_permalink_url", "video_captions_url",
        "link_title", "link_description", "link_source", "link_destination_url",
        "subattachments",
    }
    KNOWN_TYPES = {"photo", "video", "link", "album", "reel_share", "unavailable", "unknown"}

    seen_any = False
    for r in flat_rows:
        for a in (r["attachments"] or []):
            seen_any = True
            assert ATTACHMENT_KEYS == a.keys(), (
                f"attachment shape drift: missing={ATTACHMENT_KEYS - a.keys()}, "
                f"extra={a.keys() - ATTACHMENT_KEYS}"
            )
            assert a["type"] in KNOWN_TYPES, f"unknown attachment type {a['type']!r}"

    if not seen_any:
        if spec.attachments_required:
            pytest.fail(f"{spec.id}: no record carried an attachments[] — fixture may be too narrow")
        pytest.skip(f"{spec.id}: no attachments[] in this fixture's window — nothing to assert")


def test_message_entities_typed_correctly_when_present(flat_rows):
    """hashtags/mentions/external_urls extracted from `message.ranges` — where
    populated, the value must be a non-empty list of the right type."""
    for r in flat_rows:
        if r["hashtags"] is not None:
            assert isinstance(r["hashtags"], list) and r["hashtags"]
            assert all(isinstance(h, str) for h in r["hashtags"])
        if r["mentions"] is not None:
            assert isinstance(r["mentions"], list) and r["mentions"]
            assert all(isinstance(m, dict) and "id" in m for m in r["mentions"])
        if r["external_urls"] is not None:
            assert isinstance(r["external_urls"], list) and r["external_urls"]
            assert all(isinstance(u, str) for u in r["external_urls"])


def test_top_comments_shape_when_present(flat_rows):
    """Where top_comments populates, each entry has the documented keys."""
    EXPECTED = {"text", "author_id", "author_name", "author_url", "created_at", "reactions"}
    for r in flat_rows:
        for c in (r["top_comments"] or []):
            assert EXPECTED <= c.keys()
