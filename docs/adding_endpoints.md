# Adding a new endpoint

`fbscrape` is built around an endpoint × mode registry (`Query.ENDPOINT_REGISTRY`). Adding a new scrape target (e.g. `GroupTimeline`, `PageInsights`, `EventGuests`) is a wiring exercise across ~7 files. This guide is opinionated: **hybrid-first**. The scroll-driven `manual` mode exists for `UserTimeline` only and is effectively deprecated — do not add a `manual` mode to new endpoints.

The most recently added endpoint is **`Search`** (`SearchCometResultsPaginatedResultsQuery`). It's the cleanest reference for each touch point — when in doubt, grep for `Search` and copy its shape.

---

## Part 1 — what to give Claude

This is the checklist to paste into a chat with Claude when you want a new endpoint added. Fill it in, attach the captured files, and Claude has everything needed to scaffold the wiring end-to-end (and to ask you targeted follow-ups if anything is missing).

### 1. Endpoint identity

- **GraphQL query name** — the value of `fb_api_req_friendly_name` on the natural request, e.g. `ProfileCometTimelineFeedRefetchQuery`, `SearchCometResultsPaginatedResultsQuery`. You'll see this in DevTools (instructions below).
- **Endpoint label** — Pascal-case, used as the `Query.endpoint` string. Examples already used: `UserTimeline`, `Search`. Pick something short and descriptive (`GroupTimeline`, `EventDiscussion`).

### 2. Inputs the caller will supply

- **Required `query` fields** — what does the user need to provide to start a scrape? (`handle`, `query_text`, `group_id`, `event_id`, `start_date`, `end_date`, ...)
- **Optional / mode params** — anything tunable (per-page count, sleep ranges, max paginations). If you don't know yet, leave blank — Claude will copy the `Search` defaults and prune.

### 3. Navigation URL

The Facebook page where the GraphQL query fires naturally during normal browsing. Include any query-string filters that change behavior — e.g. Search uses `&filters=<base64 blob>` to encode "Latest posts + date range".

If the URL is parameterized, give a literal example: `https://www.facebook.com/groups/123456789012345/`.

### 4. Captured request/response pairs (the most important part)

Claude needs to see what FB's natural request looks like so it can replay it. Two pairs:

- **Pair A — first page (no cursor)**
- **Pair B — paginated page (with cursor)**

#### How to capture (DevTools, ~2 minutes)

1. Open Chrome or Firefox. Press **F12** to open DevTools. Click the **Network** tab.
2. In the filter bar, type `graphql` to narrow to GraphQL requests.
3. Navigate to the URL from #3.
4. Most endpoints need a scroll to fire their pagination query — scroll the page once.
5. In the Network list, find a request whose **Name** column matches the GraphQL query name from #1. (You can also click a request and look for `fb_api_req_friendly_name` in the "Payload" / "Form Data" tab to confirm.)
6. **Save the request** — right-click the request → **Copy** → **Copy as fetch (Node.js)**. Paste into a file named `request_first.txt`.
7. **Save the response** — click the request → **Response** tab → select all → save as `response_first.json` (or `.jsonl` if the body is multiple `{}` objects separated by newlines — common for FB).
8. **Scroll once more** to trigger the next paginated request. Repeat steps 5–7, naming the files `request_paginated.txt` and `response_paginated.json` / `.jsonl`.

You should end with 4 files. Attach all of them.

### 5. Termination contract

When does a scrape stop? Common patterns:

- **Date-bounded** — stop when oldest post in batch < `start_date` (this is what `UserTimeline` hybrid does).
- **Exhaustion-only** — stop when `has_next_page` is false / `end_cursor` is null.
- **Max-results cap** — stop after N posts.
- **FB-imposed degradation** — if you've observed the response shape collapse mid-scrape (like the cursor-reset symptom in `UserTimeline`, see Key Design Decision 16 in CLAUDE.md), note it.

### 6. Optional context that speeds things up

- **Per-page count** — what `count` value does FB's UI use? (UserTimeline = 3, Search = 5.) Visible in the captured request's `variables` form field.
- **Bootstrap scroll required?** — confirm yes/no. Default assumption is yes.
- **Known anti-bot quirks** — anything you've noticed about how this endpoint behaves under repeated automated access.

### Copy-paste template

```
Adding endpoint **<Label>**.

1. GraphQL query name: <e.g. GroupsCometFeedRegularStoriesPaginationQuery>
2. Endpoint label: <e.g. GroupTimeline>
3. Required query fields: <e.g. group_id, start_date, end_date>
   Optional params: <e.g. none / leave for Claude to copy from Search>
4. Navigation URL: <e.g. https://www.facebook.com/groups/123/>
5. Termination: <date-bounded | exhaustion-only | max-results=N | other>
6. Per-page count: <e.g. 6>
   Bootstrap scroll required: <yes | no | unknown>
   Notes: <anything else>

Attached:
- request_first.txt
- response_first.jsonl
- request_paginated.txt
- response_paginated.jsonl
```

---

## Part 2 — what Claude wires up (reference)

Seven touch points, in order. `Search` is the gold-standard example for each.

1. **`Query.ENDPOINT_REGISTRY`** (`fbscrape/models.py`) — add a top-level entry with `query_required` and `modes: {"hybrid": {"params": {...}}}`. Copy the `Search` `params` block and adjust.
2. **`BrowserSession.<endpoint_snake>_hybrid`** (`fbscrape/browser_session.py`) — the per-mode scrape method. Mirrors `search_hybrid` (around `browser_session.py:711`): navigate → bootstrap scroll → capture template via the new interceptor field (#5) → pagination loop → return `ScrapeOutcome`. The pagination loop helper `_hybrid_pagination_loop` is reusable when the response is Story-tree compatible.
3. **`Worker.ENDPOINT_MODE_METHODS`** (`fbscrape/worker.py`) — one row: `("GroupTimeline", "hybrid"): "group_timeline_hybrid"`.
4. **`FacebookScraper.<endpoint_snake>()`** (`fbscrape/scraper.py`) — high-level wrapper. Builds the canonical `Query`, submits via `WorkerPool.submit_task`, returns `ScrapingResult`. Mirrors `FacebookScraper.search` (around `scraper.py:264`).
5. **`ResponseInterceptor`** (`fbscrape/response.py`) — new `latest_<endpoint>_request` capture path so the hybrid template can be harvested from a natural request. Mirrors the `is_scrq` block (around `response.py:880-905`). The hybrid scrape method (#2) polls this field.
6. **`FacebookGraphQLParser`** (`fbscrape/response.py`) — `_flatten_<endpoint>_post` orchestrator + entry in `ENDPOINT_FLATTENERS`. Most FB surfaces share the Comet Story shape, so the orchestrator is usually a thin composition of the existing `_extract_*` methods. Skip this only if you don't need flattening for this endpoint (Search currently skips it — known gap).
7. **`cli.py`** — `@scrape.command(name='<endpoint-kebab>')` subcommand. Mirrors `scrape_search` (around `cli.py:1007`). Click options should mirror the hybrid params from the registry; `Query.ENDPOINT_REGISTRY[<endpoint>]["modes"]["hybrid"]["params"]` is the source of truth for defaults.

---

## Part 3 — verification

1. **Programmatic.** In a short script: `async with FacebookScraper(...) as scraper: result = await scraper.<endpoint>(...)`. Check `len(result.posts) > 0`, `result.query.endpoint == "<Label>"`, posts have a non-null `creation_time`.
2. **CLI.** `fbscrape scrape <endpoint-kebab> ...` against the same target. Same expectations.
3. **Flattener.** If #6 was wired: `fbscrape flatten <output.json>` — confirm the row schema looks reasonable and there are no fields silently dropped.
4. **Long-haul.** Run a scrape long enough to trigger several paginations. Watch for FB-imposed degradation (cursor reset, `errors[]`, HTML response bodies). If you see something odd, capture it and add a stop condition.

---

## Common gotchas

- **Response is JSONL not JSON.** Hybrid responses are often newline-delimited (`{"data":...}\n{"data":...}`). The existing parser handles both via `parse_json_or_jsonl`. Save responses with `.jsonl` extension when that's the case.
- **Token splicing.** `__csr` / `__dyn` are HasteBitMap telemetry that need to be the freshest values from any natural GraphQL POST. The interceptor tracks them globally (`latest_csr` / `latest_dyn`); your hybrid method just splices them into replay bodies. See Key Design Decision 14 in CLAUDE.md.
- **Bootstrap scroll.** Most endpoints don't fire their GraphQL query on raw navigation — they need at least one scroll. See the feedback memory in CLAUDE.md.
- **`cursor=null` + `beforeTime` always set.** For date-filtered endpoints, mirror FB's UI exactly. See Key Design Decision 12 in CLAUDE.md.
- **`afterTime=null`.** FB's UI never sets this; including a non-null value is a fingerprint. Leave it as captured.
- **CLAUDE.md drift.** When the endpoint is added, update CLAUDE.md so future-Claude has accurate context (the "Today: only `UserTimeline` is registered" type lines).
