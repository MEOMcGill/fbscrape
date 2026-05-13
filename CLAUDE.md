# Project Context

**Last Updated:** 2026-05-13

`fbscrape` is a Facebook timeline scraper built on Camoufox (stealth Firefox) with persistent SQLite-backed account rotation, support for parallel browser sessions, and two pluggable scrape strategies per endpoint.

This document describes how the codebase works *today*. For evolving design decisions, deeper rationale, and historical research, see [`docs/`](docs/).+

**Notes**:
- if an update is made to the codebase (e.g. an endpoint is added, behavior is modified, or something is remove), update the CLAUDE.md file where necessary and the README.md documentation and the various documentation in the ```docs``` folder so it stays up to date and tracks all changes made to the codebase.
- do not make updates to the codebase unless specifically asked to or given permission to
---

## How the codebase works today

### The data flow

```
caller code
    │
    ▼
FacebookScraper.user_timeline(handle, start_date, end_date, mode="hybrid", **params)
    │
    │  builds a Query from the (endpoint, mode) registry; validates;
    │  fills param defaults
    ▼
WorkerPool.submit_task(query) → asyncio.Future
    │
    │  lazily inits N workers (N = min(max_workers, active_accounts));
    │  each worker pulls (query, future) tuples off a shared queue
    ▼
Worker.execute_task(query)
    │
    │  acquires/owns an account; creates a fresh BrowserSession per task;
    │  catches typed exceptions and decides rotation policy
    ▼
BrowserSession.user_timeline_{mode}(...)
    │
    │  per-mode method dispatched via Worker.ENDPOINT_MODE_METHODS;
    │  performs the actual scrape; returns ScrapeOutcome (Query-agnostic)
    ▼
Worker composes ScrapingResult.from_outcome(query, outcome)
    │
    ▼
Future resolves; caller awaits the result
```

### Two scrape strategies for `UserTimeline`

Both produce identical `ScrapingResult` shape — caller code doesn't care which one was used.

**`mode="manual"`** — scroll-driven path.
- Navigates to the profile, scrolls in a loop, intercepts natural `ProfileCometTimelineFeedRefetchQuery` (PCTFRQ) responses via `ResponseInterceptor`, populates posts via auto-callback (`extract_posts=True`).
- Stops on: target start_date reached, no-new-posts streak, GraphQL silence (`stall_timeout_seconds`), DOM error condition (private profile, etc.).
- Wraps known-risky awaits in `asyncio.wait_for(operation_timeout_seconds)` to bound renderer-wedge damage.

**`mode="hybrid"`** *(default)* — page.request.post() driven path.
- Navigates, fires one bootstrap scroll to provoke a natural PCTFRQ, captures its form body + headers as a replay template (via `ResponseInterceptor.latest_pctfrq_request`).
- Disables post auto-extraction (`extract_posts=False`) so unfiltered natural responses don't leak off-range posts into the result.
- Replay loop: every replay sends `cursor=null` (first iter) or `end_cursor` (subsequent), `beforeTime=min(end_of_day(end_date), now_utc)`, `count=N`. `afterTime` stays at the captured `null` (matches FB's UI; including it is a fingerprint).
- Stop conditions: (a) `end_cursor` null in response, (b) oldest post in batch < `start_unix`, (c) no-progress streak hits `max_no_progress_streak`, (d) **cursor-reset detector** fires (oldest post jumps newer by > `HYBRID_CURSOR_RESET_JUMP_SECONDS`, signaling FB silently degraded the response stream — see Key Design Decision 16), (e) **response-shape error** — parser found posts but every one had an unrecognized timestamp metadata-strategy typename, terminal/non-retryable (see Key Design Decision 18).
- Token splicing: `__csr` / `__dyn` from any natural GraphQL POST (organic scroll bursts every N paginations refresh them) overridden into replay bodies.
- HTTP errors mapped to typed exceptions (see *Account lifecycle* below). 5xx retried with backoff.

See [`docs/hybrid/overview.md`](docs/hybrid/overview.md) for empirical evidence behind each design rule.

### Single-shot scrape strategy for `PageTransparency`

`mode="hybrid"` only — there is no pagination, no scroll, no date filter. `Query.query` carries the numeric `page_id`; `handle` is optional (when supplied, it's used for the navigation URL; otherwise FB redirects `/<page_id>/` to the canonical page).

- Navigates to `https://www.facebook.com/<handle or page_id>/` (warm-up + natural traffic). Skips bootstrap scroll — the natural `ProfileTransparencyDialogQuery` only fires from a UI click, so waiting for it is pointless.
- Captures `{post_data, headers}` from *any* natural GraphQL POST (`ResponseInterceptor.latest_natural_graphql_request`). Cross-cutting auth-bearing fields (`fb_dtsg`, `lsd`, `__user`, `__csr`, `__dyn`, etc.) are session-wide, not per-query.
- Synthesizes the transparency body by overriding `fb_api_req_friendly_name=ProfileTransparencyDialogQuery`, `variables={"pageID": <page_id>, "scale": 3}`, `doc_id=PAGE_TRANSPARENCY_DOC_ID` on the captured template; everything else inherits. Splices freshest `__csr` / `__dyn` (same as the paginated paths). Header `x-fb-friendly-name` is overridden to match the form field.
- Single `page.request.post()` to `/api/graphql/`. HTTP-status classification reuses `_hybrid_send_replay` (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → bounded retry).
- Returns `ScrapeOutcome(result='success', data=[transparency_dict])` — `data` is a 1-element list, not a post stream.

### Single-shot scrape strategy for `ProfileAuthenticity`

Same shape as `PageTransparency` — single-shot, no pagination, no scroll, no date filter. `Query.query` carries the numeric `user_id` only; no handle is needed (FB redirects `/<user_id>/` to the canonical profile).

- Navigates to `https://www.facebook.com/<user_id>/` (warm-up + natural traffic). Skips bootstrap scroll — the natural `ProfileCometDirectoryAuthenticityModalQuery` only fires from a UI click on FB's "About this profile / authenticity" modal.
- Reuses the same `latest_natural_graphql_request` template-capture path as PageTransparency (cross-cutting auth-bearing fields are session-wide).
- Synthesizes the authenticity body by overriding `fb_api_req_friendly_name=ProfileCometDirectoryAuthenticityModalQuery`, `variables={"scale": <scale>, "userID": <user_id>}`, `doc_id=PROFILE_AUTHENTICITY_DOC_ID`. Splices freshest `__csr` / `__dyn`. Header `x-fb-friendly-name` is overridden to match the form field.
- Single `page.request.post()` to `/api/graphql/`. HTTP-status classification reuses `_hybrid_send_replay`.
- Returns `ScrapeOutcome(result='success', data=[authenticity_dict])` — the dict is `data.user` from the GraphQL response. Top-level fields include `id`, `name`, `delegate_page_id`, and a nested `profile_directory_authenticity_modal` with `header_fields[]` (profile join date, profile-updated-since, category, transparency link), `meta_verified_section`, and `about_fields[]`. The flattener dispatches `header_fields[]` by `profile_field_type` (`PROFILE_JOIN_DATE` → `profile_join_date`, `PROFILE_UPDATED_SINCE` → `profile_updated_since`, `CATEGORY` → `category`, `TRANSPARENCY` → `transparency_present` bool).

### Endpoint × mode registry (`Query.ENDPOINT_REGISTRY`)

Single source of truth for what scrape requests are valid:

```
ENDPOINT_REGISTRY[endpoint] = {
    "query_required": [field, ...],          # required keys in `query`
    "modes": {
        mode_name: {
            "params": {param_name: default}, # default = None means required
        },
    },
}
```

Today's registered endpoints: `UserTimeline` (`manual` + `hybrid`), `Search` (`hybrid` only), `PageTransparency` (`hybrid` only — single-shot, no pagination), `ProfileAuthenticity` (`hybrid` only — single-shot, no pagination). Adding a new endpoint = one nested dict entry + a `BrowserSession` method per mode + a row in `Worker.ENDPOINT_MODE_METHODS` + a flattener entry. See [`docs/adding_endpoints.md`](docs/adding_endpoints.md) for the full playbook.

`Query.__post_init__` validates the endpoint, mode, required query fields, and param keys; fills defaults from the registry. Unknown params raise `ValueError`.

### Key types (`models.py`)

- `Query(endpoint, mode, query, params)` — the scrape spec. Validated at construction. `query` carries required fields (`handle`, `start_date`, `end_date`); `params` carries mode-specific tunables.
- `ScrapeOutcome(result, data, time_started, time_taken)` — Query-agnostic outcome from `BrowserSession`. Doesn't know which Query produced it. `data` is `list[dict]` always — post-stream endpoints (UserTimeline / Search) populate one element per post; single-record endpoints (PageTransparency) populate a 1-element list.
- `ScrapingResult(query, result, data, time_started, time_taken)` — final result. Composed by `Worker` via `ScrapingResult.from_outcome(query, outcome)` so the canonical Query is constructed exactly once and never rebuilt downstream. (Saved JSON files use `"data": [...]` on the wire; the CLI `flatten` / `download-media` loaders also accept the legacy `"posts": [...]` shape from pre-rename outputs.)

### Account lifecycle / rotation (`Worker.execute_task` + `accounts_pool.py`)

`Worker` owns an account, creates a fresh `BrowserSession` per task, and catches typed exceptions to decide rotation policy:

| Exception | Action | Counts as retry? |
|---|---|---|
| `AccountDisabledError` | rotate (no retry burn) | no |
| `CheckpointError` | rotate + retry | yes |
| `TransientLoginError` | rotate (account stays active) | yes |
| `FailedLoginError` | mark inactive + rotate | yes |
| `AccountBannedError` | mark inactive + rotate | yes |
| `RateLimitError` | lock 1h + rotate | yes |
| `NoAccountError` | put task back; stop | — |

Hybrid mode raises these from `_hybrid_send_replay` based on HTTP status (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → bounded retry then bail) and from response-body shape (HTML body or auth-ish errors[] → `FailedLoginError`).

Account rotation has a 5-minute cooldown lock to prevent immediately re-acquiring the same account.

### `ResponseInterceptor` state (`response.py`)

Set up on every `BrowserSession`. Hooks into `page.on("response")`. Tracks:

- `posts: list[dict]` — accumulator. Auto-populated by `parse_timeline_response` when `extract_posts=True` (default; manual mode keeps it on, hybrid disables).
- `add_posts(posts)` — public API hybrid uses to append manually-parsed replay results.
- `graphql_request_count`, `last_response_time` — drive the manual-mode stall watchdog.
- `viewer_seen` — `True` once any GraphQL response body contains a non-null `data.viewer` (canonical login-success marker, doesn't depend on DOM).
- `latest_csr` / `latest_dyn` — freshest tokens parsed from any natural GraphQL POST (manual replays via `page.request.post` bypass the page event stream and don't pollute these). Hybrid splices into replay bodies.
- `latest_pctfrq_request` / `latest_scrq_request` — `{post_data, headers}` of the most recent natural PCTFRQ / SCRQ. UserTimeline / Search hybrid poll these for template capture.
- `latest_natural_graphql_request` — `{post_data, headers}` of the most recent natural GraphQL POST regardless of friendly-name. Used by single-shot endpoints (PageTransparency, ProfileAuthenticity) that synthesize their own body and only need cross-cutting auth-bearing fields.
- `network_capture` — full request+response of every observed response. **Off by default**; opt-in via `FB_NETWORK_CAPTURE_ALL=1` env var. Used for offline forensic analysis (see `tmp/hybrid/`).
- `flush()` — resets transient state between scrapes; preserves `extract_posts` (it's a behavior flag, not transient state).

### Post flattening (`response.py` + `cli.py flatten`)

Raw `ScrapingResult.data` records are deeply nested GraphQL trees. `FacebookGraphQLParser.flatten(record, endpoint)` collapses one record into a row dict; the CLI `flatten` command writes a directory of these as csv / jsonl / parquet.zstd via polars.

**Layered architecture.** Per-aspect `_extract_*` methods on `FacebookGraphQLParser` each consume a Story dict and return a partial dict (ids/urls, times, audience, author, message+entities, music, flags, engagement, top_comments, attachments, shared_post). Endpoint orchestrators (`_flatten_<endpoint>_post`) compose them into the full row. Most FB surfaces share the same Story shape (the "Comet" UI), so adding a new endpoint flattener is mostly an orchestration call plus a registry entry.

**Endpoint registry.**
```python
FacebookGraphQLParser.ENDPOINT_FLATTENERS = {
    "UserTimeline":         "_flatten_pctfrq_post",
    "PageTransparency":     "_flatten_pagetransparency_record",
    "ProfileAuthenticity":  "_flatten_profile_authenticity_record",
    # Search: "_flatten_search_result_post" later
}
```
The CLI reads `query.endpoint` from the saved JSON and routes through this registry; `--endpoint` overrides it.

**Field coverage (UserTimeline orchestrator).** Per row: ids/urls/times, privacy, author (id/name/url/type/promode_badge), text + `hashtags` / `mentions` / `external_urls` (extracted from `message.ranges` typed entities), `music_artist`/`music_title` (when posted with audio attribution), `is_reel`/`is_live`/`is_repost`, full reaction breakdown (like/love/haha/wow/sad/angry/care + total), shares, comments, video_views, video_duration_sec, `top_comments` (list of dicts), `attachments` (recursive list — see below), `shared_post` (recursive dict — abbreviated by FB so usually only ids/urls/times/author).

**Uniform attachment shape.** Every attachment fills the same keys regardless of type — type-specific extras get None when not applicable. Types: `photo`, `video`, `link`, `album`, `reel_share`, `unavailable`, `unknown`. Photos/album-covers carry `image_url` (high-res) + `image_lowres_url`; videos carry `video_url` (progressive mp4) + `thumbnail_url` + `video_permalink_url` + `video_duration_sec` + `video_captions_url`; link previews carry `link_title`/`link_description`/`link_source`/`link_destination_url` + a `thumbnail_url`. Albums recurse via `subattachments[]` (mixed-media albums populate `video_url` on video subnodes); reel shares hoist the inner reel's permalink/thumbnail/duration/video_url to the outer attachment for ergonomic single-level access. `download-media` consumes this shape directly via `FacebookGraphQLParser._extract_attachments`, so URL discovery stays in one place.

**Metadata dispatch by `__typename`.** `comet_sections.context_layout.story.comet_sections.metadata[]` is a non-deterministic list of typed strategies. `_metadata_by_typenames(story, typenames)` dispatches by typename rather than positional index. Each constant is a **tuple of candidate typenames** (`_METADATA_TIMESTAMP_TYPENAMES`, `_METADATA_AUDIENCE_TYPENAMES`, `_METADATA_MUSIC_TYPENAMES`) checked in order — first match wins. This absorbs FB's sibling strategy renames without code changes: e.g. the timestamp surface returns `CometFeedStoryLongerTimestampStrategy` for some renderings and `CometFeedStoryMinimizedTimestampStrategy` for others, with an identical `story.creation_time` payload. New strategies (location, sponsored, …) plug in either as new constants or as extra entries in an existing tuple.

**Output flow (CLI).** Rows go through `pl.json_normalize(rows, separator='__', infer_schema_length=None)` so nested dicts (`shared_post.*`) become `__`-separated columns; lists stay typed (`pl.List[pl.Struct]`). Parquet writes natively with `compression='zstd'`. CSV serializes List/Struct cells as JSON strings (round-trippable via `json.loads`). JSONL writes the raw pre-normalized row dicts to preserve original nesting.

### Optional debug capture

Set `FB_NETWORK_CAPTURE_ALL=1` to record every browser response (XHR + JS + CSS + images, with binaries metadata-only) into `ResponseInterceptor.network_capture`. Dump to JSONL via `save_network_capture_to_jsonl(path)`. Off by default to keep production memory tight; hybrid does **not** rely on it.

---

## Key Design Decisions

1. **Email OR Phone** — accounts identified by either, enforced by SQL CHECK constraint. `Account.identifier` returns email if present, else phone.
2. **GraphQL viewer detection over DOM** — login status checked by intercepting the first GraphQL response with non-null `data.viewer`. No DOM polling; no race against page render.
3. **Human-like typing** — login form keystrokes use normal-distribution delays (`mean=100ms, std=30ms`).
4. **Cookies saved before browser close** — context errors otherwise corrupt the save.
5. **`wait_until="domcontentloaded"`** — Facebook's load event never fires on infinite-scroll feeds.
6. **Lock for lazy init** — `asyncio.Lock` around `WorkerPool` lazy-init prevents `gather()`-induced double-init.
7. **5-minute rotation cooldown** — `lock_until` on rotation prevents immediately re-acquiring the same account.
8. **Stall watchdog on GraphQL silence (manual only)** — keyed off `last_response_time`. Fires whether FB silences the endpoint or returns empty bodies. Hybrid uses different stop conditions (`end_cursor` null, oldest post < `start_unix`, no-progress streak).
9. **Self-signed fbcdn URLs, short TTL** — media URLs in scraped posts carry an `oh=` HMAC signature and an `oe=` expiry (hex-encoded unix seconds); no cookies/auth needed to fetch them. Empirically the TTL is **~4-5 days from scrape time**, not 30 — verified across 12k URLs from a 2026-05 scrape batch (uniform 4d for ~91%, 5d for ~9%, across all hosts and media kinds). Expired URLs return HTTP 403 with `Bad URL hash`. Practical consequences: (a) run `download-media` within ~3 days of the scrape or pipeline it into the same run, (b) raw post JSONs older than ~5 days are media-irrecoverable without re-scraping the handle. The `time_started` in each saved file's outer dict marks the issue moment; per-URL expiry can be decoded with `int(url.split('oe=')[1].split('&')[0], 16)`.
10. **Endpoint × mode registry** — `Query.ENDPOINT_REGISTRY` is the single source of truth for endpoints, modes, and per-(endpoint, mode) param defaults. Per-mode methods on `BrowserSession` (`user_timeline_manual`, `user_timeline_hybrid`) handle dispatch via `Worker.ENDPOINT_MODE_METHODS`.
11. **Query is constructed exactly once** — `FacebookScraper.user_timeline` builds the canonical Query, validates, fills defaults. `BrowserSession` returns a `ScrapeOutcome` (Query-agnostic); `Worker` attaches the original Query via `ScrapingResult.from_outcome`. No drift between caller-spec and recorded-spec.
12. **Hybrid: `cursor=null` + `beforeTime` always set** — empirically confirmed FB's UI uses `cursor=null` whenever a date filter is active. Replays mirror that: every replay carries a non-null `beforeTime` (= `min(end_of_day(end_date), now_utc)`) so FB honors `cursor=null` on the first replay, returning the most-recent in-range batch including SSR-equivalent posts. `afterTime` stays at the captured `null` (FB's UI never sets it). See [`docs/hybrid/overview.md`](docs/hybrid/overview.md).
13. **Hybrid: post auto-extraction off** — `ResponseInterceptor.extract_posts = False` for the duration of a hybrid scrape. Prevents the natural bootstrap PCTFRQ (no date filter) from leaking off-range posts into the result. All posts come from replays with explicit filters.
14. **`__csr` / `__dyn` are HasteBitMap telemetry, not auth** — from FB's bundled JS, both are bitmaps of bootloaded resources / dynamic JS modules. FB's own request builder will conditionally `delete v.__csr` before sending. We currently live-splice the freshest values from natural GraphQL POSTs; the `freeze_tokens` experiment in TODOs validates whether splicing matters.
15. **Per-call timeouts wrap renderer-prone awaits** — every `scroll()`, `check_error_conditions()`, `page.request.post()`, etc. is wrapped in `asyncio.wait_for(operation_timeout_seconds)` so a wedged renderer can't hang a scrape forever. Per-call patch; the proper fix (external watchdog task) is in TODOs.
16. **Hybrid: cursor-reset detection + multi-leg resume** — empirically, after a variable number of paginations (median ~33 across 21 dumped scrapes, range 2–214) FB silently returns a degraded response: `has_next_page: true` and a fresh `end_cursor`, but the response shape collapses (e.g. 28 JSONL → 4 lines, ~5× smaller body) and `oldest_in_batch` jumps newer with zero post overlap vs. the prior batch. Cursor handoff is intact and there are no `errors[]` — neither existing stop fires, so the loop would run forever against a degraded stream. Detector lives in `_hybrid_pagination_loop`: if oldest_in_batch jumps newer by more than `HYBRID_CURSOR_RESET_JUMP_SECONDS` (= 7 days) vs. the previous iter, dump a 20-iter rolling window to `tmp/hybrid/cursor_reset/<handle>/<UTC_ts>/` and return `'cursor_reset'`. `oldest_in_batch` / `newest_in_batch` are extracted via `_hybrid_iter_wrapping_creation_times`, which routes through the parser's `_extract_times` (same wrapping-only metadata-strategy lookup the flattener uses — it walks the timestamp typenames in `_METADATA_TIMESTAMP_TYPENAMES`, currently `Longer` + `Minimized`), so a recent share of an older post yields only the share's own date — no false-positive triggers from `attached_story.creation_time`. `Worker.execute_task` recognizes the cursor_reset result string and locks the account for 30 min, then `FacebookScraper.user_timeline` resumes via a fresh `WorkerPool.submit_task` with `end_date` advanced backward to the oldest collected post's day, capped at `MAX_CURSOR_RESET_RESUMES` (= 5) resumes per high-level scrape. Cross-leg post-id dedup prevents boundary-day duplicates. Terminal result strings: `'cursor_reset_max_retries'`, `'cursor_reset_no_progress'` (oldest post day didn't advance past current end_date), `'cursor_reset_no_posts'` (reset on a leg that collected nothing). Diagnostic dump stays on across legs to keep observing the symptom.
17. **Post-id dedup at `ResponseInterceptor.add_posts`** — `add_posts` filters by `post_id` against `self.seen_post_ids` before appending. The auto-extract path in `intercept_response` routes through `add_posts`, so manual mode benefits identically. Without dedup, FB cursor-degraded responses (which can re-serve overlapping posts) would inflate `len(self.posts)` and let the no-progress backstop fail to fire. Posts without a `post_id` are appended as-is — defensive against parser quirks.
18. **Response-shape error is terminal, not retryable** — when the hybrid loop sees `posts_in_resp > 0` but `oldest_in_batch is None` (the parser accepted posts as Story-shaped, but every one carried a timestamp metadata-strategy typename outside `_METADATA_TIMESTAMP_TYPENAMES`), the loop returns `'response_shape_error'`. `Worker.execute_task` recognizes this result string and **preserves partial data, does not rotate, does not mark inactive, does not burn a retry slot** — the condition signals a structural bug (FB shape change / unrecognized strategy typename), not an instance-specific failure, so rotating accounts wouldn't help. `FacebookScraper.user_timeline`'s multi-leg loop terminates naturally because it only resumes on `'cursor_reset'`. The typed exception `ResponseShapeError` exists in `exceptions.py` as a signal for callers that want to pattern-match, but the loop returns a result string rather than raising so the partial-data flow stays consistent with other terminal classifications (`ETIMEDOUT`, `cursor_reset`, etc.).

For account state, lifecycle, and exception → DB-write semantics: [`docs/architecture/account_management.md`](docs/architecture/account_management.md).

For deeper hybrid-mode design rules and open questions: [`docs/hybrid/overview.md`](docs/hybrid/overview.md).

For speed/memory improvement proposals: [`docs/proposals/speed_and_memory.md`](docs/proposals/speed_and_memory.md).

For onboarding a new endpoint (paste-in checklist + wiring touch points): [`docs/adding_endpoints.md`](docs/adding_endpoints.md).

---

## File Structure

```
fbscrape/
├── __init__.py          # Package exports
├── account.py           # Account dataclass with identifier property
├── accounts_pool.py     # SQLite account management with locking
├── browser_session.py   # Browser lifecycle, login, scrape methods (manual + hybrid)
├── cli.py               # Click-based CLI
├── db.py                # Database with migration system
├── downloaders.py       # Async media downloader (images, videos, thumbnails)
├── exceptions.py        # Custom exceptions
├── logger.py            # Loguru-based logging
├── models.py            # Query, ScrapeOutcome, ScrapingResult; ENDPOINT_REGISTRY
├── response.py          # ResponseInterceptor + FacebookGraphQLParser (parse_timeline_response; layered _extract_* + ENDPOINT_FLATTENERS)
├── scraper.py           # FacebookScraper high-level API
├── utils.py             # Helpers (gather, cookies, etc.)
├── worker.py            # Worker for account lifecycle + dispatch (ENDPOINT_MODE_METHODS)
└── worker_pool.py       # WorkerPool for concurrency (Future-based)
```

---

## Usage Examples

### High-Level API (recommended)

```python
from fbscrape import FacebookScraper, gather

async with FacebookScraper(db="db/accounts.db", max_browser_sessions=2) as scraper:
    handles = ["zuck", "meta"]

    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01")  # mode="hybrid" by default
        for h in handles
    ):
        print(f"{result.query.query['handle']}: {len(result.data)} posts")
        result.save(f"output/{result.query.query['handle']}.json")
```

To force the scroll-driven path or override hybrid params:

```python
result = await scraper.user_timeline(
    "zuck", "2024-01-01", "2025-01-01",
    mode="manual",
    stall_timeout_seconds=600,  # manual-only
)

result = await scraper.user_timeline(
    "zuck", "2024-01-01", "2025-01-01",
    mode="hybrid",
    pagination_count=10,
    scroll_burst_every=5,
    max_paginations=200,
)
```

### Low-Level API (`BrowserSession`)

Direct callers must use the per-mode method (`user_timeline_manual` or `user_timeline_hybrid`) and bypass Query validation — pre-validate inputs yourself if you care:

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool

pool = AccountsPool("db/accounts.db")
account = await pool.get_available()

async with BrowserSession(account, pool, headless=True) as session:
    outcome = await session.user_timeline_hybrid("zuck", "2024-01-01", "2025-01-01")
    print(f"Scraped {len(outcome.data)} posts")

await pool.release_account(account.identifier)
```

Returns `ScrapeOutcome` (no `query` field). To get a `ScrapingResult`, build a `Query` yourself and call `ScrapingResult.from_outcome(query, outcome)`.

### CLI

Account management:
```bash
fbscrape account add --phone +1234567890 --password secret123
fbscrape account add --email user@example.com --password secret123
fbscrape account list -v
fbscrape account stats
```

Scraping:
```bash
# Defaults to --mode hybrid
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01

# Multiple handles, parallel
fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --end-date 2025-01-01 \
  --headless --max-sessions 2

# Force scroll-driven path
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01 \
  --mode manual --stall-timeout-seconds 600

# Tune hybrid pagination
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01 \
  --pagination-count 10 --scroll-burst-every 5 --max-paginations 200

# Read targets from a file (csv / parquet / yaml/yml / json / jsonl/ndjson)
fbscrape scrape user-timeline --input-file targets.csv
fbscrape scrape user-timeline --input-file handles.yaml --start-date 2024-01-01

# Search (hybrid only)
fbscrape scrape search 'mark carney' --start-date 2025-01-01 --end-date 2025-12-31
fbscrape scrape search --input-file queries.csv

# Page transparency (single-shot — pass bare `page_id` args (or `handle:page_id`
# when you have the vanity handle), or --input-file with a `page_id` column
# (handle column optional); no date range)
fbscrape scrape page-transparency 899800046546098
fbscrape scrape page-transparency habsfanhub:899800046546098
fbscrape scrape page-transparency --input-file pages.csv --headless

# Profile authenticity (single-shot — pass bare user_id args or --input-file
# with a `user_id` column; no date range, no handle needed)
fbscrape scrape profile-authenticity 100044331674441
fbscrape scrape profile-authenticity --input-file profiles.csv --headless
```

`--input-file` recognizes the columns `handle` (required), `start_date`, and `end_date`; everything else is ignored. If the file supplies `start_date` / `end_date` for any row, the matching CLI flag (`--start-date` / `--end-date`) must NOT be set. Rows missing `start_date` fall back to the `--start-date` flag; rows missing `end_date` fall back to `--end-date` and ultimately to today (UTC).

Hybrid-mode flags (all optional, fall back to registry defaults): `--pagination-count`, `--scroll-burst-{every,min,max}`, `--max-paginations` (default `-1` = no cap), `--pagination-sleep-{mean,std}`, `--template-capture-timeout`, `--post-nav-sleep-seconds`, `--request-timeout-ms`, `--max-no-progress-streak`, `--operation-timeout-seconds`. `--stall-timeout-seconds` is manual-only.

Post-processing (both `flatten` and `download-media` accept `.json` or `.json.gz`,
single file or directory of either):
```bash
# Flatten raw JSON into tabular dataset. Endpoint inferred from saved query.endpoint;
# overridable with --endpoint. Parquet uses zstd (auto-derived filenames carry the
# .parquet.zstd suffix; explicit --output filenames are honored literally). CSV
# serializes list/struct columns as JSON strings (round-trippable via json.loads).
fbscrape flatten data/posts/foo.json --format all
fbscrape flatten data/posts/foo.json.gz --format parquet
fbscrape flatten data/posts/2025-06-01_2026-02-17/ --format parquet
fbscrape flatten data/posts/foo.json --endpoint UserTimeline --format jsonl

# --output may be a file path or a folder. For directory inputs it must be a folder
# unless --concat is set (in which case all inputs are merged into one file).
# Heuristic: existing dir or trailing "/" → folder; .parquet/.csv/.jsonl suffix → file;
# otherwise → folder (created if absent).
fbscrape flatten data/posts/ --output data/flat/ --format parquet           # per-file → folder
fbscrape flatten data/posts/ --output data/merged.parquet --concat          # all → one file
fbscrape flatten data/posts/ --output data/all.parquet --concat --format all  # → all.{csv,jsonl,parquet}

# Download images/videos/thumbnails. URLs expire ~4-5 days after scrape — run within
# ~3 days or expect HTTP 403 ("Bad URL hash") on the stale subset.
fbscrape download-media data/posts/foo.json --include-thumbnails
fbscrape download-media data/posts/2025-06-01_2026-02-17/ --concurrency 12
```

---

## TODO / Future Work

- Add more scraping endpoints (GroupTimeline, EventDiscussion, etc.). Pattern documented in [`docs/adding_endpoints.md`](docs/adding_endpoints.md): one new entry in `Query.ENDPOINT_REGISTRY`, per-mode methods on `BrowserSession`, row in `Worker.ENDPOINT_MODE_METHODS`, flattener orchestrator + `ENDPOINT_FLATTENERS` row, high-level wrapper on `FacebookScraper`, CLI subcommand. Use `Search` (paginated) or `PageTransparency` (single-shot) as reference depending on response shape.
- **"Scrape until exhausted" mode.** Today `--start-date` is required and acts as the lower bound; the scrape terminates when the oldest in-batch post crosses it (or a no-progress watchdog fires). Add a way to omit `--start-date` (and the underlying `query["start_date"]`) and rely solely on the no-progress / no-new-posts watchdogs to decide when the user has run out of posts. Requires loosening `query_required` for `UserTimeline` in `Query.ENDPOINT_REGISTRY` and threading an "open lower bound" through both `user_timeline_manual` (start_datetime check at `browser_session.py:446`) and `user_timeline_hybrid`'s `start_unix` termination check (`browser_session.py:611-616`). The CLI `--end-date` is already optional (defaults to today UTC); this is the matching change for `--start-date`.
- Implement ban detection heuristics.
- Add proxy rotation support.
- **Capture profile-header metadata (`ProfileCometTimelineHeaderQuery`).** Followers, page name, intro/bio, profile pic, cover photo, verified badge live in this query, which already fires automatically on profile navigation — we intercept it and discard it. Add an extraction branch to `ResponseInterceptor.intercept_response` that detects the friendly-name and stashes the parsed payload on `BrowserSession.profile_info`; surface it as a new `profile_info` field on `ScrapingResult`. No extra HTTP needed. About-tab fields (page creation date, category, contact info) are a separate follow-up — they live in `CometProfileTabAbout*` queries that we never trigger today (would need either a dedicated `page.request.post` replay or a brief nav to `/<handle>/about`).
- **Across-session dedupe / resume on stall.** Today, watchdog saves partial results but a fresh scrape starts from the top. Keep a `seen post_ids` set so a restart skips past known posts to the previous stall frontier.
- **Cursor-reset resume cap counts productive legs the same as stuck legs** (`scraper.py`, `MAX_CURSOR_RESET_RESUMES = 5`). The current `for leg_idx in range(MAX_CURSOR_RESET_RESUMES + 1)` terminates after 5 resumes regardless of whether each leg was making real progress. A long scrape (e.g. 10 years back) that gets cursor-reset every ~1 year of pagination would terminate after covering only ~6 years even though every leg was advancing the frontier and pulling fresh posts. The existing `cursor_reset_no_progress` early-stop only catches degenerate non-advancing legs; it does NOT distinguish "making progress slowly" from "stuck forever." Fix: track `consecutive_no_progress` instead of total leg count — reset to 0 whenever a leg advances `end_date` by more than a small threshold (e.g. >1 day), cap that counter (e.g. 3). Productive legs would then continue indefinitely; only true stalls trip the cap.
- **External watchdog task for hang detection.** Today the in-loop stall watchdog can't fire if an `await` itself is stuck — both the watchdog code and the hung await live in the same task. Current mitigation (`operation_timeout_seconds`) wraps known-risky awaits in `asyncio.wait_for`, but it's a per-call patch — any *new* await we add in the loop is unprotected by default. Proper fix: run the scrape loop as a child `asyncio.Task` with a sibling watchdog task that owns the GraphQL-silence + wall-clock checks and calls `task.cancel()` when conditions fire. Cancellation breaks any pending await regardless of where it's stuck.
- **Dedicated exception for renderer hangs.** The per-call `asyncio.wait_for` sites currently encode the hang as `result='hang: ...'` strings on the returned `ScrapeOutcome`. Stringly-typed — workers can't pattern-match without parsing prefixes. Add a `RendererHangError` (or similar) in `fbscrape/exceptions.py`, raise from timed-out call sites, and let `Worker` catch it explicitly to decide rotation/cooldown policy. Pairs naturally with the external-watchdog refactor.
- **Hybrid: HTTP error classification — empirical refinement.** The current mapping (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → retry) is a working hypothesis. Needs deliberate-error-trigger tests against known-banned, known-rate-limited, and stale-token scenarios to confirm or correct.
- **Hybrid: mid-scrape session invalidation — richer detection.** HTML-body and auth-error-marker detection both raise `FailedLoginError` already. Stronger detection (e.g., `data.viewer == null` mid-scrape, marker-set expansion) is still on the roadmap.
- **Hybrid: GraphQL `errors[]` with partial data — marker set.** We drain posts before bailing, but the `_HYBRID_AUTH_ERROR_MARKERS` list is incomplete; expand as new auth-ish error strings are observed.
- **Hybrid: `freeze_tokens` experiment.** FB's bundled JS strongly suggests `__csr` / `__dyn` are HasteBitMap telemetry, not auth tokens — `RelayFBNetwork` will conditionally `delete v.__csr`. If empirically validated, drop the live-splicing path and the organic-scroll bursts whose only purpose is token refresh. Add a `freeze_tokens: bool` param to `user_timeline_hybrid` that captures the tokens once from the bootstrap template and never updates them; run a 200+ pagination scrape; if it succeeds we have evidence to simplify.
