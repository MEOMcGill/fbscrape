# Project Context

**Last Updated:** 2026-02-13

## Summary of Recent Work

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
├── response.py          # GraphQL interception and parsing
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

```bash
fbscrape add --phone +1234567890 --password secret123
fbscrape add --email user@example.com --password secret123
fbscrape list -v
fbscrape stats
```

---

## TODO / Future Work

- Add more scraping endpoints (Search, GroupTimeline)
- Add scraping commands to CLI
- Implement ban detection heuristics
- Add proxy rotation support
