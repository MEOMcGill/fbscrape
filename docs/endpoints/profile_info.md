# ProfileInfo

Single-shot fetch of a profile's **header** — name, category, follower/following counts, cover photo, bio, verified badge, and intro-card fields — for a user or page.

## Overview

- **Kind:** single-shot (`hybrid` mode) — one navigation, no pagination, no scroll, no date filter.
- **Not a GraphQL replay:** unlike the timeline endpoints, this does **not** splice and replay a captured GraphQL POST. FB server-renders the profile header into the page document's embedded JSON (`profile_header_renderer.user`), so the scrape navigates to the profile, reads the rendered HTML, and pulls the header node out.
- **High-level API:** `scraper.profile_info(handle)`
- **CLI:** `fbscrape scrape profile-info <handle>...`
- **Output:** `data = [record_dict]` — a single-element list.

Sibling profile-document endpoints: [ProfileAbout](profile_about.md) (header + About sub-tabs), [ProfileAuthenticity](profile_authenticity.md) (account transparency).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity handle **or** numeric id of the profile (user or page). Both resolve via `https://www.facebook.com/<handle>/` and drive the navigation URL. |

## Strategy

`BrowserSession.profile_info_hybrid` (`browser_session.py`):

1. Flush the interceptor; set `extract_posts = False` and `skip_unneeded_body_reads = True` (no XHR body parsing needed — the header is in the document).
2. **Phase 1 — navigate** to `https://www.facebook.com/<handle>/`, pausing `post_nav_sleep_seconds` afterward so the server-rendered header settles.
3. **Phase 2 — settle & read:** wait `document_wait_seconds`, then read the rendered HTML via `page.content()` (guarded by `operation_timeout_seconds`; a hang raises `RendererHangError`).
4. **Phase 3 — extract:** `FacebookGraphQLParser.extract_profile_info(html, handle)` pulls the `profile_header_renderer.user` node out of the document's embedded JSON. `None` → `parse_error` (e.g. profile private / not visible to the account); otherwise `data = [record]`, `result = 'success'`.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["ProfileInfo"]["modes"]["hybrid"]["params"]`.

| Param | Default | Meaning |
|---|---|---|
| `post_nav_sleep_seconds` | `3.0` | Pause after navigating to the profile before proceeding. |
| `document_wait_seconds` | `4.0` | Extra settle time before reading the document, so the server-rendered header blob is present. |
| `operation_timeout_seconds` | `120` | Per-await safety timeout for renderer hangs. |

## Usage

```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    result = await scraper.profile_info("zuck")
    record = result.data[0]  # single header dict
    result.save("output/zuck_profileinfo.jsonl.gz")
```

```bash
# One or more targets (vanity handle or numeric id)
fbscrape scrape profile-info zuck
fbscrape scrape profile-info zuck 100044331674441 --headless

# Read targets from a file with a `handle` column
fbscrape scrape profile-info --input-file profiles.csv
```

Each target is written to `<handle>_profileinfo_<timestamp>.jsonl` under `--output-dir` (default `data/profile_info/`).

## Gotchas

- **`follower_count` / `following_count` are approximate.** FB ships them only as abbreviated strings (e.g. `"121M followers"`), parsed into an order-of-magnitude int (`121000000`), never an exact count. Use for sorting/comparison, not precise reporting.
- **Private / not-visible profiles yield `parse_error`** with empty `data` — the header node is absent from the document, not an exception.
- **Missing intro card or social context does not crash:** dispatched fields fall back to `None`/`[]` (`category`, `follower_count`, `followers_url`, `following_count`, `intro_card_fields`).
- **No pagination / date filter** — `--scroll-threshold` only governs account rotation across many targets.

## Output shape

`data = [profile_dict]`. Flattened via `FacebookGraphQLParser.flatten(record, "ProfileInfo")` → `_flatten_profile_info_record`. Identifying column: `profile_id`.

Flattened keys: `profile_id`, `name`, `url`, `gender`, `username_for_profile`, `is_verified`, `is_viewer_friend`, `is_memorialized`, `follower_count`, `followers_url`, `following_count`, `bio`, `category`, `intro_card_fields`, `cover_photo_url`, `profile_picture_url`.

See `tests/unit/test_flatten_profile_info.py`.
