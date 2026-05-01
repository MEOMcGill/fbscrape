# Project Context

**Last Updated:** 2026-04-19

## Summary of Recent Work

### Session 3 (2026-04-19): Stall Watchdog, CLI Scrape/Flatten/Download, Richer Post Extraction

Added production-quality scrape robustness + end-to-end CLI workflow (scrape → flatten → download media).

#### Stall Watchdog (GraphQL silence detection)

When Facebook throttles a long scraping session, the GraphQL endpoint goes *silent* — the browser keeps issuing pagination requests but responses never come back. The scroll loop kept running against a frozen feed (FB renders skeleton placeholders) because `scrollBy` is fire-and-forget and `no_new_posts_count > 10` is scroll-count based, not wall-clock based.

**Fix:** added `ResponseInterceptor.last_response_time` (set in `intercept_response()` each time any GraphQL XHR lands, reset in `flush()`). Before each `self.scroll()` in `BrowserSession.user_timeline`, the loop checks wall-clock time since the last GraphQL response. If it exceeds `stall_timeout_seconds`, the scrape returns `ScrapingResult(result='stalled: no graphql response for Ns', posts=<partial>)` and exits cleanly — no data loss.

`stall_timeout_seconds` (default **300s / 5 min**) is threaded through `BrowserSession` → `Worker` → `WorkerPool` → `FacebookScraper` and exposed as `--stall-timeout-seconds` on the CLI.

#### CLI Additions

- **`fbscrape scrape user-timeline <handles...> --start-date --end-date`** — launches scrapes from the shell. Supports multiple handles in parallel (via `gather`), `--max-sessions`, `--scroll-threshold`, `--stall-timeout-seconds`, `--headless`, `--mobile`, `--log-level`, `--output-dir`.
- **`fbscrape flatten <input.json|dir> [--format csv|jsonl|parquet|all] [--output path]`** — flattens scraped post JSON(s) into tabular datasets. Directory mode writes one `_flat.<ext>` per input. Requires `pandas` + `pyarrow` for parquet.
- **`fbscrape download-media <input.json|dir> [--include-thumbnails] [--concurrency N]`** — async downloader for images / videos / (optional) video thumbnails. Output path defaults to `<input_dir>/media/<handle>/`. Skip-existing on by default. Uses `aiohttp` + `asyncio.Semaphore` — no cookies (fbcdn URLs are self-signed, ~30-day expiry).

#### Richer Post Extraction (`FacebookGraphQLParser.flatten_post`)

Found a bug: `reactions` / `shares` / `top_reactions` were looked up inside each `adaptive_ufi_action_renderers[i].feedback`, but they actually live on the parent `comet_ufi_summary_and_actions_renderer.feedback`. Earlier flattened CSVs had None/empty for all engagement columns — now fixed.

**All 7 reaction types** (Like, Love, Haha, Wow, Sad, Angry, Care) are returned per post. Exploded into individual integer columns (`like`, `love`, `haha`, `wow`, `sad`, `angry`, `care`) that sum to `reactions`. `top_reactions` JSON list removed.

**New columns added:** `permalink_url`, `privacy`, `is_reel`, `is_live`, `video_duration_sec`, `video_views`, `external_urls` (links in post body), `comments` (total count), `top_comments` (list of `{text, author_id, author_name, author_url, created_at, reactions}`), `shared_post_{id,url,created_at,author_id,author_name,text}` (for reshares — from `node.attached_story`).

Note: `privacy_scope` lives in metadata index 1, not 0 (creation_time is at 0) — flatten_post now scans all metadata entries.

#### New Files / Modules

- `fbscrape/downloaders.py` — `extract_media_from_post(post, include_thumbnails)` walks raw GraphQL to find `photo_image.uri`, `all_subattachments[].media.image.uri`, progressive video `.mp4` URLs, and `preferred_thumbnail.image.uri`. `download_media_from_posts(...)` orchestrates the async fetch.

#### Expanded Debug Logging (scroll loop)

Per-iteration `logger.debug` in `user_timeline`'s `while True` loop now shows: before/after `get_posts()` durations, before/after `check_error_conditions()` durations, before/after `scroll()` durations, sleep duration, total iter duration, and — critically — `last_response=HH:MM:SS (Xs ago, threshold=Ns)` so you can see stall onset in real time and calibrate the threshold.

---

### Session 2 (2026-02-13): WorkerPool Implementation & Bug Fixes

Implemented the WorkerPool and FacebookScraper orchestration layer, fixed critical concurrency bugs, and added comprehensive debug logging.

#### Key Implementations

**WorkerPool (`worker_pool.py`)**
- Producer-Consumer pattern with shared `asyncio.Queue`
- Lazy initialization on first `submit_task()` call
- Future-based results - each task gets a Future that resolves when complete
- Worker count formula: `min(max_workers, active_accounts)`
- Graceful shutdown with `_shutdown` flag

**FacebookScraper (`scraper.py`)**
- High-level API that hides WorkerPool complexity
- `user_timeline(handle, start_date, end_date)` method
- Async context manager support (`async with`)
- **Race condition fix**: Added `asyncio.Lock` to `_ensure_initialized()` to prevent multiple WorkerPools being created when using `gather()`

**Worker (`worker.py`)**
- Manages account lifecycle (acquire, use, rotate, release)
- Creates fresh `BrowserSession` per task via context manager
- Tracks scroll count, rotates at threshold
- Error handling: FailedLogin → mark inactive, rotate; RateLimit → lock 1hr, rotate
- Account rotation cooldown: 5-minute lock to prevent re-acquiring same account

#### Bug Fixes

1. **Race Condition in Lazy Initialization**
   - Problem: `gather()` launches coroutines concurrently, all saw `worker_pool is None` before any completed
   - Result: Multiple WorkerPools created, spawning too many workers
   - Fix: Added `asyncio.Lock` around initialization check

2. **Queue System Simplification**
   - Removed per-queue locking complexity
   - Changed `get_for_queue()` to `get_available()` with simple `in_use` flag
   - Queue now only tracks scroll counts per endpoint

3. **Account Rotation Cooldown**
   - Problem: Same account could be reacquired immediately after rotation
   - Fix: 5-minute `lock_until` before releasing account

#### Performance Optimizations

**`check_error_conditions()` Optimization**
- Before: 7 sequential DOM queries on every scroll (~70-350ms × 100 scrolls)
- After:
  1. Single combined locator for fast-path (no errors = 1 query)
  2. Only check after navigation and when stalled (no_new_posts_count == 3)
  3. Detailed checks only if error indicator found

#### Debug Logging

Added `logger.debug()` throughout the library for testing:
- `scraper.py` - WorkerPool initialization, task submission/completion
- `worker_pool.py` - Initialize config, task queue size, worker shutdown
- `worker.py` - Account acquisition, task execution, scroll counts, rotation
- `browser_session.py` - Context manager, browser init, login, navigation, scrolling
- `accounts_pool.py` - get_available, release, lock/unlock, scroll counts

Enable with: `set_log_level("DEBUG")` or `FB_LOG_LEVEL=DEBUG`

#### Removed Files

- `session.py` - Legacy `FacebookAuth` class (unused)

---

### Session 1 (2026-02-11): Initial Refactoring

Refactored the Facebook scraper library (`fbscrape`) to create a clean, well-organized codebase with proper account management, browser session handling, and CLI tooling.

#### Architecture Changes

**AccountsPool (`accounts_pool.py`)**
- **Identifier flexibility**: Accounts can now use `email` OR `phone_number` as identifier (one required, not both)
- Added `identifier` property to `Account` dataclass that returns email if available, else phone
- All methods updated to use `_identifier_condition()` helper for SQL WHERE clauses
- New methods: `update_cookies`, `update_last_used`, `update_scroll_count`, `get_scroll_count`, `reset_scroll_counts`

**BrowserSession (`browser_session.py`)**
- Reorganized into sections: Initialization, Authentication, Scraping, Navigation, Private Helpers
- `check_logged_in()` - Uses GraphQL activity detection instead of DOM elements
- `login()` - Full login flow with human-like typing (normal distribution delays)
- Cookies saved BEFORE closing browser (fixed order bug)

**ResponseInterceptor (`response.py`)**
- Tracks `graphql_request_count` for login detection
- `has_graphql_activity()` - Check if any GraphQL requests intercepted
- Removed all `users` tracking - only returns posts now

**Models (`models.py`)**
- Added `to_json()` and `save()` methods to Query and ScrapingResult
- Endpoint validation with `ENDPOINT_REQUIRED_FIELDS`

**CLI (`cli.py`)**
- Entry point: `fbscrape`
- Commands: add, add-from-file, delete, list, info, stats, activate/deactivate, unlock/release, reset-scrolls, set-cookies/export-cookies

---

## Key Design Decisions

1. **Email OR Phone**: Accounts identified by either, enforced by CHECK constraint
2. **GraphQL Detection**: Login status checked by intercepting GraphQL requests, not DOM
3. **Human-like Typing**: Normal distribution delays between keystrokes (mean=100ms, std=30ms)
4. **No Users Tracking**: ScrapingResult only returns posts
5. **Private Methods**: All helper methods prefixed with `_` and placed at bottom
6. **Cookies First**: Save cookies before closing browser to avoid context errors
7. **wait_until="domcontentloaded"**: Prevents hangs on Facebook's infinite loading
8. **Lock for Lazy Init**: Prevents race conditions when using `gather()`
9. **Rotation Cooldown**: 5-minute lock prevents same account being re-acquired
10. **Stall Watchdog on GraphQL silence, not post growth**: keyed off `ResponseInterceptor.last_response_time` so it fires whether FB silences the endpoint, returns empty responses, or something else goes wrong downstream. Scroll-count stall check (`no_new_posts_count > 10`) stays — it still correctly signals "end of feed." (Manual mode only — hybrid uses different stop conditions.)
11. **Self-signed fbcdn URLs**: media download needs no cookies/auth. But URLs expire ~30 days post-scrape, so `download-media` should run soon after scraping.
12. **Endpoint × mode registry**: `Query.ENDPOINT_REGISTRY` is the single source of truth for which endpoints exist (`UserTimeline`, future `GroupTimeline`, …), which modes each supports (`manual`, `hybrid`, future `api`), and the param defaults per (endpoint, mode). `Query.__post_init__` validates the endpoint, mode, required query fields, and rejects unknown params. Adding a new mode = one nested entry; per-mode methods on `BrowserSession` (`user_timeline_manual`, `user_timeline_hybrid`) handle dispatch via `Worker.ENDPOINT_MODE_METHODS`.
13. **Hybrid mode — `cursor=null` first replay + `beforeTime` always set**: empirically confirmed FB's UI uses `cursor=null` whenever a date filter is active. Hybrid replays mirror that: every replay carries a non-null `beforeTime` (= `min(end_of_day(end_date), now_utc)`) so FB honors `cursor=null` on the first replay, returning the most-recent in-range batch including SSR-equivalent posts. `afterTime` is left at the captured `null` (FB's UI never sets it; overriding would be a fingerprint). Lower bound is enforced client-side. See [`docs/hybrid/overview.md`](docs/hybrid/overview.md) for the empirical evidence.
14. **Hybrid mode — auto-extraction off**: `ResponseInterceptor.extract_posts = False` for the duration of a hybrid scrape. Prevents the natural bootstrap-scroll PCTFRQ (which has no date filter) from leaking off-range posts into the result. All posts come from replays whose bodies carry the explicit filters. Token tracking, viewer detection, and the narrower `latest_pctfrq_request` template hook all keep working.
15. **`__csr` / `__dyn` are HasteBitMap telemetry, not auth**: from FB's bundled JS, `__csr` is a bitmap of bootloaded resource IDs and `__dyn` is the bitmap of dynamic JS modules. FB's own request builder will conditionally `delete v.__csr` before sending. Currently we live-splice `latest_csr` / `latest_dyn` from any natural GraphQL POST observed by the interceptor; pending the `freeze_tokens` experiment to confirm whether splicing is even necessary.

For account state, lifecycle, and exception → DB-write semantics, see [`docs/architecture/account_management.md`](docs/architecture/account_management.md).

For ideas to improve scrape speed and reduce memory footprint (date/year filters, GraphQL cursor replay, DOM cleanup, etc.), see [`docs/proposals/speed_and_memory.md`](docs/proposals/speed_and_memory.md).

For hybrid-mode design rules + open questions, see [`docs/hybrid/overview.md`](docs/hybrid/overview.md). Optional debug capture is gated behind `FB_NETWORK_CAPTURE_ALL=1` and dumps every response body for offline forensic analysis; off by default.

---

## File Structure

```
fbscrape/
├── __init__.py          # Package exports
├── account.py           # Account dataclass with identifier property
├── accounts_pool.py     # SQLite account management with locking
├── browser_session.py   # Browser lifecycle, login, scraping
├── cli.py               # Click-based CLI
├── db.py                # Database with migration system
├── exceptions.py        # Custom exceptions
├── logger.py            # Loguru-based logging
├── models.py            # Query, ScrapingResult with JSON serialization
├── downloaders.py       # Async media downloader (images, videos, thumbnails)
├── response.py          # GraphQL interception, parsing, flatten_post
├── scraper.py           # FacebookScraper high-level API
├── utils.py             # Helpers (gather, cookies, etc.)
├── worker.py            # Worker for account lifecycle
└── worker_pool.py       # WorkerPool for concurrency
```

---

## Usage Examples

### High-Level API (Recommended)

```python
from fbscrape import FacebookScraper, gather

async with FacebookScraper(db="db/accounts.db", max_browser_sessions=2) as scraper:
    handles = ["zuck", "meta"]

    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01")
        for h in handles
    ):
        print(f"{result.query.query['handle']}: {len(result.posts)} posts")
        result.save(f"output/{result.query.query['handle']}.json")
```

### Low-Level API (BrowserSession)

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool

pool = AccountsPool("db/accounts.db")
account = await pool.get_available()

async with BrowserSession(account, pool, headless=True) as session:
    result = await session.user_timeline("zuck", "2024-01-01", "2025-01-01")
    print(f"Scraped {len(result.posts)} posts")

await pool.release_account(account.identifier)
```

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
# Single or multiple handles, between two dates
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --end-date 2025-01-01 \
  --headless --max-sessions 2 --stall-timeout-seconds 300
```

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

- Add more scraping endpoints (Search, GroupTimeline)
- Implement ban detection heuristics
- Add proxy rotation support
- **Capture profile-header metadata (`ProfileCometTimelineHeaderQuery`).** Followers, page name, intro/bio, profile pic, cover photo, verified badge live in this query, which already fires automatically on profile navigation — we intercept it and discard it. Add an extraction branch to `ResponseInterceptor.intercept_response` that detects the friendly-name and stashes the parsed payload on `BrowserSession.profile_info`; surface it as a new `profile_info` field on `ScrapingResult`. No extra HTTP needed. About-tab fields (page creation date, category, contact info) are a separate follow-up — they live in `CometProfileTabAbout*` queries that we never trigger today (would need either a dedicated `page.request.post` replay or a brief nav to `/<handle>/about`).
- Across-session dedupe / resume on stall: today, watchdog saves partial results but a fresh scrape starts from the top. Keep a `seen post_ids` set so a restart skips past known posts to the previous stall frontier.
- **External watchdog task for hang detection.** Today the in-loop stall watchdog can't fire if an `await` itself is stuck — both the watchdog code and the hung await live in the same task. Current mitigation (`OPERATION_TIMEOUT_SECONDS = 30` in `BrowserSession`) wraps each known-risky await (`scroll()`, `check_error_conditions()`) in `asyncio.wait_for`, but it's a per-call patch — any *new* await we add in the loop is unprotected by default. Proper fix: run `user_timeline`'s loop as a child `asyncio.Task` with a sibling watchdog task that owns the GraphQL-silence + wall-clock checks and calls `task.cancel()` when conditions fire. Cancellation breaks any pending await regardless of where it's stuck. Convert the per-call timeouts into a single watchdog at the task boundary.
- **Dedicated exception for renderer hangs.** The per-call `asyncio.wait_for` sites currently encode the hang as a `result='hang: ...'` string on the returned `ScrapingResult`. Stringly-typed — workers can't pattern-match on it without parsing prefixes. Add a `RendererHangError` (or similar) in `fbscrape/exceptions.py`, raise it from the timed-out call sites, and let `Worker` catch it explicitly to decide rotation/cooldown policy (e.g., this account's browser is wedged → close + rotate, distinct from a logged-out or rate-limited account). Pairs naturally with the external-watchdog refactor above: the watchdog raises this exception when it cancels the scrape task.
- **Hybrid: HTTP error classification.** `_hybrid_pagination_loop` returns `'pagination_error: HTTP {status}'` for any non-200 response, collapsing 401/403/429/5xx into one outcome. `Worker.execute_task` doesn't pattern-match on result strings — so today these errors don't trigger account rotation, locking, or retry. Map status → typed exception (401 → relogin path, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → `TransientHTTPError`) so Worker's existing handlers activate.
- **Hybrid: mid-scrape session invalidation.** If FB invalidates the session mid-scrape, `page.request.post()` returns 200 with an HTML/auth-error JSON. Today we treat all 200s as success. Add a positive shape check (response body parses as GraphQL JSON with `data` or `extensions`) and surface the failure as a logged-out signal so Worker rotates accounts.
- **Hybrid: GraphQL `errors[]` with partial data.** FB sometimes returns 200 with `errors[]` populated AND posts in the same response. Currently any GraphQL error aborts; should drain posts first, then bail with the typed error.
- **Hybrid: `freeze_tokens` experiment.** Investigation of FB's JS bundles strongly suggests `__csr` / `__dyn` are HasteBitMap-of-loaded-resources (telemetry / diagnostics), not auth tokens — FB's own request builder conditionally `delete v.__csr`. If empirically validated, drop the live-splicing path and the organic-scroll bursts whose only purpose is token refresh. Add a `freeze_tokens: bool` param to `user_timeline_hybrid` that captures `__csr` / `__dyn` once from the bootstrap template and never updates them; run a 200+ pagination scrape; if it succeeds we have evidence to simplify.
