# Contributing to fbscrape

Thanks for your interest in contributing. This guide covers setting up a
development environment, running the tests, and the conventions to follow.

## Getting started

### Prerequisites

- Python 3.10+
- pip or uv
- Git

### Development setup

1. Clone the repository:
   ```bash
   git clone https://github.com/MEOMcGill/fbscrape.git
   cd fbscrape
   ```
2. Install in editable mode with the dev extras:
   ```bash
   pip install -e '.[dev]'
   ```
3. Install the Camoufox (Firefox) browser:
   ```bash
   playwright install firefox
   ```
4. Verify:
   ```bash
   fbscrape --help
   ```

## Running the tests

The suite has three tiers (full layout in [`tests/README.md`](tests/README.md)):

| Command | Runs |
|---|---|
| `pytest` | unit tier — fast, no network; run this on every change |
| `pytest -m integration` | headless scrapes against real Facebook (needs an active account in `db/accounts.db`) |
| `FBSCRAPE_RUN_E2E=1 pytest -m e2e` | full CLI scrape → flatten / download-media (needs an account; opt-in via the env var) |

Integration and e2e tests skip automatically when no account is available, so a
plain `pytest` is safe to run anywhere.

## Making changes

### Code style

- Type hints on function parameters and return values.
- Prefix private methods with `_`.
- Keep functions focused and single-purpose; use descriptive names.
- Match the style of the surrounding code.

The project is `asyncio` throughout:

```python
# Async context managers for resource management
async with BrowserSession(account, pool) as session:
    outcome = await session.user_timeline_hybrid(...)

# asyncio.Lock for shared state
async with self._lock:
    ...  # critical section
```

Use the project logger for output, and enable it with `FB_LOG_LEVEL=DEBUG`:

```python
from fbscrape.logger import logger

logger.debug(f"Processing {handle}")
logger.info(f"Scraped {len(posts)} posts")
```

### Where things live

| Component | Location |
|---|---|
| High-level API | `scraper.py` |
| Concurrency | `worker_pool.py`, `worker.py` |
| Browser automation | `browser_session.py` |
| Account management | `accounts_pool.py`, `account.py` |
| CLI commands | `cli.py` |
| Data models | `models.py` |
| GraphQL parsing | `response.py` |
| Exceptions | `exceptions.py` |

Architecture walkthrough: [`docs/architecture/overview.md`](docs/architecture/overview.md).

### Adding an endpoint

Endpoints are wired through the `Query.ENDPOINT_REGISTRY` and a handful of
touch points. The full playbook is in
[`docs/adding_endpoints.md`](docs/adding_endpoints.md), and the per-endpoint
guides under [`docs/endpoints/`](docs/endpoints/README.md) show the expected
shape. Every new endpoint must ship with its tests: a capture fixture, a
flatten unit test, an integration test, an `e2e` entry, and a registry-pin bump
— see [`tests/README.md`](tests/README.md).

### Tests and docs are required

- Add or extend tests for any behavior change.
- Update `README.md` for user-facing changes, and the relevant docs under
  `docs/` when behavior changes.
- Add docstrings to public methods.

## Pull requests

1. Fork and create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Keep the PR focused — one feature or fix.
3. Make sure `pytest` passes and documentation is updated.
4. Open a pull request with a clear description of what changed and why.

## Reporting issues

Before opening an issue, search existing ones to avoid duplicates. Include:

- Python version (`python --version`) and operating system
- Steps to reproduce
- Expected vs. actual behavior
- Relevant logs (run with `FB_LOG_LEVEL=DEBUG`)

## Creating a Facebook account for the pool

The scraper needs at least one logged-in account. `fbscrape login` opens a
Camoufox browser, pauses at a `(Pdb)` prompt so you can log in by hand, and
saves the resulting cookies into the account pool when you continue.

### Prerequisites

- A phone number for verification (a service such as
  [TextVerified](https://www.textverified.com) works if you'd rather not use a
  personal number).
- The project installed in development mode.
- A stable IP you'll consistently log in from for that account (a residential
  connection or a fixed proxy). Facebook is sensitive to the login location
  changing between sessions.

### Steps

1. Add the account stub to the pool (set a password if you'll also use the
   automatic login flow later):
   ```bash
   fbscrape account add --phone +1XXXXXXXXXX --password '<password>'
   ```
2. Run the manual login flow. A non-headless browser opens at `facebook.com`
   (use the noVNC viewport at `http://localhost:6080/vnc.html` if you're running
   in the container):
   ```bash
   fbscrape login +1XXXXXXXXXX --mode manual --no-headless
   ```
3. In the browser, log in (or create the account) and complete any verification.
4. Give the profile a minimal authentic footprint before scraping — a profile
   photo, a few interests, following a couple of large public pages. Brand-new
   accounts with no activity get flagged faster.
5. Back at the `(Pdb)` prompt, press **c** + Enter to save the cookies (or
   **q** + Enter to abort without saving).
6. Confirm the account is active:
   ```bash
   fbscrape account info +1XXXXXXXXXX
   ```

### Notes

- Cookies let the scraper reuse the session without re-logging in. They're
  stored in the account pool (`db/accounts.db` by default, which is gitignored).
- Use accounts created specifically for scraping — never a personal account.

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](LICENSE).
