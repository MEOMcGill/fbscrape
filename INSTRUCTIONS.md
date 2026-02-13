from fbscrape import FacebookScraper

# Vision for what I want the Facebook scraper to look like

## Usage

I want a whole scraping script to look like this

```python
from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather
import asyncio


async def main():
   fb_scraper = FacebookScraper(db="path_to_db")
   handles_to_scrape: list[str] = ["handle_1", "handle_2", ..., "handle_n"]

   for c in gather(
           *fb_scraper.user_timeline(handle=h, start_date="2025-01-01", end_date="2026-01-01") for h in
           handles_to_scrape
   ):
      scraping_result = await c
      print(scraping_result)

   await fb_scraper.close()


if __name__ == "__main__":
   asyncio.run(main())
```

For now only `scrape_user_homepage` is implemented but ideally in the future you can use more endpoints.

## Architecture: 

### FacebookScraper library
The core scraping method scrape_user_homepage(handle, start_date, end_date) is an async coroutine that returns a ScrapingResult(handle, posts=[...]).
Internally, the library also exposes an async generator variant that yields individual Post objects during scrolling, for callers who want per-post streaming.
Concurrency & streaming: The caller uses a gather async generator that wraps asyncio.as_completed:

```python
import asyncio
async def gather(coros):
    for c in asyncio.as_completed(list(coros)):
        yield await c
```

This yields results as each handle completes, allowing the caller to save and discard immediately — no accumulation in memory.
Usage:

```python
async for result in gather(
        scraper.user_timeline(handle=h, start_date=..., end_date=...)
        for h in handles
):
   save_to_db(result)
```
   
### Worker pool

Inside the library, a WorkerPool manages N browser instances with account rotation which is handled with the 
AccountsPool object. If an account gets banned mid-scrape, the worker swaps in a new active and free account and 
continues. I want the procedure to figure out how many browsers to instantiate like this

`num_browsers_to_instantiate = min(max_browser_sessions, free_accounts, urls_to_scrape)`

Importantly, for now, I want a browser to be logged into an account, I want to keep track of the number of times the
browser scrolled on the webpage with an account, and I want to keep using the same logged in browser for different scraping
requests until either:
1. the account gets banned (in that case we get a new free and active account),
2. it's finished scraping,
3. the number of for an account has surpassed a certain threshold.

### Key principle

The library returns data. The caller handles saving, memory management, and persistence. Clean separation.

---

# Architecture Implementation Plan

Based on the vision above and patterns from twscrape, with browser automation using Playwright/Camoufox.

## Current State

**Existing Components:**
- ✅ `Account` dataclass with email-based primary key
- ✅ `AccountsPool` with async methods (add, delete, get, login, lock management)
- ✅ `BrowserManager` and `PageController` (async Playwright)
- ✅ `FacebookAuth` for login flow
- ✅ `ResponseInterceptor` for GraphQL response parsing
- ✅ `ScrapingResult` and `Query` models
- ✅ Database layer with migrations
- ✅ `gather()` utility in utils.py

**Missing Components:**
- ❌ WorkerPool for managing N concurrent browser sessions
- ❌ FacebookScraper API class with proper initialization
- ❌ CLI interface for account management
- ❌ Scroll tracking per account
- ❌ Account rotation logic based on scroll threshold
- ❌ Error handling and ban detection
- ❌ Browser session reuse across scraping tasks

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    FacebookScraper (API)                      │
│  - scrape_user_homepage(handle, start_date, end_date)       │
│  - close()                                                    │
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
    ┌────┴────┐
    │         │
┌───▼──┐  ┌──▼───┐
│Worker│  │Worker│  ... (N workers)
└──┬───┘  └───┬──┘
   │          │
┌──▼──────────▼───────────┐
│  BrowserSession         │
│  - Playwright context   │
│  - PageController       │
│  - ResponseInterceptor  │
│  - Account              │
│  - scroll_count         │
└─────────────────────────┘
```

## Core Components

### 1. FacebookScraper (Main API Class)

**File:** `fbscrape/api.py` (new)

**Responsibility:** Public-facing API, orchestrates scraping requests

```python
class FacebookScraper:
    def __init__(
        self,
        db: str | AccountsPool = "accounts.db",
        max_browser_sessions: int = 5,
        scroll_threshold: int = 100,
        headless: bool = True,
        proxy: str | None = None
    ):
        self.pool = db if isinstance(db, AccountsPool) else AccountsPool(db)
        self.worker_pool = WorkerPool(
            pool=self.pool,
            max_workers=max_browser_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            proxy=proxy
        )

    async def scrape_user_homepage(
        self,
        handle: str,
        start_date: str,
        end_date: str
    ) -> ScrapingResult:
        """Main scraping method - submits task to worker pool"""
        return await self.worker_pool.submit_task(
            endpoint="user_page",
            handle=handle,
            start_date=start_date,
            end_date=end_date
        )

    async def close(self):
        """Cleanup all browser sessions"""
        await self.worker_pool.close()
```

**Key Changes from Current `scraper.py`:**
- Move from `FacebookScraper` being a scraper to being an API orchestrator
- Delegate actual scraping to WorkerPool
- Remove direct PageController/ResponseInterceptor dependencies

---

### 2. WorkerPool (Browser Session Manager)

**File:** `fbscrape/worker_pool.py` (new)

**Responsibility:** Manage N concurrent browser workers, task distribution, account rotation

```python
class WorkerPool:
    def __init__(
        self,
        pool: AccountsPool,
        max_workers: int,
        scroll_threshold: int,
        headless: bool,
        proxy: str | None
    ):
        self.pool = pool
        self.max_workers = max_workers
        self.scroll_threshold = scroll_threshold
        self.headless = headless
        self.proxy = proxy
        self.workers: list[Worker] = []
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def initialize(self):
        """Start worker pool - creates browser sessions"""
        num_active_accounts = await self.pool.count_active()
        num_workers = min(self.max_workers, num_active_accounts)

        for i in range(num_workers):
            worker = Worker(
                worker_id=i,
                pool=self.pool,
                scroll_threshold=self.scroll_threshold,
                headless=self.headless,
                proxy=self.proxy
            )
            await worker.initialize()
            self.workers.append(worker)
            asyncio.create_task(worker.run(self.task_queue))

        self._running = True

    async def submit_task(
        self,
        endpoint: str,
        handle: str,
        start_date: str,
        end_date: str
    ) -> ScrapingResult:
        """Submit scraping task and wait for result"""
        if not self._running:
            await self.initialize()

        result_future = asyncio.Future()
        task = ScrapeTask(
            endpoint=endpoint,
            handle=handle,
            start_date=start_date,
            end_date=end_date,
            result_future=result_future
        )
        await self.task_queue.put(task)
        return await result_future

    async def close(self):
        """Shutdown all workers"""
        self._running = False
        for worker in self.workers:
            await worker.close()
```

---

### 3. Worker (Individual Browser Session)

**File:** `fbscrape/worker.py` (new)

**Responsibility:** Manage single browser session, execute scraping tasks, handle account rotation

```python
class Worker:
   def __init__(
           self,
           worker_id: int,
           pool: AccountsPool,
           scroll_threshold: int,
           headless: bool,
           proxy: str | None
   ):
      self.worker_id = worker_id
      self.pool = pool
      self.scroll_threshold = scroll_threshold
      self.headless = headless
      self.proxy = proxy

      self.browser_session: BrowserSession | None = None
      self.current_account: Account | None = None
      self.scroll_count: int = 0

   async def initialize(self):
      """Get account and create browser session"""
      self.current_account = await self.pool.get_for_queue_or_wait("user_page")
      if not self.current_account:
         raise NoAccountError("No accounts available")

      self.browser_session = await BrowserSession.create(
         account=self.current_account,
         headless=self.headless,
         proxy=self.proxy
      )
      self.scroll_count = 0
      logger.info(f"Worker {self.worker_id} initialized with account {self.current_account.email}")

   async def run(self, task_queue: asyncio.Queue):
      """Main worker loop - process tasks from queue"""
      while True:
         task: ScrapeTask = await task_queue.get()
         try:
            result = await self.execute_task(task)
            task.result_future.set_result(result)
         except Exception as e:
            task.result_future.set_exception(e)
         finally:
            task_queue.task_done()

   async def execute_task(self, task: ScrapeTask) -> ScrapingResult:
      """Execute scraping task, handle rotation if needed"""
      # Check if need to rotate account
      if self.scroll_count >= self.scroll_threshold:
         logger.info(f"Worker {self.worker_id} reached scroll threshold, rotating account")
         await self.rotate_account()

      try:
         # Execute scraping
         result = await self.browser_session.user_timeline(
            handle=task.handle,
            start_date=task.start_date,
            end_date=task.end_date
         )

         # Track scrolls
         # TODO: Extract scroll count from scraping logic
         self.scroll_count += result.metadata.get('scroll_count', 0)

         return result

      except AccountBannedError as e:
         logger.warning(f"Worker {self.worker_id} account banned: {e}")
         await self.pool.mark_inactive(self.current_account.email, str(e))
         await self.rotate_account()
         # Retry with new account
         return await self.execute_task(task)

   async def rotate_account(self):
      """Release current account, get new one, recreate browser session"""
      # Release old account
      if self.current_account:
         await self.pool.release_account(self.current_account, "user_page")

      # Close old browser session
      if self.browser_session:
         await self.browser_session.close()

      # Reinitialize with new account
      await self.initialize()

   async def close(self):
      """Cleanup browser session"""
      if self.browser_session:
         await self.browser_session.close()
      if self.current_account:
         await self.pool.release_account(self.current_account, "user_page")
```

---

### 4. BrowserSession (Browser Lifecycle Manager)

**File:** `fbscrape/browser_session.py` (new, consolidates current components)

**Responsibility:** Manage browser context, login, and scraping execution

```python
class BrowserSession:
    def __init__(
        self,
        account: Account,
        headless: bool,
        proxy: str | None
    ):
        self.account = account
        self.headless = headless
        self.proxy = proxy

        self.browser_manager: BrowserManager | None = None
        self.context: BrowserContext | None = None
        self.page_controller: PageController | None = None
        self.response_interceptor: ResponseInterceptor | None = None

    @classmethod
    async def create(
        cls,
        account: Account,
        headless: bool,
        proxy: str | None
    ) -> "BrowserSession":
        """Factory method to create and initialize browser session"""
        session = cls(account, headless, proxy)
        await session.initialize()
        return session

    async def initialize(self):
        """Create browser, login, setup interceptors"""
        # Create browser manager
        self.browser_manager = BrowserManager()
        await self.browser_manager.create_playwright_instance()

        # Create auth manager
        auth_json = f"auth_{self.account.email}.json"
        facebook_auth = FacebookAuth(
            username=self.account.email,
            password=self.account.password,
            auth_json_path=auth_json
        )

        # Create browser context
        self.context = await self.browser_manager.create_browser_context(
            headless=self.headless,
            mobile=False,  # TODO: Make configurable?
            auth_storage_path=auth_json if os.path.exists(auth_json) else None
        )

        # Create page and controller
        page = await self.context.new_page()
        self.page_controller = PageController(page)

        # Setup response interceptor
        self.response_interceptor = ResponseInterceptor()
        self.response_interceptor.setup_interception(page)

        # Navigate and login if needed
        await self.page_controller.goto("https://www.facebook.com/")
        await asyncio.sleep(5)

        if await facebook_auth.need_to_log_in(page):
            logger.info(f"Logging in with account {self.account.email}")
            await facebook_auth.manual_login(page, mobile=False)
            await asyncio.sleep(10)
            await facebook_auth.save_session_state(self.context)
            await facebook_auth.clear_post_login_popups(page, mobile=False)

        logger.info(f"Browser session initialized for {self.account.email}")

    async def scrape_user_homepage(
        self,
        handle: str,
        start_date: str,
        end_date: str
    ) -> ScrapingResult:
        """Execute user homepage scraping"""
        # Import scraping logic from current scraper.py
        from .scraper import scrape_user_homepage_impl

        return await scrape_user_homepage_impl(
            handle=handle,
            start_date=start_date,
            end_date=end_date,
            page_controller=self.page_controller,
            response_interceptor=self.response_interceptor
        )

    async def close(self):
        """Cleanup browser resources"""
        if self.browser_manager:
            await self.browser_manager.close()
```

---

### 5. CLI Interface

**File:** `fbscrape/cli.py` (new)

**Commands:**

```bash
# Account management
fbscrape add_accounts <file.txt>           # Add accounts from file
fbscrape delete_accounts <email1> <email2>  # Delete specific accounts
fbscrape delete_inactive                    # Delete all inactive accounts

# Login
fbscrape login_accounts                     # Login all inactive accounts
fbscrape login <email>                      # Login specific account
fbscrape relogin <email>                    # Reset and relogin account

# Info
fbscrape accounts                           # List all accounts with stats
fbscrape stats                              # Show pool statistics

# Scraping (optional, mainly for testing)
fbscrape scrape <handle> --start 2025-01-01 --end 2026-01-01

# Maintenance
fbscrape reset_locks                        # Reset all account locks
```

**Implementation using Click:**

```python
import click
import asyncio
from .accounts_pool import AccountsPool

@click.group()
def cli():
    """Facebook Scraper CLI"""
    pass

@cli.command()
@click.argument('filepath')
@click.option('--db', default='accounts.db')
def add_accounts(filepath, db):
    """Add accounts from file (format: email:password:email_password)"""
    async def _add():
        pool = AccountsPool(db)
        # Parse file and add accounts
        with open(filepath) as f:
            for line in f:
                email, password, email_password = line.strip().split(':')
                await pool.add_account(
                    email=email,
                    password=password,
                    email_password=email_password
                )

    asyncio.run(_add())

@cli.command()
@click.option('--db', default='accounts.db')
def accounts(db):
    """Show all accounts"""
    async def _show():
        pool = AccountsPool(db)
        accounts = await pool.get(None)
        for acc in accounts:
            print(f"{acc.email} - Active: {acc.active} - Last used: {acc.last_used}")

    asyncio.run(_show())

# ... more commands
```

---

### 6. Refactored scraper.py

**Current scraper.py becomes a function, not a class:**

```python
# fbscrape/scraper.py
async def scrape_user_homepage_impl(
    handle: str,
    start_date: str,
    end_date: str,
    page_controller: PageController,
    response_interceptor: ResponseInterceptor,
    channel: BlockingChannel | None = None
) -> ScrapingResult:
    """
    Core scraping logic extracted from current FacebookScraper.scrape_user_homepage
    """
    # (Current implementation stays mostly the same)
    # Just extract it as a standalone function that receives dependencies
```

---

## Module Structure

```
fbscrape/
├── __init__.py              → Export FacebookScraper, gather, models
├── api.py                   → FacebookScraper (main API class) [NEW]
├── worker_pool.py           → WorkerPool, ScrapeTask [NEW]
├── worker.py                → Worker [NEW]
├── browser_session.py       → BrowserSession [NEW]
├── scraper.py               → scrape_user_homepage_impl (refactored)
├── account.py               → Account dataclass (existing)
├── accounts_pool.py         → AccountsPool (existing)
├── browser.py               → BrowserManager, PageController (existing)
├── session.py               → FacebookAuth (existing)
├── response.py              → ResponseInterceptor (existing)
├── models.py                → Query, ScrapingResult, Post, User (existing)
├── db.py                    → Database utilities (existing)
├── utils.py                 → gather, utc, helpers (existing)
├── logger.py                → Logging setup (existing)
├── cli.py                   → Command-line interface [NEW]
└── exceptions.py            → Custom exceptions [NEW]
```

---

## Database Schema Updates

**Add scroll tracking to accounts table:**

```python
# In db.py migration
async def v2():
    await db.execute("ALTER TABLE accounts ADD COLUMN scroll_count INTEGER DEFAULT 0")
```

**Update Account dataclass:**

```python
@dataclass
class Account(JSONTrait):
    # ... existing fields ...
    scroll_count: int = 0  # Track scrolls per account
```

---

## Error Handling

**File:** `fbscrape/exceptions.py` (new)

```python
class FacebookScraperError(Exception):
    """Base exception for fbscrape"""
    pass

class NoAccountError(FacebookScraperError):
    """No accounts available in pool"""
    pass

class AccountBannedError(FacebookScraperError):
    """Account has been banned"""
    pass

class LoginFailedError(FacebookScraperError):
    """Failed to login to account"""
    pass

class RateLimitError(FacebookScraperError):
    """Hit rate limit"""
    pass
```

**Ban Detection (in PageController.check_error_conditions):**
- Account suspended/banned
- Rate limit errors
- Checkpoint challenges

**Error Recovery Strategy:**
1. **AccountBannedError** → Mark inactive, rotate to new account, retry
2. **RateLimitError** → Lock account temporarily, rotate, retry
3. **LoginFailedError** → Mark inactive, continue with next account
4. **Connection errors** → Retry same account (3 attempts)

---

## Implementation Phases

### Phase 1: Refactor Current Code (Foundation)
**Goal:** Reorganize existing code without changing functionality

1. ✅ Create `exceptions.py` with custom exceptions
2. ✅ Extract scraping logic from `FacebookScraper` to `scrape_user_homepage_impl()` function
3. ✅ Add scroll count tracking to Account and database
4. ✅ Create `BrowserSession` class to encapsulate browser lifecycle

**Files to modify:**
- `fbscrape/exceptions.py` (new)
- `fbscrape/scraper.py` (refactor class to function)
- `fbscrape/browser_session.py` (new, consolidate browser setup)
- `fbscrape/account.py` (add scroll_count field)
- `fbscrape/db.py` (add migration)

### Phase 2: Worker Pool Implementation
**Goal:** Add concurrent browser management

1. ✅ Create `WorkerPool` class
2. ✅ Create `Worker` class with account rotation logic
3. ✅ Implement task queue and distribution
4. ✅ Add scroll threshold checking
5. ✅ Integrate ban detection and recovery

**Files to modify:**
- `fbscrape/worker_pool.py` (new)
- `fbscrape/worker.py` (new)
- `fbscrape/browser.py` (may need updates for session reuse)

### Phase 3: API Class
**Goal:** Create clean public API

1. ✅ Create `FacebookScraper` API class in `api.py`
2. ✅ Integrate with WorkerPool
3. ✅ Add initialization and cleanup methods
4. ✅ Update `__init__.py` exports

**Files to modify:**
- `fbscrape/api.py` (new)
- `fbscrape/__init__.py` (update exports)

### Phase 4: CLI Interface
**Goal:** Add command-line tools

1. ✅ Create `cli.py` with Click commands
2. ✅ Implement account management commands
3. ✅ Add stats/monitoring commands
4. ✅ Create entry point in `setup.py`/`pyproject.toml`

**Files to modify:**
- `fbscrape/cli.py` (new)
- `pyproject.toml` or `setup.py` (add console_scripts entry point)

### Phase 5: Testing & Documentation
**Goal:** Ensure reliability

1. ✅ Test concurrent scraping (multiple handles)
2. ✅ Test account rotation scenarios
3. ✅ Test ban detection and recovery
4. ✅ Update README with new usage examples
5. ✅ Add docstrings to all new classes/methods

---

## Configuration

**Environment Variables:**
```bash
FB_PROXY=http://proxy:8080          # Default proxy
FB_RAISE_WHEN_NO_ACCOUNT=false      # Error on no accounts
FB_LOG_LEVEL=INFO                   # Logging level
FB_MAX_RETRIES=3                    # Connection retry attempts
```

**Per-instance Configuration:**
```python
scraper = FacebookScraper(
    db="accounts.db",
    max_browser_sessions=5,     # Max concurrent browsers
    scroll_threshold=100,       # Scrolls before account rotation
    headless=True,              # Headless browser mode
    proxy=None                  # Override default proxy
)
```

---

## Testing Strategy

**Unit Tests:**
- Test AccountsPool methods
- Test Worker rotation logic
- Test error handling and recovery

**Integration Tests:**
- Test end-to-end scraping with single handle
- Test concurrent scraping with multiple handles
- Test account rotation when threshold reached
- Test ban detection and account switching

**Test Scenarios:**
1. Scrape 1 handle, verify result structure
2. Scrape 10 handles concurrently, verify all complete
3. Simulate account ban mid-scrape, verify rotation
4. Reach scroll threshold, verify account rotation
5. Run out of accounts, verify NoAccountError

---

## Migration Path from Current Code

**Step 1:** Test current code works
```bash
python -m pytest examples/
```

**Step 2:** Phase 1 refactoring (no behavior change)
- Extract scraping function
- Create BrowserSession
- Run tests again, ensure passing

**Step 3:** Phase 2 (add WorkerPool)
- Implement WorkerPool/Worker
- Keep old code path available for comparison
- Test both code paths side-by-side

**Step 4:** Phase 3 (new API)
- Create FacebookScraper API class
- Update example scripts to use new API
- Deprecate old usage pattern

**Step 5:** Phase 4 (CLI)
- Add CLI commands
- Document in README

**Step 6:** Cleanup
- Remove old code paths
- Final testing
- Update documentation

---

## Open Questions

1. **Browser reuse across tasks:** Should we log out between handles or keep session alive?
   - **Recommendation:** Keep alive for performance, but add config option

2. **Scroll counting:** How to accurately track scrolls when scraping?
   - **Recommendation:** Return scroll count from scrape_user_homepage_impl as metadata

3. **Mobile vs Desktop:** Always desktop, or make configurable?
   - **Recommendation:** Start with desktop, add mobile support later

4. **Proxy rotation:** Per account or per worker?
   - **Recommendation:** Per account (stored in Account model)

5. **Failed account retry:** Should we retry failed accounts automatically?
   - **Recommendation:** CLI command for manual retry, auto-retry optional

---

## Success Criteria

✅ **Functional:**
- Can scrape multiple handles concurrently
- Accounts rotate after 100 scrolls
- Banned accounts automatically replaced
- CLI for account management works
- Memory efficient (no accumulation)

✅ **Performance:**
- N concurrent browsers run smoothly
- Account switching < 10 seconds
- Browser sessions reused efficiently

✅ **Maintainability:**
- Clean separation of concerns
- Comprehensive docstrings
- Easy to add new endpoints
- Well-tested error handling

✅ **Usability:**
- Simple API matching vision in INSTRUCTIONS.md
- CLI commands for common operations
- Clear error messages
- Good documentation
