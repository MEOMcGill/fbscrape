# Search Filters

How to filter Facebook search results, and how to add new filter types.

The `Search` endpoint requires only `query_text`. Everything else — sort order,
date range, author/source — is an **optional filter**. With no filters, Facebook
returns results under its default ranking.

Filters are encoded as a base64 JSON blob in the search URL's `&filters=` param
(see `_build_search_url` in `browser_session.py`). The blob never flows into the
GraphQL replay body — FB enforces it server-side from the URL. For why Search has
no date-based stop conditions, see [`architecture/endpoints.md`](architecture/endpoints.md#search).

---

## Filter dict API

Pass a single `filters` dict to `scraper.search()`:

```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    # No filters — FB default ranking.
    result = await scraper.search("mark carney")

    # Latest posts, in a date range.
    result = await scraper.search("mark carney", filters={
        "recent_posts": {},
        "creation_time": {"start": "2025-01-01", "end": "2025-12-31"},
    })

    # Public posts only.
    result = await scraper.search("mark carney", filters={
        "posts_from": {"source": "public"},
    })
```

Each key is a filter name; each value is that filter's kwargs (`{}` for no-arg
filters). Order is preserved in the blob but FB does not appear to care about it.

A key that is not a known filter and does **not** contain a `:` raises `ValueError`
— this catches typos (`recent_post` → error, not silently-ignored). Keys that
*do* contain a `:` are treated as raw passthrough (see below).

---

## Known filters

Defined in `_SEARCH_FILTER_REGISTRY` (`browser_session.py`).

| Key | Kwargs | Effect | FB blob entry |
|---|---|---|---|
| `recent_posts` | none | Sort by latest instead of relevance | `recent_posts:0` → `{name: "recent_posts", args: ""}` |
| `creation_time` | `start`, `end` (YYYY-MM-DD, either optional) | Server-side date range | `rp_creation_time:0` → `{name: "creation_time", args: "<json>"}` |
| `posts_from` | `source` | "Posts from" author/source filter | `rp_author:0` → `{name: "<see below>", args: ""}` |

`posts_from` accepts one `source` (mutually exclusive):

| `source` | FB inner name |
|---|---|
| `public` | `merged_public_posts` |
| `me` | `author_me` |
| `friends` | `author_friends_feed` |
| `groups_and_pages` | `my_groups_and_pages_posts` |

An invalid `source` raises `ValueError` listing the valid values.

`creation_time` date components are **not** zero-padded (`2025-1-1`, not
`2025-01-01`) — this matches FB's own UI fingerprint. One-sided bounds are fine:
pass only `start` or only `end`.

---

## CLI

```bash
# No filters
fbscrape scrape search 'mark carney'

# Known filters via --filter (repeatable). Dot-notation sets kwargs.
fbscrape scrape search 'mark carney' \
    --filter recent_posts \
    --filter creation_time.start=2025-01-01 \
    --filter creation_time.end=2025-12-31

fbscrape scrape search 'mark carney' --filter posts_from.source=public
```

`--filter` forms:

| Form | Meaning | Example |
|---|---|---|
| `NAME` | no-arg filter | `recent_posts` |
| `NAME.key=value` | set one kwarg | `creation_time.start=2025-01-01` |
| `NAME=JSON` | full kwargs as JSON | `creation_time={"start":"2025-01-01"}` |

---

## Adding a new filter

Facebook has many filters (city, language, etc.) not yet in the registry. The
encoding for any of them can be read straight off a UI URL — no network capture
needed.

**1. Decode a UI URL.** Apply the filter in Facebook's search UI, copy the URL,
and decode the `filters=` param:

```python
import base64, json
blob = "<the filters= value from the URL>"
outer = json.loads(base64.b64decode(blob).decode())
for k, v in outer.items():
    print(k, json.loads(v))   # outer key, {"name", "args"}
```

You now have three pieces: the **outer key** (e.g. `rp_author:0`), the inner
**name**, and the inner **args** (often `""`, sometimes a nested JSON string).

**2a. Use it immediately via raw passthrough** — no code change. Any `filters`
key containing a `:` is written to the blob verbatim:

```python
await scraper.search("carney", filters={
    "city:0": {"name": "city", "args": '{"city_id":"123"}'},
})
```

```bash
# CLI: --raw-filter FB_KEY NAME ARGS_JSON  (args is a JSON *string*, or "")
fbscrape scrape search 'carney' --raw-filter "city:0" "city" '{"city_id":"123"}'
```

**2b. Or promote it to the registry** for a clean named API. Add an entry to
`_SEARCH_FILTER_REGISTRY` mapping a user-facing key → FB outer key prefix, inner
`name`, and an `args` encoder:

```python
"city": {
    "fb_key": "rp_city",                       # ":<index>" is appended automatically
    "name":   "city",
    "encode": lambda city_id: json.dumps({"city_id": city_id}, separators=(",", ":")),
},
```

`name` may also be a callable (`lambda **kwargs: ...`) when one user-facing key
maps to several FB names — see `posts_from`. After adding a registry entry,
extend the round-trip test in `tests/unit/test_search_url_filters.py`.
