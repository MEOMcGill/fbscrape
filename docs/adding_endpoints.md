# Adding a new endpoint

`fbscrape` is built around an endpoint × mode registry (`Query.ENDPOINT_REGISTRY`). Adding a new scrape target (e.g. `GroupTimeline`, `PageInsights`, `EventGuests`) is a wiring exercise across the runtime (7 touch points), the stop-condition framework (1 touch point if termination differs from existing endpoints), and the test suite (4 mandatory artifacts). This guide is opinionated: **hybrid-first**. The scroll-driven `manual` mode exists for `UserTimeline` only and is effectively deprecated — do not add a `manual` mode to new endpoints.

The most recently added endpoint is **`CommentsList`** (`CommentsListComponentsPaginationQuery`). It's the cleanest reference for an *exhaustion-only* paginated endpoint whose response isn't Story-shaped — when adding a new endpoint that doesn't fit the Story-stream model, grep for `CommentsList` or `comments_list` and copy its shape (dedicated `_hybrid_comments_pagination_loop` + dedicated `parse_comments_response` parser + minimal stop set). For date-bounded paginated endpoints where FB enforces no server-side date filter, see `GroupTimeline` (Story-shaped). For an URL-filter-driven paginated reference, see `Search`; for a single-shot reference, see `PageTransparency`.

---

## Part 1 — what to give Claude

This is the checklist to paste into a chat with Claude when you want a new endpoint added. Fill it in, attach the captured files, and Claude has everything needed to scaffold the wiring end-to-end (and to ask you targeted follow-ups if anything is missing).

### 1. Endpoint identity

- **GraphQL query name** — the value of `fb_api_req_friendly_name` on the natural request, e.g. `ProfileCometTimelineFeedRefetchQuery`, `SearchCometResultsPaginatedResultsQuery`. You'll see this in DevTools (instructions below).
- **Endpoint label** — Pascal-case, used as the `Query.endpoint` string. Examples already used: `UserTimeline`, `Search`. Pick something short and descriptive (`GroupTimeline`, `EventDiscussion`).

### 2. Inputs the caller will supply

- **Required `query` fields** — what does the user need to provide to start a scrape? (`handle`, `query_text`, `group_id`, `event_id`, ...). `start_date` / `end_date` should usually be **optional** (not in `query_required`) — see Key Design Decision 20 in CLAUDE.md. The CLI layer carries a per-endpoint policy (`require_start`, `require_end`, `default_end_to_today`) to drive what counts as "no dates" + whether a default fill applies. Match what FB's UI sends on the wire: if FB sends `beforeTime` always (UserTimeline), `default_end_to_today=True`; if FB sends no date filter (GroupTimeline), `default_end_to_today=False`.
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

- **Date-bounded** — stop when oldest post in batch < `start_date` (UserTimeline + GroupTimeline+CHRONOLOGICAL). Skipped when `start_date is None` via the existing None guard in `OldestInBatchBelowStartDate.evaluate`.
- **Exhaustion-only** — stop when `has_next_page` is false / `end_cursor` is null. Always enabled; the natural terminator for date-free scrapes.
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

Twelve touch points across three groups: **runtime** (1–7), **stop conditions** (8, conditional), and **tests** (9–12, mandatory). `Search` / `GroupTimeline` are the gold-standard examples for the runtime touch points; the existing single-shot endpoints (`PageTransparency`, `ProfileAuthenticity`) are the references for non-paginated runtime shapes.

### Runtime (7 touch points)

1. **`Query.ENDPOINT_REGISTRY`** (`fbscrape/models.py`) — add a top-level entry with `query_required` and `modes: {"hybrid": {"params": {...}}}`. Copy the `Search` `params` block and adjust. Note: per Key Design Decision 20, `start_date` / `end_date` should usually **not** be in `query_required` — keep them in `params` only if FB's UI sends them on the wire.
2. **`BrowserSession.<endpoint_snake>_hybrid`** (`fbscrape/browser_session.py`) — the per-mode scrape method. Mirrors `search_hybrid` (around `browser_session.py:711`): navigate → bootstrap scroll → capture template via the new interceptor field (#5) → pagination loop → return `ScrapeOutcome`. The pagination loop helper `_hybrid_pagination_loop` is reusable when the response is Story-tree compatible.

   **If the endpoint returns posts/comments with media**, also accept the six streaming kwargs (`on_new_posts`, `download_media`, `media_dir`, `media_manifest`, `media_concurrency`, `include_thumbnails`) and call `self._install_stream_hook(label=..., ...)` immediately after `self.response_interceptor.flush()` — `flush()` clears the hook, so ordering matters. Reusing `_hybrid_pagination_loop` gets the per-batch firing for free; a bespoke loop must call `await self.response_interceptor.fire_new_posts(self.response_interceptor.add_posts(batch))`, and a single-shot document endpoint fires once with its record (see `post_detail_hybrid`). Then thread the same kwargs through #4 (via `scraper._stream_runtime_options` into `Query.runtime_options`) and #7 (the `@media_stream_options` decorator + `_media_runtime_kwargs`). Full mechanism: [`media_streaming.md`](media_streaming.md).
3. **`Worker.ENDPOINT_MODE_METHODS`** (`fbscrape/worker.py`) — one row: `("GroupTimeline", "hybrid"): "group_timeline_hybrid"`.
4. **`FacebookScraper.<endpoint_snake>()`** (`fbscrape/scraper.py`) — high-level wrapper. Builds the canonical `Query`, submits via `WorkerPool.submit_task`, returns `ScrapingResult`. Mirrors `FacebookScraper.search` (around `scraper.py:264`). If the endpoint has no server-side date filter, **omit the multi-leg cursor_reset resume loop** — cursor_reset is terminal for those endpoints (preserve partial data, return).
5. **`ResponseInterceptor`** (`fbscrape/response.py`) — new `latest_<endpoint>_request` capture path so the hybrid template can be harvested from a natural request. Mirrors the `is_scrq` block (around `response.py:880-905`). The hybrid scrape method (#2) polls this field. Single-shot endpoints (PageTransparency, ProfileAuthenticity) skip this and reuse `latest_natural_graphql_request` instead.
6. **`FacebookGraphQLParser`** (`fbscrape/response.py`) — `_flatten_<endpoint>_post` orchestrator + entry in `ENDPOINT_FLATTENERS`. Most FB surfaces share the Comet Story shape, so the orchestrator is usually a thin composition of the existing `_extract_*` methods. Skip this only if you don't need flattening for this endpoint (Search currently skips it — known gap). If your endpoint uses a new edge-container key (e.g. `timeline_list_feed_units`, `group_feed`), also add it to `FacebookGraphQLParser._STORY_EDGE_CONTAINERS` so `parse_timeline_response` fans Shape A correctly.
7. **`cli.py`** — `@scrape.command(name='<endpoint-kebab>')` subcommand. Mirrors `scrape_search` (around `cli.py:1007`). Three sub-tasks:
   - **Click options.** Mirror the hybrid params from the registry; `Query.ENDPOINT_REGISTRY[<endpoint>]["modes"]["hybrid"]["params"]` is the source of truth for defaults.
   - **Per-endpoint CLI policy.** The `_resolve_targets()` call (around `cli.py:835`) takes three flags that encode "what does FB's UI send on the wire" — set them per Key Design Decision 20:
     - `require_start` — `True` only if FB's UI mandates a start_date on this surface (currently only Search).
     - `require_end` — same, for end_date.
     - `default_end_to_today` — `True` when FB always sends `beforeTime` (UserTimeline, Search); `False` when FB sends no date filter (GroupTimeline). A wrong default here makes the scraper's fingerprint deviate from a real user's traffic.
   - **`--continue` / auto-unstick.** If the endpoint paginates, decide whether to wire `_find_unstick_cursor` and `_finalize_continue_result` (UserTimeline + GroupTimeline both do). Single-shot endpoints skip this.
   - **In-scrape media.** If the endpoint returns media-bearing records, apply the `@media_stream_options` decorator, accept the five flag names as parameters, and spread `_media_runtime_kwargs(output_dir, <target label>, ...)` into the scraper call.

### Stop conditions (1 touch point, conditional)

8. **`stop_conditions.assemble_default_stop_conditions`** (`fbscrape/stop_conditions.py`) — only edit if your endpoint has termination semantics that don't fit the existing branches. The function currently switches on `(endpoint, sorting_setting)` to assemble the canonical condition set:
   - **Chronological responses** (UserTimeline, Search, GroupTimeline+CHRONOLOGICAL) get `OldestInBatchBelowStartDate` + `CursorReset`.
   - **Non-chronological responses** (GroupTimeline+TOP_POSTS / +RECENT_ACTIVITY) drop those and rely on `ConsecutiveOutOfRange` instead.
   - `ConsecutiveOutOfRange` is opt-in via `params["max_consecutive_out_of_range"] > 0`.

   If your endpoint introduces a **new sort value** (e.g. a third sorting_setting for GroupTimeline) or **non-monotonic response ordering by default**, extend the `is_chronological` branch logic. If `CursorReset`'s anchor-selection needs an endpoint-specific tweak (GroupTimeline uses `second_oldest_in_batch` to dodge bootstrap-edge highlights — see Key Design Decision 16), update `CursorReset.evaluate` similarly. Most new endpoints can leave this file alone.

### Tests (4 mandatory artifacts)

These are **required**, not optional — the test suite codifies the public contract and catches FB shape drift. See [`tests/README.md`](../tests/README.md) for the three-tier structure.

9. **Fixture capture** (`tests/_capture_fixtures.py`) — add a new entry to `TARGETS` and `CAPTURERS`. Pick a stable public target (a long-lived public profile / group / page). Then run:
   ```bash
   python tests/_capture_fixtures.py --only <new_endpoint>
   ```
   Verifies #1–#7 wire up at all and produces the JSON the unit test loads.
10. **Unit flatten test** (`tests/unit/test_flatten_<new_endpoint>.py`) — model on the existing single-shot ones (`test_flatten_page_transparency.py`, `test_flatten_profile_authenticity.py`) for non-paginated endpoints, or on `test_flatten_group_timeline.py` for paginated ones. Load the captured fixture, flatten via `FacebookGraphQLParser.flatten(record, endpoint)`, assert key fields are populated and the row schema matches expectations.
11. **Integration test** (`tests/integration/test_<new_endpoint>.py`) — mirrors the matching integration test. Headless scrape against real FB with a tight window; asserts `result.result in success_set`, `len(data) > 0`, and that every record flattens. Auto-skips when no active account is available.
12. **Registry tripwire bump** — update `EXPECTED_KEYS` in `tests/unit/test_query_registry.py` (the test `test_endpoint_registry_top_level_keys_pinned`). This is intentionally a tripwire so registry additions can't slip in without a deliberate ack.

---

## Part 3 — verification

1. **Run the test suite.** All four test artifacts from Part 2 §9–12 must pass:
   ```bash
   pytest tests/unit/test_flatten_<new_endpoint>.py tests/unit/test_query_registry.py
   pytest -m integration tests/integration/test_<new_endpoint>.py
   ```
   The unit tier runs on every bare `pytest`, so it's the first to catch regressions when FB shifts shape.
2. **Programmatic smoke.** In a short script: `async with FacebookScraper(...) as scraper: result = await scraper.<endpoint>(...)`. Check `len(result.data) > 0`, `result.query.endpoint == "<Label>"`, posts have a non-null `creation_time`.
3. **CLI smoke.** `fbscrape scrape <endpoint-kebab> ...` against the same target. Same expectations.
4. **Flattener.** If #6 was wired: `fbscrape flatten <output.json>` — confirm the row schema looks reasonable and there are no fields silently dropped.
5. **Long-haul.** For paginated endpoints, run a scrape long enough to trigger several paginations. Watch for FB-imposed degradation (cursor reset, `errors[]`, HTML response bodies). If you see something odd, capture it and either extend an existing `StopCondition` or add a new one in `stop_conditions.py` + wire it through `assemble_default_stop_conditions` (Part 2 §8).

---

## Common gotchas

- **Response is JSONL not JSON.** Hybrid responses are often newline-delimited (`{"data":...}\n{"data":...}`). The existing parser handles both via `parse_json_or_jsonl`. Save responses with `.jsonl` extension when that's the case.
- **Edge-container fan-out.** FB delivers paginated batches as one *bootstrap* line (`data.node = <Container>` with `edges[].node = Story`, possibly multiple stories) plus N *stream* lines (`data.node = Story` directly). `parse_timeline_response` fans both shapes into one entry per Story; if your endpoint uses a new container key, add it to `FacebookGraphQLParser._STORY_EDGE_CONTAINERS` (e.g. `timeline_list_feed_units` for UserTimeline, `group_feed` for GroupTimeline).
- **Token splicing.** `__csr` / `__dyn` are HasteBitMap telemetry that need to be the freshest values from any natural GraphQL POST. The interceptor tracks them globally (`latest_csr` / `latest_dyn`); your hybrid method just splices them into replay bodies. See Key Design Decision 14 in CLAUDE.md.
- **Bootstrap scroll.** Most endpoints don't fire their GraphQL query on raw navigation — they need at least one scroll. See the feedback memory in CLAUDE.md.
- **`cursor=null` + `beforeTime` always set.** For *server-side* date-filtered endpoints, mirror FB's UI exactly. See Key Design Decision 12 in CLAUDE.md. **Note:** not all paginated endpoints expose a `beforeTime` variable — GroupTimeline doesn't. When the GraphQL has no date filter, pass `end_unix=<value>, inject_before_time=False` to `_hybrid_pagination_loop` — `end_unix` still flows into the date-bound stop conditions (`OldestInBatchBelowStartDate`, `ConsecutiveOutOfRange`) but is NOT injected into the request body, so FB doesn't see an unrecognized variable.
- **`afterTime=null`.** FB's UI never sets this; including a non-null value is a fingerprint. Leave it as captured.
- **Choose your sort carefully.** FB's UI default sort is often algorithmic (`TOP_POSTS`, `MOST_RELEVANT`, …). For GroupTimeline, empirically the chronological sort (`CHRONOLOGICAL`) correlates with FB suspending accounts on the endpoint — even though it's the cleanest sort for date-bounded scraping. Default to FB's UI choice (lowest fingerprint) and let the `StopCondition` framework handle non-monotonic ordering via `ConsecutiveOutOfRange`. The default condition set in `stop_conditions.assemble_default_stop_conditions` is sort-aware: non-chronological sorts drop `OldestInBatchBelowStartDate` and `CursorReset` (premises don't hold) and add `ConsecutiveOutOfRange` instead.
- **No multi-leg cursor_reset resume without a server-side date filter.** UserTimeline can resume after `cursor_reset` by advancing `end_date` because `beforeTime` filters server-side. For endpoints without that (GroupTimeline), `cursor_reset` is terminal — the scraper wrapper should NOT loop on it; just preserve partial data.
- **CLAUDE.md drift.** When the endpoint is added, update CLAUDE.md so future-Claude has accurate context (the "Today's registered endpoints: …" line + the per-endpoint strategy section).
