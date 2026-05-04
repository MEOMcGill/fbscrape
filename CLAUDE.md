# Project Context

**Last Updated:** 2026-05-01

`fbscrape` is a Facebook timeline scraper built on Camoufox (stealth Firefox) with persistent SQLite-backed account rotation, support for parallel browser sessions, and two pluggable scrape strategies per endpoint.

This document describes how the codebase works *today*. For evolving design decisions, deeper rationale, and historical research, see [`docs/`](docs/).

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
- Lower bound enforced client-side: terminate when oldest post in batch < `start_unix`.
- Token splicing: `__csr` / `__dyn` from any natural GraphQL POST (organic scroll bursts every N paginations refresh them) overridden into replay bodies.
- HTTP errors mapped to typed exceptions (see *Account lifecycle* below). 5xx retried with backoff.

See [`docs/hybrid/overview.md`](docs/hybrid/overview.md) for empirical evidence behind each design rule.

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

Today: only `UserTimeline` is registered, with `manual` and `hybrid` modes. Adding a new endpoint = one nested dict entry + a `BrowserSession` method per mode + a row in `Worker.ENDPOINT_MODE_METHODS`.

`Query.__post_init__` validates the endpoint, mode, required query fields, and param keys; fills defaults from the registry. Unknown params raise `ValueError`.

### Key types (`models.py`)

- `Query(endpoint, mode, query, params)` — the scrape spec. Validated at construction. `query` carries required fields (`handle`, `start_date`, `end_date`); `params` carries mode-specific tunables.
- `ScrapeOutcome(result, posts, time_started, time_taken)` — Query-agnostic outcome from `BrowserSession`. Doesn't know which Query produced it.
- `ScrapingResult(query, result, posts, time_started, time_taken)` — final result. Composed by `Worker` via `ScrapingResult.from_outcome(query, outcome)` so the canonical Query is constructed exactly once and never rebuilt downstream.

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
- `latest_pctfrq_request` — `{post_data, headers}` of the most recent natural PCTFRQ. Hybrid polls this for its template capture.
- `network_capture` — full request+response of every observed response. **Off by default**; opt-in via `FB_NETWORK_CAPTURE_ALL=1` env var. Used for offline forensic analysis (see `tmp/hybrid/`).
- `flush()` — resets transient state between scrapes; preserves `extract_posts` (it's a behavior flag, not transient state).

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
9. **Self-signed fbcdn URLs** — media downloads need no cookies/auth, but URLs expire ~30 days post-scrape — run `download-media` soon after.
10. **Endpoint × mode registry** — `Query.ENDPOINT_REGISTRY` is the single source of truth for endpoints, modes, and per-(endpoint, mode) param defaults. Per-mode methods on `BrowserSession` (`user_timeline_manual`, `user_timeline_hybrid`) handle dispatch via `Worker.ENDPOINT_MODE_METHODS`.
11. **Query is constructed exactly once** — `FacebookScraper.user_timeline` builds the canonical Query, validates, fills defaults. `BrowserSession` returns a `ScrapeOutcome` (Query-agnostic); `Worker` attaches the original Query via `ScrapingResult.from_outcome`. No drift between caller-spec and recorded-spec.
12. **Hybrid: `cursor=null` + `beforeTime` always set** — empirically confirmed FB's UI uses `cursor=null` whenever a date filter is active. Replays mirror that: every replay carries a non-null `beforeTime` (= `min(end_of_day(end_date), now_utc)`) so FB honors `cursor=null` on the first replay, returning the most-recent in-range batch including SSR-equivalent posts. `afterTime` stays at the captured `null` (FB's UI never sets it). See [`docs/hybrid/overview.md`](docs/hybrid/overview.md).
13. **Hybrid: post auto-extraction off** — `ResponseInterceptor.extract_posts = False` for the duration of a hybrid scrape. Prevents the natural bootstrap PCTFRQ (no date filter) from leaking off-range posts into the result. All posts come from replays with explicit filters.
14. **`__csr` / `__dyn` are HasteBitMap telemetry, not auth** — from FB's bundled JS, both are bitmaps of bootloaded resources / dynamic JS modules. FB's own request builder will conditionally `delete v.__csr` before sending. We currently live-splice the freshest values from natural GraphQL POSTs; the `freeze_tokens` experiment in TODOs validates whether splicing matters.
15. **Per-call timeouts wrap renderer-prone awaits** — every `scroll()`, `check_error_conditions()`, `page.request.post()`, etc. is wrapped in `asyncio.wait_for(operation_timeout_seconds)` so a wedged renderer can't hang a scrape forever. Per-call patch; the proper fix (external watchdog task) is in TODOs.

For account state, lifecycle, and exception → DB-write semantics: [`docs/architecture/account_management.md`](docs/architecture/account_management.md).

For deeper hybrid-mode design rules and open questions: [`docs/hybrid/overview.md`](docs/hybrid/overview.md).

For speed/memory improvement proposals: [`docs/proposals/speed_and_memory.md`](docs/proposals/speed_and_memory.md).

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
├── response.py          # ResponseInterceptor + FacebookGraphQLParser (parse_timeline_response, flatten_post)
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
        print(f"{result.query.query['handle']}: {len(result.posts)} posts")
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
    print(f"Scraped {len(outcome.posts)} posts")

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
```

`--input-file` recognizes the columns `handle` (required), `start_date`, and `end_date`; everything else is ignored. If the file supplies `start_date` / `end_date` for any row, the matching CLI flag (`--start-date` / `--end-date`) must NOT be set. Rows missing `start_date` fall back to the `--start-date` flag; rows missing `end_date` fall back to `--end-date` and ultimately to today (UTC).

Hybrid-mode flags (all optional, fall back to registry defaults): `--pagination-count`, `--scroll-burst-{every,min,max}`, `--max-paginations` (default `-1` = no cap), `--pagination-sleep-{mean,std}`, `--template-capture-timeout`, `--post-nav-sleep-seconds`, `--request-timeout-ms`, `--max-no-progress-streak`, `--operation-timeout-seconds`. `--stall-timeout-seconds` is manual-only.

Post-processing:
```bash
# Flatten raw JSON into tabular dataset (csv/jsonl/parquet/all)
fbscrape flatten data/posts/foo.json --format all
fbscrape flatten data/posts/2025-06-01_2026-02-17/ --format parquet

# Download images/videos/thumbnails (run soon after scraping — URLs expire ~30 days)
fbscrape download-media data/posts/foo.json --include-thumbnails
fbscrape download-media data/posts/2025-06-01_2026-02-17/ --concurrency 12
```

---

## TODO / Future Work

- Add more scraping endpoints (Search, GroupTimeline). Pattern: one new entry in `Query.ENDPOINT_REGISTRY`, per-mode methods on `BrowserSession`, rows in `Worker.ENDPOINT_MODE_METHODS`.
- **"Scrape until exhausted" mode.** Today `--start-date` is required and acts as the lower bound; the scrape terminates when the oldest in-batch post crosses it (or a no-progress watchdog fires). Add a way to omit `--start-date` (and the underlying `query["start_date"]`) and rely solely on the no-progress / no-new-posts watchdogs to decide when the user has run out of posts. Requires loosening `query_required` for `UserTimeline` in `Query.ENDPOINT_REGISTRY` and threading an "open lower bound" through both `user_timeline_manual` (start_datetime check at `browser_session.py:446`) and `user_timeline_hybrid`'s `start_unix` termination check (`browser_session.py:611-616`). The CLI `--end-date` is already optional (defaults to today UTC); this is the matching change for `--start-date`.
- Implement ban detection heuristics.
- Add proxy rotation support.
- **Capture profile-header metadata (`ProfileCometTimelineHeaderQuery`).** Followers, page name, intro/bio, profile pic, cover photo, verified badge live in this query, which already fires automatically on profile navigation — we intercept it and discard it. Add an extraction branch to `ResponseInterceptor.intercept_response` that detects the friendly-name and stashes the parsed payload on `BrowserSession.profile_info`; surface it as a new `profile_info` field on `ScrapingResult`. No extra HTTP needed. About-tab fields (page creation date, category, contact info) are a separate follow-up — they live in `CometProfileTabAbout*` queries that we never trigger today (would need either a dedicated `page.request.post` replay or a brief nav to `/<handle>/about`).
- **Across-session dedupe / resume on stall.** Today, watchdog saves partial results but a fresh scrape starts from the top. Keep a `seen post_ids` set so a restart skips past known posts to the previous stall frontier.
- **External watchdog task for hang detection.** Today the in-loop stall watchdog can't fire if an `await` itself is stuck — both the watchdog code and the hung await live in the same task. Current mitigation (`operation_timeout_seconds`) wraps known-risky awaits in `asyncio.wait_for`, but it's a per-call patch — any *new* await we add in the loop is unprotected by default. Proper fix: run the scrape loop as a child `asyncio.Task` with a sibling watchdog task that owns the GraphQL-silence + wall-clock checks and calls `task.cancel()` when conditions fire. Cancellation breaks any pending await regardless of where it's stuck.
- **Dedicated exception for renderer hangs.** The per-call `asyncio.wait_for` sites currently encode the hang as `result='hang: ...'` strings on the returned `ScrapeOutcome`. Stringly-typed — workers can't pattern-match without parsing prefixes. Add a `RendererHangError` (or similar) in `fbscrape/exceptions.py`, raise from timed-out call sites, and let `Worker` catch it explicitly to decide rotation/cooldown policy. Pairs naturally with the external-watchdog refactor.
- **Hybrid: HTTP error classification — empirical refinement.** The current mapping (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → retry) is a working hypothesis. Needs deliberate-error-trigger tests against known-banned, known-rate-limited, and stale-token scenarios to confirm or correct.
- **Hybrid: mid-scrape session invalidation — richer detection.** HTML-body and auth-error-marker detection both raise `FailedLoginError` already. Stronger detection (e.g., `data.viewer == null` mid-scrape, marker-set expansion) is still on the roadmap.
- **Hybrid: GraphQL `errors[]` with partial data — marker set.** We drain posts before bailing, but the `_HYBRID_AUTH_ERROR_MARKERS` list is incomplete; expand as new auth-ish error strings are observed.
- **Hybrid: `freeze_tokens` experiment.** FB's bundled JS strongly suggests `__csr` / `__dyn` are HasteBitMap telemetry, not auth tokens — `RelayFBNetwork` will conditionally `delete v.__csr`. If empirically validated, drop the live-splicing path and the organic-scroll bursts whose only purpose is token refresh. Add a `freeze_tokens: bool` param to `user_timeline_hybrid` that captures the tokens once from the bootstrap template and never updates them; run a 200+ pagination scrape; if it succeeds we have evidence to simplify.
