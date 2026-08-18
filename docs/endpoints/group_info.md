# GroupInfo

Single-shot fetch of a Facebook group's **header** info — name, privacy setting, member count, cover photo, and content-view (tab) directory — read straight from the server-rendered group page.

## Overview

- **Kind:** single-shot (`hybrid` mode) — one navigation, no pagination/scroll/date filter.
- **High-level API:** `scraper.group_info(handle)`
- **CLI:** `fbscrape scrape group-info <handle>...`
- **Output:** `data = [record_dict]` — single-element list.

Unlike [GroupTimeline](group_timeline.md), there is **no GraphQL replay**. FB server-renders the group header (a `Group`-typed node carrying `viewer_join_state`) into the document's embedded JSON, so the scrape navigates to the group page and parses the rendered document directly. Companion of [GroupAbout](group_about.md), which reads the richer `/about/` page.

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity group handle **or** numeric group id. Both resolve via `/groups/<handle>/`, which drives the navigation URL. |

## Strategy

`BrowserSession.group_info_hybrid` (`fbscrape/browser_session.py`):

1. Set `endpoint = "GroupInfo"`; navigate to `https://www.facebook.com/groups/<handle>/`.
2. Flush the interceptor; disable post extraction (`extract_posts = False`) and skip unneeded body reads — this endpoint does not consume GraphQL responses.
3. `_hybrid_navigate` with `post_nav_sleep_seconds` pause; bail with the diagnostic `result` on navigation error.
4. Wait `document_wait_seconds` for the server-rendered header blob to settle, then read `page.content()` (guarded by `operation_timeout_seconds`; a hang raises `RendererHangError`).
5. Parse the document with `FacebookGraphQLParser.extract_group_info(html, handle)`. `None` → `result='parse_error'`, empty data. Otherwise return `data=[record]`, `result='success'`.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["GroupInfo"]["modes"]["hybrid"]["params"]`.

| Param | Default | Meaning |
|---|---|---|
| `post_nav_sleep_seconds` | `3.0` | Pause after navigating to the group page. |
| `document_wait_seconds` | `4.0` | Extra settle time before reading the document, so the header blob is present. |
| `operation_timeout_seconds` | `120` | Per-await safety timeout for renderer hangs. |

CLI overrides: `--post-nav-sleep-seconds`, `--document-wait-seconds`, `--operation-timeout-seconds`. Also `--input-file` (CSV/Parquet/YAML/JSON/JSONL with a `handle` column), `--output-dir` (default `data/group_info/`), `--max-sessions`, `--headless`, `--mobile`, `--wait-for-account`.

## Usage

```bash
fbscrape scrape group-info 392585550772135
fbscrape scrape group-info 392585550772135 --headless
fbscrape scrape group-info --input-file groups.csv
```

```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    result = await scraper.group_info("392585550772135")
    record = result.data[0]
```

## Gotchas

- **`member_count` is approximate.** It ships only as FB's abbreviated display string (e.g. `"120.4K members"`), parsed into an order-of-magnitude int — never an exact integer on this surface.
- **`privacy_label` only.** The header's `privacy_info` carries a single display string (e.g. `"Public group"`); `privacy_description` is always `None` here. GroupAbout carries the richer split label/description.
- **No GraphQL, no pagination, no date filter.** A single navigation returns the whole record; a missing/changed header shape yields `result='parse_error'` with empty data.

## Output shape

`data = [record]` — a single group node. Flattened via `FacebookGraphQLParser.flatten(record, "GroupInfo")` → `_flatten_group_info_record`. Identifying column: `group_id`.

Columns: `group_id`, `name`, `url`, `handle`, `privacy_label`, `privacy_description`, `member_count`, `viewer_join_state`, `cover_photo_url`, `content_views`. `content_views` is the group's tab directory as `{content_view_type: uri}` (e.g. `ABOUT`, `DISCUSSION`, `MEDIA`, ...). Returns `None` on shape mismatch (no `id`). See `tests/unit/test_flatten_group_info.py`.
