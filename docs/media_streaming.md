# Media streaming: collecting media during a scrape

**Last Updated:** 2026-08-13

fbcdn media URLs are self-signed — `oh=` is the signature, `oe=` the expiry as a
hex unix timestamp — and empirically go stale ~4-5 days after the scrape, after
which they answer HTTP 403 `Bad URL hash`. The post-hoc `fbscrape download-media`
command therefore races the clock: a scrape that runs for three days and gets
downloaded a week later loses its earliest media.

The streaming path closes that gap by moving media collection *into* the scrape,
with two sinks that fire on every batch of newly-parsed records:

- **immediate** — download the batch's photos/videos right there
  (`download_media=True` + `media_dir`);
- **handoff** — append one JSONL line per media item to a manifest for a separate
  process to fetch (`media_manifest=<path>`).

Both can be on at once. With neither on, nothing is installed and the scrape is
byte-for-byte what it was before.

Ported from `igscrape`, which uses the same `runtime_options` → per-batch-hook
shape (there the immediate sink is video-only and the handoff is a raw JSONL post
dump; here the handoff is a media manifest, because FB signature expiry is the
problem being solved).

---

## The chain

```
CLI flags  (--download-media / --media-dir / --media-manifest /
            --media-concurrency / --include-thumbnails)
  → cli._media_runtime_kwargs(output_dir, label, ...)      # per-target media dir
  → FacebookScraper.<endpoint>(..., download_media=, media_dir=, ...)
  → scraper._stream_runtime_options(...)   → Query.runtime_options   (or None)
  → Worker.execute_task: method(**query, **params, **runtime_options)
  → BrowserSession.<endpoint>_<mode>(..., download_media=, ...)
  → BrowserSession._install_stream_hook(label, ...)
      → downloaders.build_media_stream_hook(...)     # the media sink(s)
      → downloaders.combine_post_hooks([media_cb, on_new_posts])
      → ResponseInterceptor.on_new_posts = <composed hook>
  → per batch: ResponseInterceptor.fire_new_posts(add_posts(batch))
```

`Query.runtime_options` is deliberately *not* part of the scrape spec: it's
`compare=False, repr=False` and absent from `to_dict()` / `to_json()`, so a saved
`ScrapingResult` still records exactly the reproducible query (and doesn't try to
JSON-encode a callback). It rides `Query` only so `Worker` can spread it as
kwargs the way it already spreads `params`.

## Where the hook fires

`ResponseInterceptor.add_posts` returns the posts it actually added (post-dedup),
and `fire_new_posts` hands that list to the hook — awaiting it when it's async,
logging and swallowing anything it raises. Two call sites, never overlapping:

| Path | Fires from | Batch = |
| --- | --- | --- |
| `manual` mode | `ResponseInterceptor.intercept_response` (auto-extract branch) | posts parsed from one intercepted GraphQL response |
| `hybrid` paginated | `_hybrid_pagination_loop` / `_hybrid_comments_pagination_loop`, right after `add_posts` | posts/comments from one replay |
| `PostDetail` | `post_detail_hybrid`, before returning | the single extracted record |

Hybrid sets `extract_posts = False`, so the interceptor's auto-extract branch
never runs there — no double-firing. In the paginated loops the hook fires
*before* the stop-condition walk, so the batch that terminates the loop still
gets its media.

`flush()` clears `on_new_posts`, and every scrape method installs the hook right
after its `flush()` — install before that and it would be wiped.

## Sinks

`downloaders.build_media_stream_hook` returns one coroutine (or `None` when
neither sink is enabled) that, per batch:

1. walks each record via `extract_media_from_post` → entries
   `{post_id, kind, idx, url, ext}`. Media discovery reuses the parser's
   `_extract_attachments`, so high-res photo paths, album recursion and
   reel-share inner videos track parser updates automatically. Comment records
   (CommentsList) have no `post_id` but the same `attachments` shape, so
   `_resolve_media_root` falls back to the numeric comment id (`legacy_fbid`, or
   the numeric tail of the base64 `id`) — see `_safe_id` for why raw base64 ids
   are not used as filenames;
2. appends them to the manifest, when one is configured;
3. downloads them, when `download_media=True`, at `media_concurrency` in
   parallel, skipping files already on disk.

Filenames come from `media_filename` — `<post_id>_img_00.jpg`, `_vid_00.mp4`,
`_thumb_00.jpg` — the single naming authority shared by the post-hoc path, the
immediate path, and a manifest drain. That's what makes a drain into the same
directory a no-op instead of a re-download.

### Manifest format

One JSON object per line, `.jsonl` or `.jsonl.gz`:

```json
{"post_id": "1234", "kind": "image", "idx": 0, "url": "https://scontent...",
 "ext": "jpg", "filename": "1234_img_00.jpg",
 "queued_at": "2026-08-13T18:20:04+00:00",
 "endpoint": "GroupTimeline", "label": "albertaseparatism"}
```

- `queued_at` is the signature-runway marker: a consumer can tell how close a
  line is to the ~4-5 day cliff.
- `endpoint` / `label` come from the session, so one manifest can serve a whole
  multi-target run and still be bucketable per target.
- `append_media_manifest` does one `write()` per batch, so several concurrent
  browser sessions appending to the same manifest can't interleave partial lines.
- `iter_media_manifest` tolerates blank lines and a torn final line — draining a
  manifest a scrape is still appending to is a supported workflow.
- `download_media_from_manifest` dedupes on target filename, because a retried
  leg (account rotation mid-task) re-fires its hooks and can queue an item twice.

## Cost model

The immediate sink runs inside the pagination loop: the loop waits on the batch's
downloads before its next replay. That's the price of a guaranteed-fresh
signature, and it's why the manifest exists. For a long multi-day scrape, the
manifest plus a draining worker is the better shape; for a short scrape of a
media-heavy target, `--download-media` alone is simpler.

Each batch opens its own `aiohttp.ClientSession`. Batches are seconds apart and
only a handful of posts wide, so the lost connection reuse is noise next to the
scrape's own pacing.

## Gotchas

- `download_media=True` without `media_dir` raises `ValueError` at the API
  boundary (`_stream_runtime_options`), before an account is acquired. On the CLI,
  `--media-dir` implies `--download-media`.
- Media is fetched for every post in the batch, including posts outside the date
  window — the same set that lands in the output file (date bounds are enforced
  by stop conditions, not by dropping records).
- Link-preview thumbnails are not downloaded: the walker handles `photo`, `album`
  and `video` attachments only. Same as the post-hoc path.
- A retried task (account rotation, renderer hang) re-installs the sinks on the
  fresh session and re-fires from the start of the leg. Downloads skip existing
  files; the manifest may carry duplicate lines, which the drain dedupes.
- No cookies are sent for media fetches — fbcdn URLs are self-authenticating, so
  media traffic carries no account risk.
- **Manual mode is fire-and-forget.** Playwright's `Page` is an
  `AsyncIOEventEmitter`, so the response handler (and therefore the hook) runs as
  its own task: manual-mode scrolling never waits on a download, but a batch still
  in flight when the session closes can be cut short. Hybrid mode awaits its hook
  inline, so it has no such tail. Prefer the manifest for manual mode, or just use
  hybrid (manual is deprecated).

## Related

- `fbscrape/downloaders.py` — extraction, naming, download, manifest, hook builders
- `BrowserSession._install_stream_hook` — composition + arming
- `ResponseInterceptor.add_posts` / `fire_new_posts` — firing points
- `tests/unit/test_media_stream.py` — pure-function coverage of the whole chain
- `tests/e2e/test_scrape_with_streaming_media.py` — CLI smoke over both sinks
