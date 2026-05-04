# Contributing to fbscrape

Thank you for your interest in contributing to fbscrape. This document provides guidelines and instructions for contributing.

## Getting Started

### Prerequisites

- Python 3.10+
- pip or uv package manager
- Git

### Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/MEOMcGill/dt-facebook-scraper.git
   cd fbscrape
   ```

2. Install the package in editable mode:
   ```bash
   pip install -e .
   ```

3. Install Playwright browsers:
   ```bash
   playwright install
   ```

4. Verify the installation:
   ```bash
   fbscrape --help
   ```

## How to Contribute

### Reporting Issues

Before submitting an issue:

1. Search existing issues to avoid duplicates
2. Use a clear, descriptive title
3. Include:
   - Python version (`python --version`)
   - Operating system
   - Steps to reproduce the issue
   - Expected vs actual behavior
   - Relevant logs (use `FB_LOG_LEVEL=DEBUG` for detailed output)

### Submitting Pull Requests

1. Fork the repository
2. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
3. Make your changes following the code style guidelines below
4. Test your changes thoroughly
5. Commit with clear, descriptive messages
6. Push to your fork and open a pull request

#### Pull Request Guidelines

- Keep PRs focused on a single feature or fix
- Update documentation if adding new features
- Add debug logging for new functionality using `logger.debug()`
- Ensure backwards compatibility when possible

## Code Style

### General Guidelines

- Use type hints for function parameters and return values
- Prefix private methods with underscore (`_`)
- Keep functions focused and single-purpose
- Use descriptive variable names

### Async Patterns

This project uses `asyncio` throughout. Follow these patterns:

```python
# Use async context managers for resource management
async with BrowserSession(account, pool) as session:
    result = await session.user_timeline(...)

# Use asyncio.Lock for shared state
async with self._lock:
    # critical section
```

### Logging

Use the project's logger for debug output:

```python
from fbscrape.logger import logger

logger.debug(f"Processing {handle}")
logger.info(f"Scraped {len(posts)} posts")
logger.warning(f"Rate limit approaching")
logger.error(f"Failed to login: {error}")
```

Enable debug logging during development:
```bash
FB_LOG_LEVEL=DEBUG python your_script.py
```

### Project Structure

When adding new functionality:

| Component | Location | Purpose |
|-----------|----------|---------|
| High-level API | `scraper.py` | User-facing methods |
| Concurrency | `worker_pool.py`, `worker.py` | Task distribution |
| Browser automation | `browser_session.py` | Playwright/Camoufox |
| Account management | `accounts_pool.py`, `account.py` | SQLite operations |
| CLI commands | `cli.py` | Click-based commands |
| Data models | `models.py` | Query, ScrapingResult |
| Exceptions | `exceptions.py` | Custom error types |

## Testing

### Manual Testing

Test your changes against real Facebook pages:

```python
from fbscrape import FacebookScraper
import asyncio

async def test():
    async with FacebookScraper(db="test.db") as scraper:
        result = await scraper.user_timeline("zuck", "2024-01-01", "2025-01-01")
        print(f"Posts: {len(result.posts)}")

asyncio.run(test())
```

### Debug Mode

Run with debug logging to verify behavior:

```bash
FB_LOG_LEVEL=DEBUG python your_test_script.py
```

## Documentation

- Update README.md for user-facing changes
- Update CLAUDE.md for architectural changes (used by AI assistants)
- Add docstrings to public methods

## Creating a Test Account

For the scraper to be usable in production, we need many accounts. The `fbscrape login`
command opens a Camoufox browser session, pauses at a `breakpoint()`, and persists the
resulting cookies into the AccountsPool DB once you `c`ontinue.


### Prerequisites

- A phone number for account verification (I suggest using [TextVerified](https://www.textverified.com))
- The project installed in development mode
- Be connected to McGill's WiFI directly (not VPN) or the proxy which you will always be logging in from with this account

### Steps

1. Add the account stub to the DB (no cookies yet — set the password if you plan to also use
   `--automatic` later):
   ```bash
   fbscrape account add --phone +1XXXXXXXXXX --password '<password>'
   ```

2. Run the manual login flow. The browser window will open automatically at `facebook.com`
   (use the noVNC viewport at `http://localhost:6080/vnc.html` if you're in the container):
   ```bash
   fbscrape login +1XXXXXXXXXX --manual --no-headless
   ```

3. In the browser, manually create your Facebook account:
   - Click "Create new account"
   - Fill in the required information
   - Complete any verification steps (phone)
   - Ensure you are fully logged in

4. Add a human touch to your Facebook profile. Facebook would rather you scrape than produce inauthentic activity and ruin people's experience. Build the profile without leaving too much of a trace by:
   - Add a profile and cover photo,
   - Fill out account information (interests, hobbies, films, music etc...)
   - Follow a couple of big pages
   - Watch some reels and maybe like a couple

5. Once logged in and humanized, return to the terminal at the `(Pdb)` prompt and press **c** + Enter
   to save cookies to the DB. (Press **q** + Enter to abort without saving anything.)

6. Verify the account is now active:
   ```bash
   fbscrape account info +1XXXXXXXXXX
   ```

### Notes

- The `--output` flag specifies the filename (saved in `~/.fbscrape/auth/`)
- If the output file already exists, the script will raise an error
- Cookies allow the scraper to reuse sessions without re-logging in
- For testing, consider using accounts specifically created for development
- I suggest you name `my_account.json` to `first_name_last_name.json`
- Currently, all accounts are being saved in `db/accounts.db` by default. Currently, the file is ignored in `.gitignore`. While we figure out how best to share accounts, let's just create accounts independently and share informally through Slack. 