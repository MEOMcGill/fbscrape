# PageTransparency

Single-shot fetch of a Facebook **Page's** transparency record — creation date,
admin-location counts, name-change history, whether the Page runs ads, linked
profile, and related metadata.

## Overview

- **Kind:** single-shot (`hybrid` mode) — one GraphQL POST, no pagination, no
  scroll, no date filter.
- **High-level API:** `scraper.page_transparency(page_id, handle=None)`
- **CLI:** `fbscrape scrape page-transparency <page_id>` (or `<handle>:<page_id>`)
- **Output:** `data = [transparency_dict]` — a single-element list.

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `page_id` | yes | The Page-side node id (e.g. `899800046546098`, `20531316728`). Sent as `variables.pageID`. |
| `handle` | no | Vanity handle. Only sets the warm-up navigation URL (`/<handle>/`) so the request looks like a real user landing on the page. Cosmetic — the GraphQL body always uses `page_id`. |

### `page_id` vs `user_id` — the distinction that bites

`PageTransparency` and [`ProfileAuthenticity`](profile_authenticity.md) look
similar but take **different identifiers**:

- **`page_id`** (e.g. `899800046546098`) — the Page-side node in FB's graph.
  Used here.
- **`user_id`** (e.g. `100044331674441`, `61577505662345`) — the profile id
  (`profile.php?id=…`). Used by `ProfileAuthenticity`. Modern accounts start
  `61…`; classic accounts `100…`.

Passing a `user_id` to `PageTransparency` does **not** error — FB returns
`{"data":{"page":null}}` because `page(id: <user_id>)` resolves to no node, and
`fbscrape` surfaces this as `result="parse_error"`. If you have a `user_id` and
need transparency data, go through the [pipeline](#usage) below.

## Strategy

Single-shot replay — no pagination loop:

1. Navigate to `/<handle or page_id>/`. **Skips the bootstrap scroll** — the
   natural `ProfileTransparencyDialogQuery` only fires from a UI click, so we
   don't wait for it.
2. Capture `{post_data, headers}` from any natural GraphQL POST that fires
   during navigation (`ResponseInterceptor.latest_natural_graphql_request`) —
   this carries the auth-bearing fields (`fb_dtsg`, `lsd`, `__user`, cookies).
3. Synthesize the transparency body: `fb_api_req_friendly_name =
   ProfileTransparencyDialogQuery`, `variables = {"pageID": <page_id>,
   "scale": 3}`, `doc_id = PAGE_TRANSPARENCY_DOC_ID`; splice fresh
   `__csr`/`__dyn`; override the `x-fb-friendly-name` header.
4. One `page.request.post()`. HTTP-status classification via
   `_hybrid_send_replay`.
5. Return `ScrapeOutcome(result="success", data=[transparency_dict])`.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["PageTransparency"]["modes"]["hybrid"]`
(the source of truth). All are timing/robustness knobs — no pagination or date
options for a single-shot endpoint.

| Param | Default | Meaning |
|---|---|---|
| `post_nav_sleep_seconds` | `3.0` | Pause after navigation before synthesizing the replay. |
| `template_capture_timeout` | `45.0` | Max seconds to wait for a natural GraphQL POST to harvest auth fields from. |
| `request_timeout_ms` | `30000` | Per-request timeout for `page.request.post`. |
| `operation_timeout_seconds` | `120` | Overall per-await safety timeout. |

## Usage

```python
from fbscrape import FacebookScraper

async with FacebookScraper(db="accounts.db") as scraper:
    r = await scraper.page_transparency(page_id="899800046546098")
    print(r.result, len(r.data))
    r.save("data/transparency/habsfanhub.jsonl.gz")
```

### Pipeline: `user_id` → `page_id` → transparency

When you start from `user_id`s (or from post `author_id`s, which **are**
`user_id`s) and want transparency data for the subset that has a Page side,
chain [`ProfileAuthenticity`](profile_authenticity.md) into `PageTransparency`.
The bridge is the `delegate_page_id` field on the authenticity response:

```
post.author_id  ──►  ProfileAuthenticity(user_id=author_id)
                                │
                                ▼
                         delegate_page_id?
                          ╱            ╲
                  populated            null
                       │                  │
                       ▼                  ▼
       PageTransparency(             stop — plain User,
       page_id=delegate_page_id,     no linked Page
       handle=author_id)
```

```python
import asyncio
from fbscrape import FacebookScraper, gather
from fbscrape.response import FacebookGraphQLParser

async def main():
    user_ids = ["100044331674441", "61577505662345"]
    parser = FacebookGraphQLParser()

    async with FacebookScraper(db="accounts.db", max_browser_sessions=3) as scraper:
        page_jobs: list[tuple[str, str]] = []  # (user_id, delegate_page_id)
        async for r in gather(scraper.profile_authenticity(uid) for uid in user_ids):
            if not r.data:
                continue
            row = parser.flatten(r.data[0], endpoint="ProfileAuthenticity")
            if row and row.get("delegate_page_id"):
                page_jobs.append((row["user_id"], row["delegate_page_id"]))

        async for r in gather(
            scraper.page_transparency(page_id=pid, handle=uid) for uid, pid in page_jobs
        ):
            print(r.query.query["page_id"], r.result, len(r.data))

asyncio.run(main())
```

`delegate_page_id` is `null` for pure personal profiles, so Stage 2 only runs
for accounts with a linked Page. The `handle=uid` argument is cosmetic.

## Gotchas

- **Wrong id type → `parse_error`, not an exception.** A `user_id` yields
  `{"data":{"page":null}}`. Check `result` before trusting `data`.
- **No post-level signal for "has a Page".** `actors[0].__typename` is always
  `User` on posts, so you can't pre-filter — call `ProfileAuthenticity` on every
  distinct `author_id` and branch on `delegate_page_id`.

## Output shape

`data = [transparency_dict]`. Flattened via
`FacebookGraphQLParser.flatten(record, "PageTransparency")` →
`_flatten_pagetransparency_record`, producing keys such as `page_id`, `name`,
`page_type_name_for_content`, `verification_status`, `category_text`,
`has_run_political_ads`, and `admin_country_counts`. See
`tests/unit/test_flatten_page_transparency.py` for the full key set.
