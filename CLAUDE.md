# Project Context

**Last Updated:** 2026-02-11

## Summary of Recent Work

### Session Overview
Refactored the Facebook scraper library (`fbscrape`) to create a clean, well-organized codebase with proper account management, browser session handling, and CLI tooling.

---

## Architecture Changes

### AccountsPool (`accounts_pool.py`)
- **Identifier flexibility**: Accounts can now use `email` OR `phone_number` as identifier (one required, not both)
- Added `identifier` property to `Account` dataclass that returns email if available, else phone
- All methods updated to use `_identifier_condition()` helper for SQL WHERE clauses
- New methods added:
  - `update_cookies(identifier, cookies)` - Update cookies for an account
  - `update_last_used(identifier)` - Update last_used timestamp
  - `update_scroll_count(identifier, endpoint, increment)` - Track scrolls per endpoint and overall
  - `get_scroll_count(identifier, endpoint)` - Get scroll counts
  - `reset_scroll_counts(identifier, endpoint)` - Reset scroll counts

### BrowserSession (`browser_session.py`)
- **Reorganized into sections:**
  1. Initialization & Lifecycle (`__init__`, `create`, `__aenter__`, `__aexit__`, `initialize`, `close`)
  2. Authentication (`login`, `check_logged_in`, `get_cookies`, `save_cookies`)
  3. Scraping (`scrape_user_homepage`, `check_error_conditions`, `record_scroll`, `get_scroll_count`)
  4. Navigation (`goto`, `is_on_page`, `scroll_to_element`, `find_elements`)
  5. Private Helpers (all `_` prefixed methods at bottom)

- **Key implementations:**
  - `check_logged_in()` - Uses GraphQL activity detection instead of DOM elements
  - `login()` - Full login flow with human-like typing
  - `_human_type()` - Types characters with delays sampled from normal distribution (mean=100ms, std=30ms)
  - `scrape_user_homepage()` - Scrolls and intercepts GraphQL responses to collect posts
  - Cookies saved BEFORE closing browser (fixed order bug)
  - `FailedLoginError` raised on login failure

### ResponseInterceptor (`response.py`)
- Tracks `graphql_request_count` for login detection
- `has_graphql_activity()` - Check if any GraphQL requests intercepted
- Removed all `users` tracking - only returns posts now

### ScrapingResult (`models.py`)
- Removed `users` field - only tracks `posts` now
- Simplified `to_dict()` method

### Database (`db.py`)
- Migration system with `MIGRATIONS` list for easy additions
- v1: Initial schema with nullable email, CHECK constraint for email OR phone
- v2: Migration for existing databases to convert old schema

### Exceptions (`exceptions.py`)
- `FacebookScraperError` - Base exception
- `FailedLoginError` - Login failures
- `NoAccountError` - No accounts available
- `AccountBannedError` - Account banned
- `RateLimitError` - Rate limited

### CLI (`cli.py`)
- Entry point: `fbscrape` (configured in `pyproject.toml`)
- Commands:
  - `add` - Add account with --email or --phone
  - `add-from-file` - Bulk add with format string
  - `delete` - Delete accounts (--all, --inactive flags)
  - `list` - List accounts (--active, --inactive, -v)
  - `info` - Detailed account info
  - `stats` - Pool statistics
  - `activate/deactivate` - Set account status
  - `unlock/release` - Remove locks
  - `reset-scrolls` - Reset scroll counts
  - `set-cookies/export-cookies` - Cookie management

---

## Key Design Decisions

1. **Email OR Phone**: Accounts identified by either, enforced by CHECK constraint
2. **GraphQL Detection**: Login status checked by intercepting GraphQL requests, not DOM
3. **Human-like Typing**: Normal distribution delays between keystrokes
4. **No Users Tracking**: ScrapingResult only returns posts
5. **Private Methods**: All helper methods prefixed with `_` and placed at bottom
6. **Cookies First**: Save cookies before closing browser to avoid context errors
7. **wait_until="domcontentloaded"**: Prevents hangs on Facebook's infinite loading

---

## File Structure

```
fbscrape/
├── __init__.py
├── account.py           # Account dataclass with identifier property
├── accounts_pool.py     # SQLite account management
├── browser_session.py   # Browser lifecycle, login, scraping
├── cli.py               # Click-based CLI
├── db.py                # Database with migration system
├── exceptions.py        # Custom exceptions
├── logger.py            # Logging setup
├── models.py            # Query, ScrapingResult
├── response.py          # GraphQL interception and parsing
├── scraper.py           # High-level API (placeholder)
├── session.py           # Legacy FacebookAuth
├── utils.py             # Helpers (cookies, gather, etc.)
├── worker.py            # Worker for concurrent scraping
└── worker_pool.py       # Worker pool management
```

---

## Usage Examples

### Python API

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool

pool = AccountsPool("db/accounts.db")
account = await pool.get_for_queue("user_page")

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
- Implement WorkerPool for concurrent scraping with account rotation
- Add scraping commands to CLI
- Implement scroll threshold for automatic account rotation
- Add ban detection and recovery
