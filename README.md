# fbscrape

A Python library for scraping Facebook using Camoufox with account pooling, rotation, and concurrent scraping.

## Features

- **Five endpoints** - `UserTimeline`, `Search`, `GroupTimeline`, `PageTransparency`, `ProfileAuthenticity`
- **High-level API** - Simple `FacebookScraper` class handles all complexity
- **Concurrent scraping** - WorkerPool manages multiple browser sessions
- **Account management** - SQLite-backed pool with automatic rotation
- **Scroll threshold rotation** - Rotate accounts after N scrolls to avoid detection
- **GraphQL interception** - Replay-based pagination via captured request templates
- **Cookie persistence** - Reuse sessions across runs
- **CLI tools** - Manage accounts and run scrapes from the command line

## Installation

```bash
pip install -e .
```

Install Playwright browsers:

```bash
playwright install
```

## Quick Start

### High-Level API (Recommended)

```python
from fbscrape import FacebookScraper, gather
import asyncio


async def main():
    async with FacebookScraper(db="accounts.db", max_browser_sessions=3) as scraper:
        result = await scraper.user_timeline(
            handle="zuck",
            start_date="2024-01-01",
            end_date="2025-01-01"
        )
        print(f"Scraped {len(result.data)} posts")


if __name__ == "__main__":
    asyncio.run(main())
```

### Concurrent Scraping

```python
from fbscrape import FacebookScraper, gather
import asyncio


async def main():
    async with FacebookScraper(db="accounts.db", max_browser_sessions=3) as scraper:
        handles = ["zuck", "meta", "facebook"]

        # Results arrive as they complete (not in submission order)
        async for result in gather(
            scraper.user_timeline(h, "2024-01-01", "2025-01-01")
            for h in handles
        ):
            handle = result.query.query["handle"]
            print(f"{handle}: {len(result.data)} posts")


if __name__ == "__main__":
    asyncio.run(main())
```

### Low-Level API (BrowserSession)

For more control, use `BrowserSession` directly. The per-endpoint methods are
`user_timeline_hybrid` / `user_timeline_manual` / `search_hybrid` /
`page_transparency_hybrid` / `profile_authenticity_hybrid`:

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool
import asyncio


async def main():
    pool = AccountsPool("accounts.db")
    account = await pool.get_available()

    async with BrowserSession(account, pool, headless=True) as session:
        outcome = await session.user_timeline_hybrid(
            handle="zuck",
            start_date="2024-01-01",
            end_date="2025-01-01"
        )
        print(f"Scraped {len(outcome.data)} posts")

    await pool.release_account(account.identifier)


if __name__ == "__main__":
    asyncio.run(main())
```

Returns `ScrapeOutcome` (no `query` field). To build a `ScrapingResult`,
construct a `Query` yourself and call `ScrapingResult.from_outcome(query, outcome)`.

## Endpoints

`fbscrape` supports five endpoints, all registered in `Query.ENDPOINT_REGISTRY`.
Two are paginated post streams; two are single-shot record fetches.

| Endpoint | Required `query` | Mode(s) | Output shape |
|---|---|---|---|
| `UserTimeline` | `handle`, `start_date`, `end_date` | `manual`, `hybrid` *(default)* | `data: list[dict]` — one element per post |
| `Search` | `query_text`, `start_date`, `end_date` | `hybrid` | `data: list[dict]` — one element per search-result post |
| `GroupTimeline` | `handle`, `start_date`, `end_date` | `hybrid` | `data: list[dict]` — one element per group post (date filter is client-side; default sort `TOP_POSTS`) |
| `PageTransparency` | `page_id` (handle optional) | `hybrid` | `data: [transparency_dict]` — single-element list |
| `ProfileAuthenticity` | `user_id` | `hybrid` | `data: [authenticity_dict]` — single-element list |

### `user_id` vs `page_id` — the distinction that bites

`ProfileAuthenticity` and `PageTransparency` look similar but expect **different
identifiers**:

- **`user_id`** (e.g. `100044331674441`, `61577505662345`) — the profile id,
  what `https://www.facebook.com/profile.php?id=…` resolves to. Used by
  `ProfileAuthenticity`. Modern accounts start with `61…`; classic accounts
  with `100…`.
- **`page_id`** (e.g. `899800046546098`, `382264105226668`) — the Page-side id,
  a separate node in FB's graph. Used by `PageTransparency`.

Passing a user_id to `PageTransparency` does **not** error — FB returns
`{"data":{"page":null}}` because `page(id: <user_id>)` resolves to no node,
and `fbscrape` surfaces this as `result="parse_error"`.

### Two-stage pipeline: user_id → page_id → transparency

When all you have is a list of user_ids and you want page-transparency data
for the subset that has a Page side, chain the two single-shot endpoints:

```python
import asyncio
from fbscrape import FacebookScraper, gather
from fbscrape.response import FacebookGraphQLParser


async def main():
    user_ids = ["100044331674441", "61577505662345", ...]
    parser = FacebookGraphQLParser()

    async with FacebookScraper(db="accounts.db", max_browser_sessions=3) as scraper:
        # Stage 1 — authenticity for every user_id.
        page_ids: list[str] = []
        async for r in gather(scraper.profile_authenticity(uid) for uid in user_ids):
            if not r.data:
                continue
            row = parser.flatten(r.data[0], endpoint="ProfileAuthenticity")
            if row and row.get("delegate_page_id"):
                page_ids.append(row["delegate_page_id"])

        # Stage 2 — transparency for the subset that has a Page side.
        async for r in gather(scraper.page_transparency(page_id=pid) for pid in page_ids):
            print(r.query.query["page_id"], r.result, len(r.data))


if __name__ == "__main__":
    asyncio.run(main())
```

The `delegate_page_id` field on the authenticity response is the bridge —
it carries the Page id for profiles that have a Page side, and is `null` for
pure personal profiles (no transparency record exists for those).

#### Starting from a post scrape

If you already have `UserTimeline` output (or `Search` / `GroupTimeline`) and
want transparency info for every account that posted, the post-level
`author_id` IS the input to `ProfileAuthenticity` (same id namespace).
**There is no post-level signal that tells you whether an account has a
linked Page** — FB ships `actors[0].__typename = "User"` on every post
regardless. So you have to call `ProfileAuthenticity` on every distinct
`author_id` and branch on `delegate_page_id`:

```python
import polars as pl
from fbscrape import FacebookScraper, gather
from fbscrape.response import FacebookGraphQLParser

posts = pl.read_parquet("posts_flattened.parquet.zstd")
user_ids = posts["author_id"].unique().to_list()
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
        ...
```

The `handle=uid` on the `page_transparency` call is purely cosmetic — it
controls the warm-up navigation URL (`/<uid>/`) so the request looks like a
real user clicking from a profile. The GraphQL body sent to FB carries
`delegate_page_id` as `variables.pageID` regardless.

### Comments on a post

Top-level comments only (v1). Each comment carries `replies_total_count` so
you can decide which comments to drill into via a future reply-fetching
endpoint:

```python
from fbscrape import FacebookScraper

async with FacebookScraper(db="accounts.db", max_browser_sessions=2) as scraper:
    result = await scraper.comments_list(
        handle="brianlilley",
        post_id="pfbid0FocuLnBJtzSwMWrdRtkAX8oLDYM9koTY7Ph8RKVTTX9wxKNL8EDshFTohjmixSo9l",
        max_results=200,  # -1 (default) = exhaust
    )
    print(f"{len(result.data)} comments scraped, result={result.result!r}")
    result.save(f"data/comments/brianlilley_top_comments.json", compress=True)
```

`post_id` accepts either the numeric form (e.g. `"1608937113934197"`) OR the
pfbid form — both resolve via FB's permalink redirect.

One edge case: pre-2010 accounts (Zuck = `"4"`, etc.) have a legacy short id
as `author_id` on their own posts, but their modern `ProfileAuthenticity.user_id`
is a different 15-digit number. The two ids refer to the same person but
aren't cross-referenceable from a post alone. Filter to modern ids
(`len(author_id) >= 10 and author_id.startswith(("100", "61"))`) if you need
to be strict.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     FacebookScraper                               │
│  user_timeline / search / page_transparency /                    │
│  profile_authenticity → ScrapingResult                            │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                       WorkerPool                                  │
│  - Creates N workers (N = min(max_sessions, active_accounts))    │
│  - Shared task queue with Future-based results                   │
│  - Workers pull tasks and execute concurrently                   │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Worker                                    │
│  - Owns ONE account from AccountsPool                            │
│  - Tracks scroll count, rotates at threshold                     │
│  - Creates fresh BrowserSession per task                         │
│  - Handles errors: login failure, ban, rate limit → rotate       │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                      BrowserSession                               │
│  - Playwright browser with Camoufox                              │
│  - Login, cookie management                                       │
│  - GraphQL response interception + replay-based pagination       │
│  - Returns ScrapeOutcome with `data: list[dict]`                 │
└──────────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Responsibility |
|-------|---------------|
| **FacebookScraper** | User-facing API, hides complexity |
| **WorkerPool** | Concurrency, task distribution via Futures |
| **Worker** | Account lifecycle, scroll tracking, error recovery |
| **BrowserSession** | Browser automation, GraphQL interception |
| **AccountsPool** | SQLite storage, locking, account selection |

## Command Line Interface

### Global Options

```bash
fbscrape --db /path/to/accounts.db <command>
fbscrape --help
```

### Scraping

```bash
# UserTimeline — paginated; --start-date / --end-date are both OPTIONAL.
# When omitted: start has no lower bound; end defaults to today (UTC) to
# mirror FB's UI fingerprint (which always sends `beforeTime`). Default
# mode is hybrid.
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --headless --max-sessions 2

# Open-ended UserTimeline scrape — pull the most recent N posts with no date filter.
fbscrape scrape user-timeline zuck --max-posts 500

# Force the scroll-driven path (deprecated; UserTimeline only)
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --mode manual

# Search — paginated, date-bounded, hybrid only (no-date URL form is TODO).
fbscrape scrape search 'mark carney' --start-date 2025-01-01 --end-date 2025-12-31
fbscrape scrape search --input-file queries.csv

# GroupTimeline — paginated, hybrid only. `handle` accepts vanity OR numeric
# group id. `--start-date` and `--end-date` are BOTH OPTIONAL (FB's UI sends
# no date filter on group feeds — when omitted there is no client-side
# bound either). Default sort is TOP_POSTS (matches FB UI; lowest-
# fingerprint and empirically safer for sustained scraping than
# CHRONOLOGICAL); under non-chronological sorts, termination is driven by
# --max-consecutive-out-of-range (default 20 = bail after N posts in a row
# outside the date window — no-op when no dates are provided), or
# --max-posts.
fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape group-timeline 787909081545196 --start-date 2024-01-01
fbscrape scrape group-timeline --input-file groups.csv --headless
# Open-ended group scrape — pull the most recent N posts.
fbscrape scrape group-timeline albertaseparatism --max-posts 500
# Override sort / stop knobs:
fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01 \
    --sorting-setting CHRONOLOGICAL --max-consecutive-out-of-range 30

# CommentsList — top-level comments on a post. Identifier is `<handle>:<post_id>`;
# post_id accepts numeric form OR the pfbid form (both work in FB's permalink URL).
# Exhaustion-only by default; pass --max-results to cap. No date filter (comments
# are returned non-chronologically by FB's "Most Relevant" ranking). Replies
# (depth>0) are NOT collected here — each comment carries `replies_total_count`
# so callers can decide which comments warrant a separate reply-fetching pass.
fbscrape scrape comments-list brianlilley:pfbid0FocuLnBJtzSwMWrdRtkAX8oLDYM9koTY7Ph8RKVTTX9wxKNL8EDshFTohjmixSo9l
fbscrape scrape comments-list zuck:10115311901107991 --max-results 200 --headless
fbscrape scrape comments-list --input-file posts.csv

# PageTransparency — single-shot, takes a page_id (handle optional)
fbscrape scrape page-transparency 899800046546098
fbscrape scrape page-transparency habsfanhub:899800046546098
fbscrape scrape page-transparency --input-file pages.csv --headless

# ProfileAuthenticity — single-shot, takes a user_id
fbscrape scrape profile-authenticity 100044331674441
fbscrape scrape profile-authenticity --input-file users.csv --headless

# Post-processing — flatten raw JSON into csv/jsonl/parquet (accepts .json or .json.gz).
# Saved scrape files are named `<handle>_<endpoint>_<mode>.json{,.gz}` —
# no date segment. The actual scrape parameters live in the file's
# `query.query` field.
fbscrape flatten data/posts/zuck_UserTimeline_hybrid.json --format all
fbscrape flatten data/posts/ --format parquet
fbscrape flatten data/posts/ --output data/merged.parquet --concat

# Download media (within ~3 days of scrape — fbcdn URLs expire ~4-5 days out)
fbscrape download-media data/posts/zuck_UserTimeline_hybrid.json --include-thumbnails

# Inspect a cURL copied from DevTools — prints a structured GraphQL summary
# (friendly_name, doc_id, decoded `variables` JSON, key headers). Cookie /
# fb_dtsg / lsd / jazoest are redacted by default; pass --raw to disable
# redaction, --full to include every header and telemetry body field.
fbscrape utils parse-curl "curl 'https://www.facebook.com/api/graphql/' -X POST ..."
```

`--input-file` accepts CSV / Parquet / YAML / JSON / JSONL. Recognized columns
depend on the subcommand: `handle` + optional `start_date` / `end_date` for
`user-timeline` and `group-timeline`; `query_text` + dates for `search`;
`handle` + `post_id` (both required) for `comments-list`; `page_id` (required) +
`handle` (optional) for `page-transparency`; `user_id` for `profile-authenticity`.

### Account Management

```bash
# Add account with email
fbscrape add --email user@example.com --password secret123

# Add account with phone
fbscrape add --phone +1234567890 --password secret123

# Add with all options
fbscrape add \
    --email user@example.com \
    --password secret123 \
    --username fbusername \
    --cookies /path/to/cookies.json \
    --proxy http://proxy:8080

# Bulk add from file
fbscrape add-from-file accounts.txt --format "email:password"

# List accounts
fbscrape list
fbscrape list --active
fbscrape list -v  # verbose

# Show account details
fbscrape info user@example.com

# Delete accounts
fbscrape delete user@example.com
fbscrape delete --inactive
fbscrape delete --all
```

### Account Status

```bash
# Activate/deactivate
fbscrape activate user@example.com
fbscrape deactivate user@example.com --error "Account banned"

# Unlock (remove rate limit locks)
fbscrape unlock user@example.com
fbscrape unlock --all

# Release (set in_use=false)
fbscrape release user@example.com
fbscrape release --all

# Reset scroll counts
fbscrape reset-scrolls user@example.com
fbscrape reset-scrolls --all
fbscrape reset-scrolls --all --endpoint UserTimeline
```

### Statistics

```bash
fbscrape stats
```

Output:
```
Account Pool Statistics
------------------------------
  Total:    10
  Active:   8
  Inactive: 2
  In Use:   3
  Locked:   1
```

### Cookie Management

```bash
fbscrape set-cookies user@example.com cookies.json
fbscrape export-cookies user@example.com output.json
```

### Field Management

```bash
# Update individual fields
fbscrape set user@example.com username myusername
fbscrape set user@example.com active true
fbscrape set user@example.com proxy_server http://proxy:8080

# List updatable fields
fbscrape fields
```

## Configuration

### FacebookScraper Options

```python
FacebookScraper(
    db="accounts.db",           # Path to SQLite database
    max_browser_sessions=5,     # Max concurrent browsers
    scroll_threshold=500,       # Scrolls before rotating account
    headless=True,              # Run browsers headlessly
    mobile=False,               # Use mobile browser emulation
)
```

### Environment Variables

```bash
FB_RAISE_WHEN_NO_ACCOUNT=false  # Raise error vs wait when no accounts
FB_LOG_LEVEL=INFO               # Logging level (TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL)
```

### Debug Logging

Enable debug logging to see detailed execution flow:

```python
from fbscrape.logger import set_log_level

set_log_level("DEBUG")
```

Or via environment variable:

```bash
FB_LOG_LEVEL=DEBUG python your_script.py
```

## Account Fields

| Field | Type | Description |
|-------|------|-------------|
| `email` | str \| None | Account email (identifier) |
| `phone_number` | str \| None | Account phone (identifier) |
| `password` | str | Account password |
| `username` | str \| None | Facebook username |
| `cookies` | list[dict] | Playwright-format cookies |
| `proxy_server` | str \| None | Proxy URL |
| `active` | bool | Whether account is usable |
| `in_use` | bool | Whether currently in use |
| `locks` | dict | Rate limit locks (`{"locked_until": "..."}`) |
| `scroll_count_overall_24h` | int | Scrolls in last 24 hours |
| `scroll_count_per_endpoint_total` | dict | Scrolls per endpoint (e.g., `{"UserTimeline": 150}`) |
| `error_msg` | str \| None | Last error message |

## Error Handling

```python
from fbscrape.exceptions import (
    # Pool-level
    NoAccountError,             # No accounts available in the pool

    # Login / account state (raised from BrowserSession, caught by Worker)
    FailedLoginError,           # Login attempt failed
    CheckpointError,            # FB redirected to /checkpoint — manual action required
    AccountDisabledError,       # FB redirected to /checkpoint/disabled — account is dead
    TransientLoginError,        # Likely playwright/page flake — account stays active
    AccountBannedError,         # HTTP 403 mid-scrape, account flagged
    RateLimitError,             # HTTP 429 mid-scrape

    # Browser / runtime
    RendererHangError,          # A page-level await exceeded operation_timeout_seconds
    RetryBudgetExhaustedError,  # Worker exhausted its 3-retry budget for a single task
)
```

### Worker policy per exception

| Exception | Action | Counts as retry? |
|---|---|---|
| `AccountDisabledError` | rotate to a new account | no |
| `CheckpointError` | rotate to a new account | yes |
| `TransientLoginError` | rotate, account stays active | yes |
| `RendererHangError` | restart task on **same** account, fresh BrowserSession | yes |
| `FailedLoginError` | mark account inactive + rotate | yes |
| `AccountBannedError` | mark account inactive + rotate | yes |
| `RateLimitError` | lock account 1h + rotate | yes |
| `NoAccountError` | re-queue task, worker exits cleanly | — |

After 3 retries on the same task, `Worker.execute_task` raises `RetryBudgetExhaustedError`. That exception surfaces in your `gather()` loop as the value of the resolved future.

### What you'll see in user code

```python
from fbscrape import FacebookScraper, gather
from fbscrape.exceptions import NoAccountError, RetryBudgetExhaustedError

async with FacebookScraper(db="db/accounts.db") as scraper:
    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01")
        for h in handles
    ):
        # result is a ScrapingResult on success, or the loop body raises:
        #   - RetryBudgetExhaustedError: this handle failed 3 times
        #   - NoAccountError: pool fully drained mid-run
        # ScrapeOutcome.result strings ("account is private", "logged out
        # while scraping", "graphql_error: ...", "error: ...") indicate
        # per-task non-rotation outcomes — the future still resolves
        # successfully with those.
        print(result.query.query["handle"], result.result, len(result.data))
```

Renderer hangs (`RendererHangError`) are caught internally and trigger a same-account restart — they do not surface to user code unless they exceed the retry budget, in which case `RetryBudgetExhaustedError` is raised instead.

## Project Structure

```
fbscrape/
├── __init__.py          # Package exports
├── scraper.py           # FacebookScraper (high-level API)
├── worker_pool.py       # WorkerPool (concurrency)
├── worker.py            # Worker (account lifecycle)
├── browser_session.py   # BrowserSession (browser automation)
├── accounts_pool.py     # AccountsPool (SQLite management)
├── account.py           # Account dataclass
├── response.py          # GraphQL response interception
├── models.py            # Query, ScrapingResult
├── cli.py               # Command-line interface
├── db.py                # Database migrations
├── utils.py             # Helpers (gather, cookies, etc.)
├── exceptions.py        # Custom exceptions
└── logger.py            # Logging setup
```

## License

MIT