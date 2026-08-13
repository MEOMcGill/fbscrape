"""Async media downloader for scraped Facebook posts.

Walks scraped post dicts via FacebookGraphQLParser._extract_attachments — so URL
discovery (high-res photo path, reel-share inner videos, album recursion) stays
in one place and tracks parser updates automatically.

FB CDN URLs are self-signed (`oh=` signature, `oe=` expiry-as-hex-unix). No cookies
needed, but TTL is short: empirically ~4-5 days from scrape time. Expired URLs
return HTTP 403 with `Bad URL hash` — run download soon after scraping or pipeline
it into the scrape itself.

Three ways to use this module:
  1. Post-hoc — `download_media_from_posts(posts, out_dir)` over a saved file
     (`fbscrape download-media`).
  2. In-scrape, immediate — `build_media_stream_hook(download_media=True,
     media_dir=...)` returns a per-batch coroutine the scrape fires as each
     pagination batch is parsed, so media lands before the signature expires.
  3. In-scrape, handed off — the same hook with `media_manifest=<path.jsonl>`
     appends one JSON line per media item (URL + target filename + timestamp)
     instead of (or alongside) downloading, for a separate process to drain via
     `download_media_from_manifest` / `fbscrape download-media --from-manifest`.
"""

import asyncio
import gzip
import inspect
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator
from urllib.parse import urlparse

import aiohttp

from .logger import logger
from .response import FacebookGraphQLParser, _resolve_story

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "heic")
VIDEO_EXTS = ("mp4", "mov", "webm")

_parser = FacebookGraphQLParser()


def _ext_from_url(url: str, default: str) -> str:
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTS + VIDEO_EXTS:
        if path.endswith("." + ext):
            return ext
    return default


_UNSAFE_ID_CHARS = str.maketrans({c: "_" for c in '/\\:=+ *?"<>|'})


def _safe_id(value) -> str | None:
    """Filename-safe record id. Base64 GraphQL ids carry `/`, `+` and `=`, which
    would break out of the directory or confuse the extension split."""
    if not value:
        return None
    return str(value).translate(_UNSAFE_ID_CHARS)


def _resolve_media_root(post: dict) -> tuple[dict | None, str | None]:
    """Resolve a raw record to the node whose `attachments` carry media, plus the
    id to name files after.

    Stories (UserTimeline / Search / GroupTimeline / PostDetail) resolve via
    `_resolve_story` and key off `post_id`. CommentsList records are Comment
    nodes — no `post_id`, but the same `attachments` shape — so they fall back to
    the raw node, keyed by the numeric comment id the flattener uses
    (`legacy_fbid`, or the numeric tail of the base64 `id`).
    """
    if not isinstance(post, dict):
        return None, None
    story = _resolve_story(post)
    if story:
        return story, _safe_id(story.get("post_id") or story.get("id"))
    node = post.get("node") if isinstance(post.get("node"), dict) else post
    if isinstance(node, dict) and node.get("attachments"):
        comment_id = (
            node.get("comment_id")
            or node.get("legacy_fbid")
            or _parser._decode_b64_legacy(node.get("id"))
            or node.get("id")
        )
        return node, _safe_id(comment_id)
    return None, None


def extract_media_from_post(post: dict, include_thumbnails: bool = False) -> list[dict]:
    """Walk a raw post dict; return list of media entries.

    Each entry: {post_id, kind ('image'|'video'|'video_thumb'), idx, url, ext}.
    Dedupes by URL so reel-share hoists and carousel recursion don't double-count.
    Comment records (CommentsList) are keyed by their comment id — see
    `_resolve_media_root`.
    """
    story, post_id = _resolve_media_root(post)
    if not story or not post_id:
        return []

    entries: list[dict] = []
    seen_urls: set[str] = set()

    def _add(kind: str, url: str | None, ext_default: str):
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        entries.append({
            "post_id": post_id,
            "kind": kind,
            "idx": len(entries),
            "url": url,
            "ext": _ext_from_url(url, ext_default),
        })

    def _walk(att: dict):
        atype = att.get("type")
        if atype in ("photo", "album"):
            _add("image", att.get("image_url"), "jpg")
        if atype == "video":
            _add("video", att.get("video_url"), "mp4")
            if include_thumbnails:
                _add("video_thumb", att.get("thumbnail_url"), "jpg")
        for sub in att.get("subattachments") or []:
            _walk(sub)

    for att in _parser._extract_attachments(story) or []:
        _walk(att)

    return entries


def media_filename(entry: dict) -> str:
    """`<post_id>_<kind>_<idx>.<ext>` — the on-disk name for a media entry.

    Deterministic, so the immediate path and the manifest-handoff path agree on
    filenames (and `skip_existing` recognizes an already-downloaded file).
    Manifest entries carry the name verbatim under `filename`; recompute for
    entries produced fresh by `extract_media_from_post`.
    """
    kind_tag = {"image": "img", "video": "vid", "video_thumb": "thumb"}.get(entry["kind"], "media")
    return f"{entry['post_id']}_{kind_tag}_{entry['idx']:02d}.{entry['ext']}"


async def _fetch_bytes(
    session: aiohttp.ClientSession,
    url: str,
    max_retries: int = 2,
    timeout_sec: int = 60,
) -> bytes | None:
    backoff = 5
    for attempt in range(max_retries + 1):
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"fetch returned {resp.status} for {url[:100]}")
                if resp.status in (403, 404, 410):
                    return None  # signature expired or resource gone — don't retry
        except Exception as e:
            logger.warning(f"fetch failed for {url[:100]}: {e}")
        if attempt < max_retries:
            await asyncio.sleep(backoff)
            backoff *= 2
    return None


def _save_bytes(data: bytes, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


async def download_media_entries(
    entries: list[dict],
    out_dir: str | Path,
    concurrency: int = 8,
    skip_existing: bool = True,
    timeout_sec: int = 60,
) -> dict:
    """Download a list of media entries (as produced by `extract_media_from_post`
    or read back from a manifest) into `out_dir`.

    Returns a dict summary: {total, saved, skipped, failed}.
    """
    if not entries:
        return {"total": 0, "saved": 0, "skipped": 0, "failed": 0}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    counts = {"saved": 0, "skipped": 0, "failed": 0}

    async with aiohttp.ClientSession() as session:

        async def _one(entry: dict):
            path = out / (entry.get("filename") or media_filename(entry))
            if skip_existing and path.exists() and path.stat().st_size > 0:
                counts["skipped"] += 1
                return
            async with sem:
                data = await _fetch_bytes(session, entry["url"], timeout_sec=timeout_sec)
            if data is None:
                logger.warning(f"failed: {path.name}")
                counts["failed"] += 1
                return
            _save_bytes(data, path)
            counts["saved"] += 1
            logger.info(f"saved {path.name} ({len(data):,} bytes)")

        await asyncio.gather(*[_one(e) for e in entries])

    return {"total": len(entries), **counts}


async def download_media_from_posts(
    posts: list[dict],
    out_dir: str | Path,
    include_thumbnails: bool = False,
    concurrency: int = 8,
    skip_existing: bool = True,
    timeout_sec: int = 60,
) -> dict:
    """Download all media for a list of raw post dicts.

    Returns a dict summary: {total, saved, skipped, failed}.
    """
    entries: list[dict] = []
    for p in posts:
        entries += extract_media_from_post(p, include_thumbnails=include_thumbnails)

    return await download_media_entries(
        entries,
        out_dir,
        concurrency=concurrency,
        skip_existing=skip_existing,
        timeout_sec=timeout_sec,
    )


# ---------------------------------------------------------------------------
# Manifest handoff — queue media for a separate downloader process
# ---------------------------------------------------------------------------

def append_media_manifest(
    entries: Iterable[dict],
    path: str | Path,
    context: dict | None = None,
) -> int:
    """Append one JSON line per media entry to `path` (a `.jsonl`, or `.jsonl.gz`).

    Line shape: the entry's `{post_id, kind, idx, url, ext}` plus the resolved
    `filename` and a `queued_at` UTC timestamp, merged with any `context` keys
    (the scrape layer passes `{endpoint, label}` so a consumer can bucket media
    by target). `queued_at` matters: fbcdn signatures expire ~4-5 days out, so a
    draining process can tell how much runway a line has left.

    One `write()` per call (a single line-buffer flush) so concurrent browser
    sessions in the same process can append to one manifest without interleaving
    partial lines. Returns the number of lines written.
    """
    entries = list(entries)
    if not entries:
        return 0

    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    queued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = []
    for entry in entries:
        record = dict(entry)
        record.setdefault("filename", media_filename(entry))
        record["queued_at"] = queued_at
        if context:
            record.update(context)
        lines.append(json.dumps(record, default=str))

    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "at", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))
    return len(lines)


def iter_media_manifest(path: str | Path) -> Iterator[dict]:
    """Yield entries from a media manifest written by `append_media_manifest`.

    Tolerates `.gz`, blank lines, and a truncated final line (a consumer may
    drain a manifest a producer is still appending to).
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"skipping malformed manifest line in {path}")
                continue
            if isinstance(record, dict) and record.get("url"):
                yield record


async def download_media_from_manifest(
    manifest_path: str | Path,
    out_dir: str | Path,
    concurrency: int = 8,
    skip_existing: bool = True,
    timeout_sec: int = 60,
) -> dict:
    """Drain a media manifest into `out_dir`.

    Dedupes on target filename (a manifest can carry the same item twice — e.g.
    a scrape leg that was retried after an account rotation re-fires its hooks).
    Returns the same {total, saved, skipped, failed} summary as the other
    download entry points.
    """
    seen: set[str] = set()
    entries: list[dict] = []
    for record in iter_media_manifest(manifest_path):
        name = record.get("filename") or media_filename(record)
        if name in seen:
            continue
        seen.add(name)
        entries.append(record)

    return await download_media_entries(
        entries,
        out_dir,
        concurrency=concurrency,
        skip_existing=skip_existing,
        timeout_sec=timeout_sec,
    )


# ---------------------------------------------------------------------------
# In-scrape streaming hooks
# ---------------------------------------------------------------------------

# A per-batch sink: called with each list of newly-collected raw records as the
# scrape parses them. Sync or async both work.
PostHook = Callable[[list[dict]], None | Awaitable[None]]


def build_media_stream_hook(
    download_media: bool = False,
    media_dir: str | Path | None = None,
    media_manifest: str | Path | None = None,
    media_concurrency: int = 8,
    include_thumbnails: bool = False,
    media_timeout_sec: int = 60,
    context: dict | None = None,
) -> PostHook | None:
    """Build the per-batch media sink used during a scrape, or None if neither
    the immediate nor the handoff path is enabled.

    - `download_media=True` (requires `media_dir`): fetch each batch's media
      right away. Runs inside the scrape, so pagination waits on it — the media
      lands while its fbcdn signature is certainly fresh, at the cost of a
      slower scrape.
    - `media_manifest=<path>`: append the batch's media entries to a JSONL
      manifest for another process to drain. Costs ~nothing in the scrape loop.

    Both may be enabled at once (download now, keep the manifest as a record of
    what was queued). Failures are logged and swallowed by the caller
    (`combine_post_hooks`) — a CDN hiccup must never abort a scrape.
    """
    if download_media and media_dir is None:
        raise ValueError("download_media=True requires media_dir")
    if not download_media and media_manifest is None:
        return None

    async def _media_cb(batch: list[dict]):
        entries: list[dict] = []
        for post in batch:
            entries += extract_media_from_post(post, include_thumbnails=include_thumbnails)
        if not entries:
            return
        if media_manifest is not None:
            n = append_media_manifest(entries, media_manifest, context=context)
            logger.debug(f"[media] queued {n} item(s) to {media_manifest}")
        if download_media:
            summary = await download_media_entries(
                entries,
                media_dir,
                concurrency=media_concurrency,
                skip_existing=True,
                timeout_sec=media_timeout_sec,
            )
            logger.info(
                f"[media] batch of {summary['total']}: saved={summary['saved']} "
                f"skipped={summary['skipped']} failed={summary['failed']} "
                f"-> {media_dir}"
            )

    return _media_cb


def combine_post_hooks(hooks: list[PostHook | None]) -> PostHook | None:
    """Fold several per-batch sinks (media downloader, manifest writer, the
    caller's own `on_new_posts`) into one coroutine fired per batch of new
    records. Returns None when no sink is enabled.

    Each hook is isolated: one raising does not stop the others, and never
    propagates into the scrape loop — a broken sink must not lose a scrape.
    """
    hooks = [h for h in hooks if h is not None]
    if not hooks:
        return None

    async def _multi(batch: list[dict]):
        for hook in hooks:
            try:
                res = hook(batch)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.warning(f"post hook {getattr(hook, '__name__', hook)!r} raised: {e}")

    return _multi
