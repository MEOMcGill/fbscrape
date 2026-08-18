# PostDetail

Single-shot fetch of one Facebook post's full content by its permalink.

## Overview
- **Kind:** single-shot (`hybrid` mode) — one navigation, no pagination, no scroll, no date filter.
- **No GraphQL replay:** unlike [PageTransparency](page_transparency.md) / [ProfileAuthenticity](profile_authenticity.md), FB server-renders the post's Comet Story into the permalink document's embedded JSON, so the scrape reads the rendered document and extracts the Story directly.
- **High-level API:** `scraper.post_detail(handle, post_id, is_group=False)`
- **CLI:** `fbscrape scrape post-detail <handle>:<post_id> [--group]`
- **Output:** `data = [{"node": story}]` — a single-element list. Flattens to one post row with the **same schema** as a [UserTimeline](user_timeline.md) / [GroupTimeline](group_timeline.md) post.

## Inputs
| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity handle / numeric id of the group, page, or user that owns the post. Drives the navigation URL. |
| `post_id` | yes | Numeric post id (e.g. `27209929835285847`) **or** `pfbid`-form; both resolve via the permalink redirect. |

CLI positional form is `handle:post_id` pairs (space-separated for multiple), or `--input-file` with required `handle` and `post_id` columns (CSV / Parquet / YAML / JSON / JSONL).

## Strategy
1. Build the permalink: `--group` → `/groups/<handle>/posts/<post_id>/`; otherwise `/<handle>/posts/<post_id>/`. FB does not cross-resolve the two surfaces, so the caller must declare which one the post lives on.
2. Navigate to the permalink (`_hybrid_navigate`), pausing `post_nav_sleep_seconds` so FB server-renders the Story.
3. Wait `document_wait_seconds` for the rendered document to settle, then read the page HTML (`page.content()`).
4. Extract the Story from the document's embedded Relay JSON via `FacebookGraphQLParser.extract_permalink_story(html, post_id)`.
5. Return a `ScrapeOutcome` — `data = [{"node": story}]` with `result='success'`, or `[]` with `result='parse_error'` when the Story is absent (deleted / not visible to the account).

## Options
Defaults from `Query.ENDPOINT_REGISTRY["PostDetail"]["modes"]["hybrid"]["params"]` (the source of truth).

| Param | Default | Meaning |
|---|---|---|
| `is_group` | `False` | `True` → `/groups/<handle>/posts/<post_id>/`; `False` → `/<handle>/posts/<post_id>/`. Set by CLI `--group`. |
| `post_nav_sleep_seconds` | `3.0` | Pause after navigating to the permalink. |
| `document_wait_seconds` | `4.0` | Extra settle time before reading the rendered document, so the Story blob is present. |
| `operation_timeout_seconds` | `120` | Per-await safety timeout for hangs. |

## Usage
```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    result = await scraper.post_detail(
        "albertansunitedtostoptheucp", "27209929835285847", is_group=True
    )
    post = next(result.iter_posts())
```

```bash
# Group post
fbscrape scrape post-detail albertansunitedtostoptheucp:27209929835285847 --group
# Page / user post
fbscrape scrape post-detail zuck:10115311901107991
# From a file of handle,post_id rows
fbscrape scrape post-detail --input-file posts.csv --group
```

## Gotchas
- **`--group` matters:** a group post fetched without `--group` (or vice versa) will not resolve — FB does not cross-resolve the two permalink surfaces.
- **`parse_error` is not a crash:** deleted, private, or not-visible-to-the-account posts return `data = []` with `result='parse_error'`. Check `result` before assuming a post row.
- **No date range / pagination:** exactly one record per target; date and cursor knobs do not apply.
- **`post_id` accepts both forms:** numeric or `pfbid`; both survive the permalink redirect.

## Output shape
`data = [{"node": story}]`. Flattened via `FacebookGraphQLParser.flatten(record, "PostDetail")` → `_flatten_postdetail_record`, which routes through the timeline flattener (`_flatten_pctfrq_post`). The row carries the **same key set** as a GroupTimeline / UserTimeline post — `post_id`, `author_name`, `created_at_utc`, `text`, `permalink_url`, etc. Identifying column: `post_id`. See `tests/unit/test_flatten_post_detail.py` (which pins the schema equal to a GroupTimeline row).
