# fbscrape

A Python library for scraping Facebook using Camoufox with account pooling, rotation, and concurrent scraping.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Built and maintained at the [Media Ecosystem Observatory](https://mediatechdemocracy.com/en/meo/)
for social-media research. Sibling projects: [igscrape](https://github.com/MEOMcGill/igscrape)
for Instagram and [pytok](https://github.com/networkdynamics/pytok) for TikTok.

## Features

- **Eleven endpoints** - `UserTimeline`, `Search`, `GroupTimeline`, `CommentsList`, `PostDetail`, `PageTransparency`, `ProfileAuthenticity`, `ProfileInfo`, `ProfileAbout`, `GroupInfo`, `GroupAbout`
- **High-level API** - Simple `FacebookScraper` class handles all complexity
- **Concurrent scraping** - WorkerPool manages multiple browser sessions
- **Account management** - SQLite-backed pool with automatic rotation
- **Threshold rotation** - Rotate accounts after N paginations to avoid detection
- **GraphQL interception** - Replay-based pagination via captured request templates
- **Resumable** - `--continue` picks a scrape back up from its last cursor
- **In-scrape media** - Collect media during the scrape before fbcdn URLs expire
- **Post-processing** - Flatten raw GraphQL into CSV/JSONL/Parquet and download media
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
`user_timeline_hybrid` / `search_hybrid` / `group_timeline_hybrid` /
`comments_list_hybrid` / `post_detail_hybrid` / `page_transparency_hybrid` /
`profile_authenticity_hybrid` / `profile_info_hybrid` / `profile_about_hybrid` /
`group_info_hybrid` / `group_about_hybrid`:

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

`fbscrape` supports eleven endpoints, all registered in `Query.ENDPOINT_REGISTRY`,
all running in `hybrid` mode (capture a real request template from the live
page, then replay it). Four are paginated post/comment streams; the rest are
single-shot record fetches.

| Endpoint | Required `query` | Output shape |
|---|---|---|
| [`UserTimeline`](docs/endpoints/user_timeline.md) | `handle` (dates optional) | `data: list[dict]` — one per post |
| [`Search`](docs/endpoints/search.md) | `query_text` (filters optional) | `data: list[dict]` — one per search-result post |
| [`GroupTimeline`](docs/endpoints/group_timeline.md) | `handle` (dates optional) | `data: list[dict]` — one per group post |
| [`CommentsList`](docs/endpoints/comments_list.md) | `handle`, `post_id` | `data: list[dict]` — one per top-level comment |
| [`PostDetail`](docs/endpoints/post_detail.md) | `handle`, `post_id` | `data: [record]` — one post, timeline schema |
| [`PageTransparency`](docs/endpoints/page_transparency.md) | `page_id` (handle optional) | `data: [transparency_dict]` |
| [`ProfileAuthenticity`](docs/endpoints/profile_authenticity.md) | `user_id` | `data: [authenticity_dict]` |
| [`ProfileInfo`](docs/endpoints/profile_info.md) | `handle` | `data: [profile_header_dict]` |
| [`ProfileAbout`](docs/endpoints/profile_about.md) | `handle` | `data: [profile_about_dict]` |
| [`GroupInfo`](docs/endpoints/group_info.md) | `handle` | `data: [group_header_dict]` |
| [`GroupAbout`](docs/endpoints/group_about.md) | `handle` | `data: [group_about_dict]` |

**Each endpoint has a full guide** under
[`docs/endpoints/`](docs/endpoints/README.md) — inputs, scrape strategy, every
option, usage recipes, and gotchas (including the `user_id` vs `page_id`
distinction, the `user_id → page_id → transparency` pipeline, and comments
handling).

## Output format

Each scrape is saved as a **gzipped JSONL** file — one record per line — named
`<handle>_<endpoint>_hybrid.jsonl.gz` (no date segment; the actual scrape
parameters live in each line's `query` field):

```
data/posts/zuck_UserTimeline_hybrid.jsonl.gz
```

Every line is `{query, result, time_started, time_taken, last_cursor, data: <one record>}`;
`result` / `time_taken` are `null` mid-scrape and stamped on the final line.
Records stream to disk as they're parsed (constant memory), and `--continue`
appends a new gzip member without rewriting the file. Read it back with:

```python
from fbscrape.jsonl_store import load_scrape_file
query, records = load_scrape_file("data/posts/zuck_UserTimeline_hybrid.jsonl.gz")
```

The `flatten` and `download-media` commands read both this format and legacy
whole-file envelopes (`.json` / `.json.gz`).

## Architecture

A quick note; the full walkthrough is in
[`docs/architecture/overview.md`](docs/architecture/overview.md).

```
┌──────────────────────────────────────────────────────────────────┐
│                     FacebookScraper                               │
│  user_timeline / search / group_timeline / comments_list /       │
│  post_detail / page_transparency / profile_* / group_* →         │
│  ScrapingResult                                                   │
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
# mirror FB's UI fingerprint (which always sends `beforeTime`).
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --headless --max-sessions 2

# Open-ended UserTimeline scrape — pull the most recent N posts with no date filter.
fbscrape scrape user-timeline zuck --max-posts 500

# Search — paginated, hybrid only. Filters optional; no filters = FB default ranking.
fbscrape scrape search 'mark carney'
fbscrape scrape search 'mark carney' --filter recent_posts \
    --filter creation_time.start=2025-01-01 --filter creation_time.end=2025-12-31
fbscrape scrape search 'mark carney' --filter posts_from.source=public
fbscrape scrape search --input-file queries.csv --filter recent_posts

# GroupTimeline — paginated, hybrid only. `handle` accepts vanity OR numeric
# group id. `--start-date` and `--end-date` are BOTH OPTIONAL (FB's UI sends
# no date filter on group feeds — when omitted there is no client-side
# bound either). Default sort is TOP_POSTS (matches FB UI; lowest-
# fingerprint and empirically safer for sustained scraping than
# CHRONOLOGICAL); under non-chronological sorts, termination is driven by
# --max-consecutive-out-of-range (default 20 = bail after N posts in a row
# outside the date window — no-op when no dates are provided), or
# --max-posts.
fbscrape scrape group-timeline 392585550772135 --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape group-timeline 787909081545196 --start-date 2024-01-01
fbscrape scrape group-timeline --input-file groups.csv --headless
# Open-ended group scrape — pull the most recent N posts.
fbscrape scrape group-timeline 392585550772135 --max-posts 500
# Override sort / stop knobs:
fbscrape scrape group-timeline 392585550772135 --start-date 2024-01-01 \
    --sorting-setting CHRONOLOGICAL --max-consecutive-out-of-range 30

# CommentsList — top-level comments on a post. Identifier is `<handle>:<post_id>`;
# post_id accepts numeric form OR the pfbid form (both work in FB's permalink URL).
# Exhaustion-only by default; pass --max-results to cap. No date filter (comments
# are returned non-chronologically by FB's "Most Relevant" ranking). Replies
# (depth>0) are NOT collected here — each comment carries `replies_total_count`
# so callers can decide which comments warrant a separate reply-fetching pass.
fbscrape scrape comments-list MarkJCarney2025:pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl
fbscrape scrape comments-list zuck:10115311901107991 --max-results 200 --headless
fbscrape scrape comments-list --input-file posts.csv

# PageTransparency — single-shot, takes a page_id (handle optional)
fbscrape scrape page-transparency 899800046546098
fbscrape scrape page-transparency habsfanhub:899800046546098
fbscrape scrape page-transparency --input-file pages.csv --headless

# ProfileAuthenticity — single-shot, takes a user_id
fbscrape scrape profile-authenticity 100044331674441
fbscrape scrape profile-authenticity --input-file users.csv --headless

# PostDetail — single-shot; fetch ONE post's content by its permalink.
# handle + post_id (numeric OR pfbid). Pass --group for group posts
# (/groups/<handle>/posts/<post_id>/); default is a page/user post
# (/<handle>/posts/<post_id>/). No GraphQL replay — the post's Story is read
# from the permalink's server-rendered document, then flattened to the same
# schema as a timeline post.
fbscrape scrape post-detail albertansunitedtostoptheucp:27209929835285847 --group
fbscrape scrape post-detail zuck:10115311901107991 --headless
fbscrape scrape post-detail --input-file posts.csv --group

# ProfileInfo / ProfileAbout — single-shot; a profile's header, and its About
# page (contact/basic-info/links sections). Take a vanity handle or numeric id.
fbscrape scrape profile-info zuck
fbscrape scrape profile-about 61582991935083 --headless

# GroupInfo / GroupAbout — single-shot; a group's header, and its About page
# (description, rules, activity stats, admin facepile). Vanity or numeric id.
fbscrape scrape group-info 392585550772135
fbscrape scrape group-about 392585550772135 --headless

# Post-processing — flatten raw GraphQL into csv/jsonl/parquet. Accepts the
# current `.jsonl.gz` output as well as legacy `.json` / `.json.gz` envelopes,
# a single file, or a whole directory.
fbscrape flatten data/posts/zuck_UserTimeline_hybrid.jsonl.gz --format all
fbscrape flatten data/posts/ --format parquet
fbscrape flatten data/posts/ --output data/merged.parquet --concat

# Download media (within ~3 days of scrape — fbcdn URLs expire ~4-5 days out)
fbscrape download-media data/posts/zuck_UserTimeline_hybrid.jsonl.gz --include-thumbnails

# ...or collect media DURING the scrape, so signatures are never stale.
# Immediately (pagination waits on each batch's downloads):
fbscrape scrape user-timeline zuck --download-media
# Handed off to another process (near-zero cost to the scrape):
fbscrape scrape user-timeline zuck --media-manifest data/media_queue.jsonl
fbscrape download-media data/media_queue.jsonl --from-manifest --out-dir data/media/

# Inspect a cURL copied from DevTools — prints a structured GraphQL summary
# (friendly_name, doc_id, decoded `variables` JSON, key headers). Cookie /
# fb_dtsg / lsd / jazoest are redacted by default; pass --raw to disable
# redaction, --full to include every header and telemetry body field.
fbscrape utils parse-curl "curl 'https://www.facebook.com/api/graphql/' -X POST ..."
```

`--input-file` accepts CSV / Parquet / YAML / JSON / JSONL. Recognized columns
depend on the subcommand: `handle` + optional `start_date` / `end_date` for
`user-timeline` and `group-timeline`; `query_text` for `search` (filters via `--filter`, not file columns);
`handle` + `post_id` (both required) for `comments-list` and `post-detail`;
`page_id` (required) + `handle` (optional) for `page-transparency`; `user_id`
for `profile-authenticity`.

### Media: during the scrape, or after it

fbcdn URLs are self-signed (`oh=` signature, `oe=` expiry) and go stale in about
4–5 days — after that they answer HTTP 403 `Bad URL hash`. So there are three
ways to get the pixels, and the right one depends on how much you trust the gap
between scraping and downloading:

| Path | How | Cost | Use when |
| --- | --- | --- | --- |
| Post-hoc | `fbscrape download-media <file>` | none to the scrape | you'll download within a couple of days |
| Immediate | `--download-media` during the scrape | pagination waits on each batch's fetches | the scrape is long, or the media matters more than speed |
| Handoff | `--media-manifest <path.jsonl>` during the scrape | ~nothing (one appended line per item) | you want fresh URLs *and* a fast scrape |

The two in-scrape flags work on every post-bearing endpoint — `user-timeline`,
`group-timeline`, `search`, `comments-list`, `post-detail` — and can be combined
(download now, keep the manifest as a record of what was queued):

```bash
# Immediate: media lands in <output-dir>/media/<target>/ as each batch is parsed
fbscrape scrape group-timeline 392585550772135 --download-media
fbscrape scrape group-timeline 392585550772135 --media-dir /data/media   # implies --download-media

# Handoff: append one line per media item; drain it from any other process
fbscrape scrape user-timeline zuck meta --media-manifest data/media_queue.jsonl
fbscrape download-media data/media_queue.jsonl --from-manifest --out-dir data/media/

# Both, plus thumbnails and a tighter fetch pool
fbscrape scrape search 'mark carney' --download-media --media-manifest q.jsonl \
  --include-thumbnails --media-concurrency 4
```

Manifest lines are plain JSONL (`.jsonl.gz` also works — pass a `.gz` path), one
per media item, and are safe to append to from several concurrent sessions:

```json
{"post_id": "1234", "kind": "image", "idx": 0, "url": "https://scontent...", "ext": "jpg",
 "filename": "1234_img_00.jpg", "queued_at": "2026-08-13T18:20:04+00:00",
 "endpoint": "GroupTimeline", "label": "392585550772135"}
```

`filename` is the same name the immediate path writes (`<post_id>_img_00.jpg`,
`_vid_00.mp4`, `_thumb_00.jpg`), so a drain into the same directory skips what's
already there. `queued_at` tells a consumer how much signature runway is left.
Comment attachments are named after the comment id.

From the Python API the same options are keyword arguments on every
post-bearing scraper method, plus `on_new_posts` for your own sink:

```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    async def index_batch(posts):           # sync or async, both work
        await my_queue.put(posts)           # raw (unflattened) records

    result = await scraper.group_timeline(
        "392585550772135",
        start_date="2025-01-01",
        download_media=True,                # requires media_dir
        media_dir="data/media/392585550772135",
        media_manifest="data/media_queue.jsonl",
        include_thumbnails=True,
        on_new_posts=index_batch,
    )
```

A sink that raises is logged and skipped — a CDN hiccup or a bug in your callback
can't kill the scrape. See [`docs/media_streaming.md`](docs/media_streaming.md)
for the mechanism.

### Account Management

Account commands live under the `account` group: `fbscrape account <cmd>`.

```bash
# Add account with email
fbscrape account add --email user@example.com --password secret123

# Add account with phone
fbscrape account add --phone +1234567890 --password secret123

# Add with all options
fbscrape account add \
    --email user@example.com \
    --password secret123 \
    --username fbusername \
    --cookies /path/to/cookies.json \
    --proxy http://proxy:8080

# Bulk add from file
fbscrape account add-from-file accounts.txt --format "email:password"

# List accounts
fbscrape account list
fbscrape account list --active
fbscrape account list -v  # verbose

# Show account details
fbscrape account info user@example.com

# Delete accounts
fbscrape account delete user@example.com
fbscrape account delete --inactive
fbscrape account delete --all
```

### Logging In

Accounts need a live Facebook session (cookies) before scraping. `login`
authenticates one or more accounts and persists cookies to the DB. Scrapes also
log in automatically on start when an account has stored credentials.

```bash
# Automatic form-fill login (account needs a stored password)
fbscrape login user@example.com --mode automatic

# Try stored cookies first, fall back to form-fill
fbscrape login user@example.com --mode automatic --cookies

# Manual: opens a non-headless browser and pauses for a human to log in by hand
fbscrape login user@example.com --mode manual --no-headless
```

### Account Status

```bash
# Activate/deactivate
fbscrape account activate user@example.com
fbscrape account deactivate user@example.com --error "Account banned"

# Unlock (remove rate limit locks)
fbscrape account unlock user@example.com
fbscrape account unlock --all

# Release (set in_use=false)
fbscrape account release user@example.com
fbscrape account release --all

# Reset scroll counts
fbscrape account reset-scrolls user@example.com
fbscrape account reset-scrolls --all
fbscrape account reset-scrolls --all --endpoint UserTimeline
```

### Statistics

```bash
fbscrape account stats
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
fbscrape account set-cookies user@example.com cookies.json
fbscrape account export-cookies user@example.com output.json
```

### Field Management

```bash
# Update individual fields
fbscrape account set user@example.com username myusername
fbscrape account set user@example.com active true
fbscrape account set user@example.com proxy_server http://proxy:8080

# List updatable fields
fbscrape account fields
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

Scrape calls resolve to a `ScrapingResult` — **check `.result` before trusting
`.data`**, since per-task conditions like `"rate_limit"`, `"account is
private"`, or `"parse_error"` still resolve successfully. Tasks that exhaust
their 3-retry budget or drain the pool instead **raise** in your `gather()`
loop (`RetryBudgetExhaustedError` / `NoAccountError`). Behind the scenes the
`Worker` rotates, locks, or deactivates accounts per exception type.

```python
from fbscrape import FacebookScraper, gather
from fbscrape.exceptions import NoAccountError, RetryBudgetExhaustedError

async with FacebookScraper(db="db/accounts.db") as scraper:
    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01") for h in handles
    ):
        print(result.query.query["handle"], result.result, len(result.data))
```

Full reference — every `result` string, the exception → Worker-action table,
and exactly what the `gather()` loop yields vs. raises — is in
[`docs/results_and_errors.md`](docs/results_and_errors.md).

## Project Structure

```
fbscrape/
├── __init__.py          # Package exports
├── scraper.py           # FacebookScraper (high-level API)
├── worker_pool.py       # WorkerPool (concurrency)
├── worker.py            # Worker (account lifecycle + dispatch)
├── browser_session.py   # BrowserSession (browser automation, scrape methods)
├── login.py             # Login flows (cookies → automatic → manual)
├── accounts_pool.py     # AccountsPool (SQLite management)
├── account.py           # Account dataclass
├── response.py          # GraphQL interception + FacebookGraphQLParser
├── models.py            # Query, ScrapeOutcome, ScrapingResult, ENDPOINT_REGISTRY
├── stop_conditions.py   # Pagination stop-condition framework
├── jsonl_store.py       # JSONL I/O: writer, readers, resume tail, converter
├── downloaders.py       # Async media downloader
├── cli.py               # Command-line interface
├── db.py                # Database migrations
├── utils.py             # Helpers (gather, cookies, etc.)
├── exceptions.py        # Custom exceptions
└── logger.py            # Logging setup
```

## Testing

Three-tier suite (unit / integration / e2e). `pytest` runs the fast unit tier
only; integration and e2e drive a real browser against Facebook and are gated
on an available account (e2e additionally requires `FBSCRAPE_RUN_E2E=1`). See
[`tests/README.md`](tests/README.md) for the layout and fixture capture.

```bash
pytest                              # unit tier (fast, no network)
pytest -m integration               # live headless scrapes (needs an account)
FBSCRAPE_RUN_E2E=1 pytest -m e2e     # full CLI scrape → flatten / download-media
```

## Documentation

Deeper references live under [`docs/`](docs/) (indexed in
[`docs/README.md`](docs/README.md)):

| Doc | Contents |
|---|---|
| [`docs/endpoints/`](docs/endpoints/README.md) | Per-endpoint guides — inputs, strategy, options, recipes, gotchas |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | Full architecture walkthrough |
| [`docs/architecture/account_management.md`](docs/architecture/account_management.md) | Account state machine + DB semantics |
| [`docs/results_and_errors.md`](docs/results_and_errors.md) | Result strings, exception → Worker-action, the `gather()` contract |
| [`docs/search_filters.md`](docs/search_filters.md) | Search filter usage + adding a filter |
| [`docs/media_streaming.md`](docs/media_streaming.md) | Collecting media during a scrape |
| [`docs/adding_endpoints.md`](docs/adding_endpoints.md) | Playbook for adding a new endpoint |

Contributing? See [`CONTRIBUTING.md`](CONTRIBUTING.md). Working with an AI agent
on this repo? [`CLAUDE.md`](CLAUDE.md) is the agent-facing map.

## License

MIT — see [LICENSE](LICENSE).