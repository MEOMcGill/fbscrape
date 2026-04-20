"""Async media downloader for scraped Facebook posts.

Walks raw scraped post dicts, extracts image / video / thumbnail URLs from the
GraphQL response tree, and downloads them concurrently to disk.

FB CDN URLs are self-signed (`oh=` signature, `oe=` expiry) — anyone holding the
URL can fetch it within ~30 days of scraping. No cookies needed.
"""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .logger import logger

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


def _ext_from_url(url: str, default: str) -> str:
    path = urlparse(url).path.lower()
    for ext in IMAGE_EXTS + VIDEO_EXTS:
        if path.endswith("." + ext):
            return ext
    return default


def _resolve_story(post: dict) -> dict | None:
    """Mirrors FacebookGraphQLParser.flatten_post's story resolution."""
    node = post.get("node") or {}
    if "timeline_list_feed_units" in node:
        try:
            return node["timeline_list_feed_units"]["edges"][0]["node"]
        except (KeyError, IndexError, TypeError):
            return None
    if "post_id" in node or "id" in node:
        return node
    return None


def _pick_progressive_video(vdrf: dict) -> str | None:
    """Return the highest-priority progressive mp4 URL if any."""
    vdrr = (vdrf or {}).get("videoDeliveryResponseResult") or {}
    for p in vdrr.get("progressive_urls") or []:
        url = p.get("progressive_url")
        if url:
            return url  # first entry is typically highest quality
    return None


def extract_media_from_post(post: dict, include_thumbnails: bool = False) -> list[dict]:
    """Walk a raw post dict; return list of media entries.

    Each entry: {post_id, kind ('image'|'video'|'video_thumb'), idx, url, ext}.
    Dedupes by URL so nested (carousel inner) videos don't get pulled twice.
    """
    story = _resolve_story(post)
    if not story:
        return []

    post_id = story.get("post_id") or story.get("id")
    if not post_id:
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

    for a in story.get("attachments") or []:
        styles = (a.get("styles") or {}).get("attachment") or {}
        media = styles.get("media") or {}

        # Single photo
        _add("image", ((media.get("photo_image") or {}).get("uri")), "jpg")

        # Video (progressive mp4)
        _add("video", _pick_progressive_video(media.get("videoDeliveryResponseFragment") or {}), "mp4")

        # Video preferred thumbnail
        if include_thumbnails:
            _add("video_thumb", ((media.get("preferred_thumbnail") or {}).get("image") or {}).get("uri"), "jpg")
            # Sometimes a first_frame_thumbnail URL string too
            ffs = media.get("first_frame_thumbnail")
            if isinstance(ffs, str):
                _add("video_thumb", ffs, "jpg")

        # Multi-photo carousel (all_subattachments)
        for sub in ((styles.get("all_subattachments") or {}).get("nodes") or []):
            sub_media = sub.get("media") or {}
            _add("image", (sub_media.get("image") or {}).get("uri"), "jpg")
            # Rare: a sub carousel video
            _add("video", _pick_progressive_video(sub_media.get("videoDeliveryResponseFragment") or {}), "mp4")

        # Nested carousel via style_infos (common for mixed media albums)
        for si in (styles.get("style_infos") or []):
            for inner in ((si.get("containing_story") or {}).get("attachments") or []):
                inner_media = ((inner.get("styles") or {}).get("attachment") or {}).get("media") or (inner.get("media") or {})
                _add("image", ((inner_media.get("photo_image") or {}).get("uri")), "jpg")
                _add("video", _pick_progressive_video(inner_media.get("videoDeliveryResponseFragment") or {}), "mp4")

    return entries


def _filename(entry: dict) -> str:
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
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    for p in posts:
        entries += extract_media_from_post(p, include_thumbnails=include_thumbnails)

    if not entries:
        return {"total": 0, "saved": 0, "skipped": 0, "failed": 0}

    sem = asyncio.Semaphore(concurrency)
    counts = {"saved": 0, "skipped": 0, "failed": 0}

    async with aiohttp.ClientSession() as session:

        async def _one(entry: dict):
            path = out / _filename(entry)
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
