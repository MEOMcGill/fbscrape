# fbscrape

A Python library for scraping Facebook user homepages using Playwright and Camoufox with account pooling and rotation.

## Features

- Async-first design with GraphQL response interception
- Account pool management with SQLite storage
- Automatic account rotation based on scroll thresholds
- Cookie persistence for session reuse
- CLI for account management
- Support for proxies and fingerprint customization

## Installation

```bash
pip install -e .
```

Install Playwright browsers:

```bash
playwright install
```

## Quick Start

### Python API

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool
import asyncio

async def main():
    # Initialize account pool
    pool = AccountsPool("accounts.db")

    # Get an available account
    account = await pool.get_for_queue("user_page")

    # Create browser session
    async with BrowserSession(account, pool, headless=True) as session:
        # Check if logged in
        if await session.check_logged_in():
            print(f"Logged in as {account.identifier}")

            # Scrape a user's homepage
            result = await session.scrape_user_homepage(
                handle="zuck",
                start_date="2024-01-01",
                end_date="2025-01-01"
            )

            print(f"Scraped {len(result.posts)} posts")

    # Release the account
    await pool.release_account(account.identifier)

if __name__ == "__main__":
    asyncio.run(main())
```

### Concurrent Scraping

```python
from fbscrape.utils import gather

async def scrape_multiple():
    pool = AccountsPool("accounts.db")
    handles = ["handle1", "handle2", "handle3"]

    # Get accounts for each handle
    sessions = []
    for handle in handles:
        account = await pool.get_for_queue("user_page")
        session = BrowserSession(account, pool, headless=True)
        await session.initialize()
        sessions.append((session, handle))

    # Scrape concurrently and yield results as they complete
    async for result in gather(
        session.scrape_user_homepage(handle, "2024-01-01", "2025-01-01")
        for session, handle in sessions
    ):
        print(f"Completed: {result.query.query} - {len(result.posts)} posts")
        # Save result immediately, don't accumulate in memory
        save_to_db(result)

    # Cleanup
    for session, _ in sessions:
        await session.close()
```

## Command Line Interface

The `fbscrape` CLI provides account management functionality.

### Global Options

```bash
fbscrape --db /path/to/accounts.db <command>  # Use custom database path
fbscrape --help                                # Show help
```

### Account Management

#### Add Accounts

```bash
# Add single account with email
fbscrape add --email user@example.com --password secret123

# Add single account with phone number
fbscrape add --phone +1234567890 --password secret123

# Add with all options
fbscrape add \
    --email user@example.com \
    --password secret123 \
    --username fbusername \
    --email-password emailpass \
    --proxy http://proxy:8080 \
    --proxy-user proxyuser \
    --proxy-pass proxypass \
    --cookies /path/to/cookies.json \
    --os macos

# Bulk add from file
fbscrape add-from-file accounts.txt --format "email:password"
fbscrape add-from-file accounts.txt --format "email:password:email_password"
fbscrape add-from-file accounts.txt --format "phone:password"
```

**File format** (one account per line, `#` for comments):
```
user1@example.com:password123
user2@example.com:password456:emailpass789
# This is a comment
user3@example.com:password789
```

#### List Accounts

```bash
# List all accounts
fbscrape list

# List only active accounts
fbscrape list --active

# List only inactive accounts
fbscrape list --inactive

# Verbose output with all fields
fbscrape list -v
```

#### Account Details

```bash
# Show detailed info for an account
fbscrape info user@example.com
```

#### Delete Accounts

```bash
# Delete specific accounts
fbscrape delete user@example.com user2@example.com

# Delete all inactive accounts (with confirmation)
fbscrape delete --inactive

# Delete all accounts (with confirmation)
fbscrape delete --all
```

### Account Status

#### Activate/Deactivate

```bash
# Mark accounts as active
fbscrape activate user@example.com
fbscrape activate --all

# Mark accounts as inactive
fbscrape deactivate user@example.com --error "Account banned"
fbscrape deactivate --all
```

#### Unlock/Release

```bash
# Remove all locks from accounts
fbscrape unlock user@example.com
fbscrape unlock --all

# Release accounts from use (set in_use=false)
fbscrape release user@example.com
fbscrape release --all --queue user_page
```

### Statistics

```bash
# Show pool statistics
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
  Locked (user_page): 2
  Locked (general): 1
```

### Scroll Management

```bash
# Reset scroll counts for specific accounts
fbscrape reset-scrolls user@example.com

# Reset scroll counts for all accounts
fbscrape reset-scrolls --all

# Reset only specific endpoint
fbscrape reset-scrolls --all --endpoint user_page
```

### Cookie Management

```bash
# Import cookies from file
fbscrape set-cookies user@example.com cookies.json

# Export cookies to file
fbscrape export-cookies user@example.com output.json
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    FacebookScraper (API)                    │
│  - scrape_user_homepage(handle, start_date, end_date)       │
│  - close()                                                  │
└────────────────────┬────────────────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
┌────────▼─────────┐    ┌───────▼──────────┐
│   WorkerPool     │    │  AccountsPool    │
│  - N browsers    │◄───┤  - SQLite DB     │
│  - Task queue    │    │  - Lock mgmt     │
│  - Account       │    │  - Stats         │
│    rotation      │    └──────────────────┘
└────────┬─────────┘
         │
┌────────▼───────────────────────┐
│       BrowserSession           │
│  - Playwright/Camoufox context │
│  - ResponseInterceptor         │
│  - Account & cookies           │
│  - scroll tracking             │
└────────────────────────────────┘
```

### Key Components

| Component | File | Description |
|-----------|------|-------------|
| `AccountsPool` | `accounts_pool.py` | Manages accounts in SQLite with locking, rotation |
| `Account` | `account.py` | Account dataclass with email/phone identifier |
| `BrowserSession` | `browser_session.py` | Browser lifecycle, login, scraping methods |
| `ResponseInterceptor` | `response.py` | Intercepts GraphQL responses for data extraction |
| `FacebookScraper` | `scraper.py` | High-level API (delegates to WorkerPool) |

## Configuration

### Environment Variables

```bash
FB_RAISE_WHEN_NO_ACCOUNT=false  # Raise error when no accounts available
FB_LOG_LEVEL=INFO               # Logging level
```

### Account Fields

| Field | Type | Description |
|-------|------|-------------|
| `email` | str \| None | Account email (identifier) |
| `phone_number` | str \| None | Account phone (identifier) |
| `password` | str | Account password |
| `username` | str \| None | Facebook username |
| `email_password` | str \| None | Email account password (for 2FA) |
| `cookies` | list[dict] | Playwright-format cookies |
| `proxy_server` | str \| None | Proxy URL |
| `proxy_username` | str \| None | Proxy auth username |
| `proxy_password` | str \| None | Proxy auth password |
| `os` | str | OS fingerprint (macos/windows/linux) |
| `active` | bool | Whether account is usable |
| `in_use` | bool | Whether account is currently in use |
| `scroll_count_overall_24h` | int | Scrolls in last 24 hours |
| `scroll_count_per_endpoint_total` | dict | Scrolls per endpoint |
| `last_used` | datetime | Last usage timestamp |
| `error_msg` | str \| None | Last error message |

## Database

Accounts are stored in SQLite. The database is automatically created and migrated.

Default location: `~/.fbscrape/db/accounts.db`

### Manual Database Access

```python
from fbscrape.accounts_pool import AccountsPool

pool = AccountsPool("accounts.db")

# Get all accounts
accounts = await pool.get(None)

# Get specific account
account = await pool.get("user@example.com")

# Get available account for scraping
account = await pool.get_for_queue("user_page")

# Save modified account
await pool.save(account)
```

## Error Handling

The library defines custom exceptions in `fbscrape/exceptions.py`:

```python
from fbscrape.exceptions import (
    NoAccountError,      # No accounts available
    AccountBannedError,  # Account was banned
    LoginFailedError,    # Login attempt failed
    RateLimitError,      # Hit rate limit
)
```

## Development

### Running Tests

```bash
python -m pytest examples/
```

### Project Structure

```
fbscrape/
├── __init__.py          # Package exports
├── cli.py               # Command-line interface
├── account.py           # Account dataclass
├── accounts_pool.py     # Account pool management
├── browser_session.py   # Browser lifecycle & scraping
├── response.py          # GraphQL response interception
├── scraper.py           # High-level API
├── models.py            # ScrapingResult, Query, etc.
├── db.py                # Database utilities & migrations
├── utils.py             # Helpers (gather, cookies, etc.)
├── logger.py            # Logging setup
├── exceptions.py        # Custom exceptions
├── session.py           # Facebook auth (legacy)
├── worker.py            # Worker for concurrent scraping
└── worker_pool.py       # Worker pool management
```

## License

MIT