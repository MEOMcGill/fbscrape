# PostMediaSet

Fetches every item of a multi-photo post's photo set through FB's media viewer, one GraphQL replay per item.

## Overview
- **Kind:** paginated (`hybrid` mode) — one navigation, then one replay of `CometPhotoRootContentQuery` per item.
- **Why it exists:** feed and permalink Stories (UserTimeline, GroupTimeline, PostDetail, and the production media collector built on them) carry only the **first 5** album subattachments; `all_subattachments.count` holds the real size and there is no cursor for the rest. Posts with 6+ photos therefore lose everything past the 5th. This endpoint walks the post's own photo set and returns all of it.
- **High-level API:** `scraper.post_media_set(post_id, start_media_id, handle=None, **params)`
- **CLI:** `fbscrape scrape post-media-set <post_id>:<start_media_id>` or `--input-file`
- **Output:** one record per item, in the set's order. Records are shaped as one-item albums of the parent post, so the media pipeline (`--download-media`, `download-media`, dt-facebook-crawler's `MediaCollector`) names files `<post_id>_img_NN` like the post's own media.

## Inputs
| Field | Required | Purpose |
|---|---|---|
| `post_id` | yes | Numeric id of the post that owns the set. |
| `start_media_id` | yes | Item to start from. PostDetail's first album subattachment (`attachments[].styles.attachment.all_subattachments.nodes[0].media.id`) for a full walk; the last subattachment you already have to continue past the 5-node cap. |
| `handle` | no | Owner handle; labels and media folder names only (the viewer URL doesn't need it). |

CLI positional form is `post_id:start_media_id` (with optional `--handle`, `--expected-count`), or `--input-file` with columns `post_id`, `start_media_id` and optional `handle`, `mediaset_token`, `skip_media_ids` (list, or ids separated by `;` `,` or spaces), `expected_count`, `media_index_offset` (CSV / Parquet / YAML / JSON / JSONL).

## Strategy
1. Open the viewer: `/photo/?fbid=<start_media_id>&set=<mediaset_token>` (default token `pcb.<post_id>`, the post's own set). `_hybrid_navigate(..., dismiss_overlays=False)` — Escape would close the viewer.
2. Capture the natural `CometPhotoRootContentQuery` (`ResponseInterceptor.latest_cprcq_request`). The viewer prefetches the previous / next items right after load; if nothing fired after 5 s, one `ArrowRight` provokes it. No scrolling (`_hybrid_capture_template(rescroll=False)`).
3. Replay it with `variables.nodeID` = each item and `mediasetToken` = the set. Each response is JSONL (@defer): line 0 has `data.currMedia` (the item — `id`, `__typename`, full-size `image{uri,width,height}` up to 2048 px, `owner`, `accessibility_caption`, `created_time`); a chunk labelled `…CometNoCursorMediaSetViewerNextMediaFetcher_mediaset` has `data.nextMediaAfterNodeId`, the next item **in the requested set**. Parsed by `FacebookGraphQLParser.parse_media_viewer_response`.
4. Emit one record per new item (`build_media_set_record`), fire the streaming sinks, sleep, continue with the next id.

### Termination
The set **wraps** (the last item's next is the first) — there is no end marker. The walk returns `success` at the first of:
- the next id was already visited, is `start_media_id`, or is in `skip_media_ids`;
- `expected_count` distinct items have been seen (skipped ids included);
- there is no next id.

`max_media_reached` when `max_media` items were emitted. Errors follow the CommentsList loop: transport errors return their string, auth GraphQL errors raise `FailedLoginError` (Worker rotates), in-body rate limits return `rate_limit` (Worker locks the account), a response without `currMedia` returns `response_shape_error`.

## Options
Defaults from `Query.ENDPOINT_REGISTRY["PostMediaSet"]["modes"]["hybrid"]["params"]` (the source of truth).

| Param | Default | Meaning |
|---|---|---|
| `mediaset_token` | `""` | Set to walk; `""` → `pcb.<post_id>`. |
| `skip_media_ids` | `[]` | Ids you already have: not emitted, not re-fetched; reaching one ends the walk. |
| `expected_count` | `-1` | Album size if known (`all_subattachments.count`); stop once reached. |
| `max_media` | `-1` | Cap on emitted items. |
| `media_index_offset` | `0` | File index of the first emitted photo (see *Media naming*). |
| `pagination_sleep_mean` / `_std` | `2.0` / `0.5` | Gaussian pause between replays. |
| `template_capture_timeout` | `30.0` | Seconds to wait for the natural query. |
| `post_nav_sleep_seconds` | `3.0` | Pause after opening the viewer. |
| `request_timeout_ms` | `30000` | Per-replay HTTP timeout. |
| `operation_timeout_seconds` | `600` | Per-await safety timeout for hangs. |

## Continuing a Story past its first 5 photos
```python
story = (await scraper.post_detail(handle, post_id)).data[0]          # or a feed record
album = story["node"]["attachments"][0]["styles"]["attachment"]
nodes = album["all_subattachments"]["nodes"]                            # FB sends 5
known = [n["media"]["id"] for n in nodes]
already = len(extract_media_from_post(story))                           # files the Story produced
if album["all_subattachments"].get("count", 0) > len(nodes):
    result = await scraper.post_media_set(
        post_id, known[-1], handle=handle,
        skip_media_ids=known, expected_count=album["all_subattachments"]["count"],
        media_index_offset=already,
        download_media=True, media_dir="data/media/<label>",
    )
```
Costs: one navigation per post plus one replay per *new* item (the start item is fetched once to read its `next`).

```bash
fbscrape scrape post-media-set 2049017276045005:2049016942711705 --expected-count 8 --headless
fbscrape scrape post-media-set --input-file albums.csv --download-media --headless
```

## Media naming
Each record is the parent Story reduced to a one-item album (`node.post_id` = parent post, `node.attachments[0]` = `StoryAttachmentAlbumStyleRenderer` with one subnode), plus `node.media_index_offset`. `extract_media_from_post` starts `idx` at that offset, so a photo gets `<post_id>_img_<file_index>.<ext>` — continuing the post's numbering when the caller passes the number of media entries the Story already produced. Video items are emitted (flattened rows) but carry no playable URL, so — as in Story albums — they produce no file and don't consume an index. Existing files / S3 keys are skipped by the downloader and the MediaCollector, so reruns are safe.

## Gotchas
- **Use the post's set (`pcb.<post_id>`), not the subattachment URL's `set=a.<id>`:** that is the page's general album; walking it returns unrelated photos. Likewise `currMedia.default_mediaset.{next,prev}Media` in the response is the page album and is ignored.
- **The set wraps** — always give a stop (`skip_media_ids`, `expected_count`); the visited-set check is the backstop.
- **Order matches the Story's subattachments** for the first 5 (checked on 2026-11-09 against an 18- and an 8-photo album), so `media_index_offset` numbering lines up.
- **Top-level `post_id` of a record is the item id** (the interceptor's dedup key, as CommentsList does with comment ids); the parent post is `node.post_id` and the flattened `post_id`.
- **Deleted / hidden items** return a response without `currMedia` → `response_shape_error` for that post.

## Output shape
`data = [record, ...]`, one per emitted item. Flattened via `FacebookGraphQLParser.flatten(record, "PostMediaSet")` → `_flatten_post_media_set_record`:
`post_id`, `media_id`, `media_type` (`photo` / `video` / …), `position` (0 = `start_media_id`), `file_index` (None when no file), `mediaset_token`, `image_url`, `width`, `height`, `accessibility_caption`, `owner_id`, `created_at`, `created_at_utc`, `url`. Identifying columns: `post_id`, `media_id`. See `tests/unit/test_post_media_set.py`.
