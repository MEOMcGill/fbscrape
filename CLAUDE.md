# Project Context

**Last Updated:** 2026-06-29

`fbscrape` is a Facebook timeline scraper built on Camoufox (stealth Firefox) with persistent SQLite-backed account rotation, parallel browser sessions, and two pluggable scrape strategies per endpoint.

**Rules:**
- Update `CLAUDE.md`, `README.md`, and relevant `docs/` files whenever the codebase changes.
- Do not make changes to the codebase unless explicitly asked.
- **Always add tests** when adding endpoints or non-trivial logic (see [`tests/README.md`](tests/README.md)):
  - **New endpoint**: fixture in `tests/_capture_fixtures.py`, `tests/unit/test_flatten_<endpoint>.py`, `tests/integration/test_<endpoint>.py`, bump `EXPECTED_KEYS` in `tests/unit/test_query_registry.py`.
  - **Unit additions**: happy path, error path, round-trip / golden-output check.
  - **Behavior changes**: extend the matching integration test; add a unit test if a new pure function was introduced.

---

## Data flow

```
caller
  → FacebookScraper.user_timeline(handle, ..., mode="hybrid")
  → WorkerPool.submit_task(query) → asyncio.Future
  → Worker.execute_task(query)     [acquires account; fresh BrowserSession per task]
  → BrowserSession.user_timeline_{mode}(...)  [returns ScrapeOutcome]
  → ScrapingResult.from_outcome(query, outcome)
  → Future resolves; caller awaits
```

---

## Endpoints

| Endpoint | Mode(s) | Required query fields | Strategy | Date filter |
|---|---|---|---|---|
| `UserTimeline` | `manual`, `hybrid` | `handle` (dates optional) | PCTFRQ | server-side `beforeTime` |
| `Search` | `hybrid` | `query_text` (filters optional) | SCRQ | URL filter blob (`creation_time`) |
| `GroupTimeline` | `hybrid` | `handle` (dates optional) | GCFRSPQ | client-side only |
| `CommentsList` | `hybrid` | `handle`, `post_id` | CLCPQ | none |
| `PageTransparency` | `hybrid` | `page_id` (handle optional) | single-shot POST | none |
| `ProfileAuthenticity` | `hybrid` | `user_id` | single-shot POST | none |

Adding a new endpoint: one dict entry in `Query.ENDPOINT_REGISTRY`, per-mode methods on `BrowserSession`, row in `Worker.ENDPOINT_MODE_METHODS`, flattener + `ENDPOINT_FLATTENERS` entry, high-level wrapper on `FacebookScraper`, CLI subcommand, plus full test additions. Full playbook: [`docs/adding_endpoints.md`](docs/adding_endpoints.md). Per-endpoint strategy deep dives: [`docs/architecture/endpoints.md`](docs/architecture/endpoints.md).

Post flattener registry:
```python
FacebookGraphQLParser.ENDPOINT_FLATTENERS = {
    "UserTimeline":        "_flatten_pctfrq_post",
    "Search":              "_flatten_pctfrq_post",
    "GroupTimeline":       "_flatten_grouptimeline_post",
    "CommentsList":        "_flatten_commentslist_comment",
    "PageTransparency":    "_flatten_pagetransparency_record",
    "ProfileAuthenticity": "_flatten_profile_authenticity_record",
}
```

---

## Key types (`models.py`)

- `Query(endpoint, mode, query, params)` — scrape spec. Validated at construction; fills defaults from registry.
- `ScrapeOutcome(result, data, time_started, time_taken, last_cursor, post_count, spill_path)` — Query-agnostic outcome from `BrowserSession`. Records are inline in `data` (single-record endpoints; manual mode) or spilled to `spill_path` `.jsonl.gz` (write-on-parse paginated scrapes).
- `ScrapingResult(query, result, data, ...)` — final result. `num_records` = `post_count` or `len(data)`; `iter_posts()` streams from spill or iterates inline. Saved as one-post-per-line JSONL (`<stem>.jsonl.gz`, KDD 24). `jsonl_store.load_scrape_file` reads both JSONL and legacy envelope formats.

---

## Account lifecycle / rotation (`worker.py` + `accounts_pool.py`)

`Worker` owns an account; creates a fresh `BrowserSession` per task.

| Exception | Action | Counts as retry? |
|---|---|---|
| `AccountDisabledError` | rotate (no retry burn) | no |
| `AutomationCheckpointError` | lock 24h + rotate (stays active) | yes |
| `CheckpointError` | rotate + retry | yes |
| `TransientLoginError` | rotate (stays active) | yes |
| `FailedLoginError` | mark inactive + rotate | yes |
| `AccountBannedError` | mark inactive + rotate | yes |
| `RateLimitError` (HTTP 429) | lock 24h + rotate | yes |
| `NoAccountError` | put task back; stop | — |

Scroll-based rotation: pre-task when `Worker.scroll_count >= scroll_threshold` (default 500), reading `BrowserSession.scrolls_recorded` (per-session integer), not the DB. 5-minute cooldown prevents immediately re-acquiring the same account.

In-body rate-limit (`errors[{code:1675004}]`) → `'rate_limit'` result, account locked 24h, partial data preserved, no retry slot burned. In-body `graphql_error` → no rotation, forensic dump to `tmp/hybrid/graphql_error/`. See [`docs/architecture/account_management.md`](docs/architecture/account_management.md).

---

## `ResponseInterceptor` state (`response.py`)

Per-`BrowserSession` page-event hook. Key fields:
- `posts`, `add_posts()` — accumulator + dedup (by `post_id`).
- `viewer_seen` — login-success marker (non-null `data.viewer` in any GraphQL response).
- `latest_csr` / `latest_dyn` — freshest tokens for hybrid replay splicing.
- `latest_pctfrq_request` / `latest_scrq_request` / `latest_gcfrspq_request` — template capture for paginated endpoints.
- `latest_natural_graphql_request` — cross-cutting auth fields for single-shot endpoints.
- `extract_posts=False` during hybrid (prevents bootstrap leak). `flush()` resets transient state.
- `FB_NETWORK_CAPTURE_ALL=1` enables full capture for forensics.

---

## On-disk format (KDD 24)

Output: `<handle>_<endpoint>_<mode>.jsonl.gz`. Each line: `{query, result, time_started, time_taken, last_cursor, data: <one record>}`. `result`/`time_taken` are `null` mid-leg and stamped on the final line. `--continue` appends a new gzip member (O(new leg), prior file never loaded). Resume reads the JSONL tail (`read_resume_tail`, last ~150 lines). Legacy whole-file envelopes auto-converted on first `--continue`. `fbscrape utils convert-to-jsonl` for bulk migration.

---

## File structure

```
fbscrape/
├── __init__.py          # Package exports
├── account.py           # Account dataclass
├── accounts_pool.py     # SQLite account management
├── browser_session.py   # Browser lifecycle, login, scrape methods (manual + hybrid)
├── cli.py               # Click-based CLI
├── db.py                # Database with migration system
├── downloaders.py       # Async media downloader
├── exceptions.py        # Custom exceptions
├── jsonl_store.py       # JSONL I/O: writer, dual-format readers, resume tail-read, converter
├── logger.py            # Loguru logging
├── models.py            # Query, ScrapeOutcome, ScrapingResult; ENDPOINT_REGISTRY
├── response.py          # ResponseInterceptor + FacebookGraphQLParser
├── scraper.py           # FacebookScraper high-level API
├── stop_conditions.py   # StopCondition framework + assemble_default_stop_conditions
├── utils.py             # Helpers (gather, cookies, etc.)
├── worker.py            # Worker: account lifecycle + dispatch (ENDPOINT_MODE_METHODS)
└── worker_pool.py       # WorkerPool: concurrency (Future-based)
```

---

## Quick reference

```python
from fbscrape import FacebookScraper, gather

async with FacebookScraper(db="db/accounts.db", max_browser_sessions=2) as scraper:
    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01") for h in handles
    ):
        result.save(f"output/{result.query.query['handle']}.jsonl.gz")
```

```bash
# Scrape
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01
fbscrape scrape search 'mark carney' --filter recent_posts --filter creation_time.start=2025-01-01 --filter creation_time.end=2025-12-31
fbscrape scrape comments-list zuck:10115311901107991 --max-results 200
fbscrape scrape page-transparency 899800046546098
fbscrape scrape profile-authenticity 100044331674441

# Post-process
fbscrape flatten data/posts/ --format parquet
fbscrape download-media data/posts/ --concurrency 12
fbscrape utils convert-to-jsonl data/posts/ --dry-run
```

Full CLI reference (all flags, `--input-file`, `--continue`, `--skip-existing`): see `README.md`.

---

## Further reading

| Doc | Contents |
|---|---|
| [`docs/architecture/endpoints.md`](docs/architecture/endpoints.md) | Per-endpoint strategy deep dives |
| [`docs/search_filters.md`](docs/search_filters.md) | Search filter dict/CLI usage + how to add a new filter |
| [`docs/design_decisions.md`](docs/design_decisions.md) | All 25 key design decisions |
| [`docs/hybrid/overview.md`](docs/hybrid/overview.md) | Hybrid mode empirical evidence |
| [`docs/architecture/account_management.md`](docs/architecture/account_management.md) | Account state machine + DB semantics |
| [`docs/adding_endpoints.md`](docs/adding_endpoints.md) | New endpoint checklist |
| [`docs/proposals/roadmap.md`](docs/proposals/roadmap.md) | TODO / future work |
| [`docs/proposals/speed_and_memory.md`](docs/proposals/speed_and_memory.md) | Performance proposals |
