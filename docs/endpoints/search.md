# Search

Scrape Facebook's post search results for one or more query terms.

## Overview

- **Kind:** paginated post stream (`hybrid` mode).
- **High-level API:** `scraper.search(query_text, ..., filters=None, max_posts=-1, **params)`
- **CLI:** `fbscrape scrape search '<query>' [--filter …]`
- **Output:** `data = list[dict]` — one element per search-result post.
- **Target query:** `SearchCometResultsPaginatedResultsQuery` (SCRQ).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `query_text` | yes | The search string. |
| `filters` | no | Sort order, date range, and source filters — baked into the navigation URL, not GraphQL variables. |

Search takes **no `start_date`/`end_date`** directly. Date bounding is a
filter: `creation_time`. See [`../search_filters.md`](../search_filters.md) for
the full registry and the add-a-filter recipe. Known filters:

- `recent_posts` — sort by latest instead of relevance.
- `creation_time` — server-side date range (`{"start","end"}`, one-sided OK).
- `posts_from` — `{"source": "public"|"me"|"friends"|"groups_and_pages"}`.

Unknown FB filters pass through verbatim, so new filters work without code
changes — decode the `filters=` param from a UI URL to discover the shape.

## Strategy

Same overall shape as [UserTimeline](user_timeline.md) hybrid, with four
differences:

1. **URL-filter-driven, no required dates.** All filtering is optional and
   encoded into the navigation URL as a base64 JSON blob via
   `_build_search_url`. The replay body inherits `args.filters` from the
   captured template; only `cursor` + `count` are overridden.
2. **Different response shape.** SCRQ returns
   `data.serpResponse.results.edges[]`; stories sit at
   `edge.rendering_strategy.view_model.click_model.story`. Non-story edges are
   skipped. Dedicated `parse_search_response`; flattener aliases
   `_flatten_pctfrq_post` (same Comet Story shape once extracted).
3. **Non-null initial cursor.** SCRQ's first request carries
   `cursor={"page_number":0,…}`; `search_hybrid` extracts it from the template
   and passes it as `initial_cursor`.
4. **No date-based stop conditions.** Search results aren't strictly
   chronological, so termination is exhaustion/cap-only. No `--continue`.

## Options

| Param / flag | Default | Meaning |
|---|---|---|
| `pagination_count` | `5` | Results per replay (matches FB UI). |
| `filters` / `--filter` | — | See above / `search_filters.md`. |
| `max_posts` / `--max-posts` | `-1` | Cap on accumulated posts (`-1` = no cap). |
| `max_paginations` / `--max-paginations` | `-1` | Safety cap on replays. |
| `max_no_progress_streak` | `5` | Bail after N replays with no new posts. |

Pacing knobs are in `Query.ENDPOINT_REGISTRY["Search"]["modes"]["hybrid"]`.

## Usage

```bash
# No filters → FB default ranking
fbscrape scrape search 'mark carney'

# Recent + bounded to a date window
fbscrape scrape search 'mark carney' --filter recent_posts \
    --filter creation_time.start=2025-01-01 --filter creation_time.end=2025-12-31

# Several queries, from a file
fbscrape scrape search --input-file queries.csv --filter recent_posts
```

```python
async with FacebookScraper(db="accounts.db") as scraper:
    r = await scraper.search("mark carney", filters={"recent_posts": {},
                                                     "creation_time": {"start": "2025-01-01"}})
```

## Gotchas

- **No resume.** `cursor_reset` is terminal; there's no `--continue` or
  auto-unstick for Search.
- **Filters are optional but load-bearing for cost.** With no `creation_time`,
  a broad term can page a long way — cap with `--max-posts` /
  `--max-paginations`.

## Output shape

`data = list[dict]`, one per post. Flattened via
`FacebookGraphQLParser.flatten(record, "Search")` → `_flatten_pctfrq_post`
(same keys as UserTimeline). See `tests/unit/test_flatten_comet_contract.py`.
