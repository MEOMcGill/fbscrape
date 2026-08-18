# Architecture Overview

Design decisions, architecture, and implementation patterns used in the fbscrape library.

## Design Goals

1. **Simple API** - Users should be able to scrape Facebook with minimal code
2. **Concurrent** - Support multiple browser sessions scraping in parallel
3. **Resilient** - Handle account bans, rate limits, and errors gracefully
4. **Memory Efficient** - Stream results as they complete, don't accumulate in memory
5. **Maintainable** - Clean separation of concerns, easy to extend

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     FacebookScraper                               │
│  user_timeline(handle, start_date, end_date) → ScrapingResult    │
│  - High-level API, hides complexity                               │
│  - asyncio.Lock for thread-safe lazy initialization              │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                       WorkerPool                                  │
│  - Creates N workers: min(max_sessions, active_accounts)         │
│  - Shared asyncio.Queue for task distribution                    │
│  - Future-based results (Producer-Consumer pattern)              │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                         Worker                                    │
│  - Owns ONE account from AccountsPool                            │
│  - Tracks scroll count, rotates at threshold                     │
│  - Creates fresh BrowserSession per task (context manager)       │
│  - Error recovery: login failure, ban, rate limit → rotate       │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                      BrowserSession                               │
│  - Playwright browser with Camoufox anti-detection               │
│  - Cookie-based authentication                                    │
│  - GraphQL response interception for data extraction             │
│  - Returns ScrapingResult with posts                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                      AccountsPool                                 │
│  - SQLite storage for accounts                                   │
│  - Locking mechanism (in_use flag, locked_until)                 │
│  - Scroll count tracking per endpoint                            │
│  - Account selection by least recently used                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## Layer Responsibilities

| Layer | Responsibility | Key Pattern |
|-------|---------------|-------------|
| **FacebookScraper** | User-facing API, hides complexity | Facade |
| **WorkerPool** | Concurrency, task distribution | Producer-Consumer |
| **Worker** | Account lifecycle, error recovery | State Machine |
| **BrowserSession** | Browser automation, data extraction | Context Manager |
| **AccountsPool** | Persistence, locking, selection | Repository |

---

## Key Design Patterns

### 1. Producer-Consumer with Futures

The WorkerPool uses a shared `asyncio.Queue` where:
- **Producer**: `submit_task()` creates a Future and puts `(query, future)` on the queue
- **Consumers**: Worker loops pull tasks, execute them, and resolve the Future

```python
# Producer (submit_task)
future = loop.create_future()
await self.task_queue.put((query, future))
return future  # Caller awaits this

# Consumer (worker_loop)
query, future = await self.task_queue.get()
result = await worker.execute_task(query)
future.set_result(result)  # Resolves the caller's await
```

**Why Futures?**
- Caller gets a handle to the result immediately
- Result arrives when ready (not necessarily in order)
- Works naturally with `asyncio.as_completed()` / `gather()`

### 2. Lazy Initialization with Lock

FacebookScraper creates the WorkerPool only on first use:

```python
async def _ensure_initialized(self):
    async with self._init_lock:  # Prevents race condition
        if self.worker_pool is None:
            self.worker_pool = WorkerPool(...)
```

**Why the Lock?**
When using `gather()`, multiple coroutines call `user_timeline()` concurrently. Without a lock:
1. All coroutines check `worker_pool is None` → True
2. All create their own WorkerPool
3. Each spawns workers, using all accounts

The `asyncio.Lock` ensures only one coroutine initializes.

### 3. Context Manager for BrowserSession

Each task gets a fresh BrowserSession via context manager:

```python
async with BrowserSession(account, pool, headless=True) as session:
    result = await session.user_timeline(handle, start_date, end_date)
```

**Why fresh sessions per task?**
- Clean state between handles
- Automatic cleanup on errors
- Browser resources released promptly

### 4. Account Rotation with Cooldown

When rotating accounts, we add a 5-minute lock:

```python
async def rotate_account(self):
    await self.pool.lock_until(
        self.current_account.identifier,
        "datetime('now', '+5 minutes')"
    )
    await self.pool.release_account(...)
```

**Why the cooldown?**
Without it, `get_available()` could return the same account we just released (if it's the least-recently-used). The cooldown ensures we get a different account.

---

## Data Flow

### Scraping a Single Handle

```
1. User calls: scraper.user_timeline("zuck", "2024-01-01", "2025-01-01")

2. FacebookScraper._ensure_initialized()
   └── Creates WorkerPool (if needed)
       └── Creates N Workers
           └── Each Worker acquires an Account

3. WorkerPool.submit_task(query)
   └── Creates Future
   └── Puts (query, future) on task_queue
   └── Returns Future

4. Worker._worker_loop()
   └── Pulls (query, future) from queue
   └── Calls execute_task(query)

5. Worker.execute_task(query)
   └── Creates BrowserSession (context manager)
   └── Calls session.user_timeline(...)

6. BrowserSession.user_timeline()
   └── Navigates to facebook.com/zuck
   └── Scrolls, intercepting GraphQL responses
   └── Builds ScrapingResult with posts

7. Result flows back:
   └── BrowserSession returns ScrapingResult
   └── Worker sets future.set_result(result)
   └── User's await resolves with result
```

### Concurrent Scraping with gather()

```python
async for result in gather(
    scraper.user_timeline(h, "2024-01-01", "2025-01-01")
    for h in handles
):
    process(result)
```

**Flow:**
1. `gather()` wraps coroutines in `asyncio.as_completed()`
2. All `user_timeline()` calls execute concurrently
3. Each creates a Future via WorkerPool
4. Workers process tasks from shared queue
5. `gather()` yields results as Futures resolve (not in order)

---

## Error Handling Strategy

| Error | Action | Retries |
|-------|--------|---------|
| `FailedLoginError` | Mark account inactive, rotate | Up to 3 |
| `AccountBannedError` | Mark account inactive, rotate | Up to 3 |
| `RateLimitError` | Lock account 1 hour, rotate | Up to 3 |
| `NoAccountError` | Propagate to caller | None |
| Other exceptions | Propagate via Future | None |

**Rotation Logic:**
```python
except FailedLoginError:
    await self.pool.mark_inactive(account, "Login failed")
    await self.rotate_account()
    retry_count += 1
    # Continue loop to retry with new account
```

---

## Account Selection Algorithm

`AccountsPool.get_available()` selects accounts that are:
1. `active = true` (not banned/disabled)
2. `in_use = false` (not currently used by another worker)
3. Not locked (no `locked_until` or it's in the past)

Ordered by `scroll_count_overall_24h ASC` (least-used first).

**SQL Query:**
```sql
SELECT * FROM accounts
WHERE active = true
    AND in_use = false
    AND (locks IS NULL
         OR json_extract(locks, '$.locked_until') IS NULL
         OR json_extract(locks, '$.locked_until') < datetime('now'))
ORDER BY scroll_count_overall_24h ASC
LIMIT 1
```

---

## GraphQL Interception

Facebook loads data via GraphQL requests. Instead of parsing DOM, we intercept network responses:

```python
# ResponseInterceptor.setup_interception()
page.on("response", self._handle_response)

async def _handle_response(self, response):
    if "graphql" in response.url:
        data = await response.json()
        posts = self._extract_posts(data)
        self.posts.extend(posts)
```

**Advantages:**
- Gets structured data (JSON) not HTML
- More reliable than DOM selectors
- Captures data even if UI changes

---

## Scroll Tracking

Accounts track scrolls per endpoint:
- `scroll_count_per_endpoint_total`: `{"UserTimeline": 150, "Search": 50}`
- `scroll_count_overall_24h`: Total scrolls in 24 hours

**Rotation Trigger:**
Worker checks scroll count before each task:
```python
if self.scroll_count >= self.scroll_threshold:
    await self.rotate_account()
```

---

## Performance Optimizations

### 1. Error Condition Checking

**Before:** 7 sequential DOM queries on every scroll (~70-350ms each)

**After:**
1. Single combined locator for fast-path detection
2. Only check after navigation and when stalled
3. Detailed checks only if error indicator found

```python
# Fast path - single query
error_locator = page.locator('button:has-text("Retry"), ...')
if await error_locator.count() == 0:
    return None  # No errors

# Slow path - only if something found
# ... detailed checks ...
```

### 2. Lazy Initialization

WorkerPool not created until first task. Avoids startup overhead if scraper is instantiated but not used.

### 3. Worker Reuse

Workers persist across tasks. Only the BrowserSession is recreated per task, not the entire worker infrastructure.

---

## Configuration

```python
FacebookScraper(
    db="accounts.db",           # SQLite database path
    max_browser_sessions=5,     # Max concurrent browsers
    scroll_threshold=500,       # Scrolls before rotating account
    headless=True,              # Run browsers headlessly
    mobile=False,               # Use mobile browser emulation
)
```

**Environment Variables:**
```bash
FB_RAISE_WHEN_NO_ACCOUNT=false  # Raise error vs wait when no accounts
FB_LOG_LEVEL=INFO               # Logging level
```

---

## Extending the Library

### Adding a New Endpoint

1. **Add to `Query.ENDPOINT_REQUIRED_FIELDS`** in `models.py`:
   ```python
   ENDPOINT_REQUIRED_FIELDS = {
       "UserTimeline": ["handle", "start_date", "end_date"],
       "Search": ["query", "start_date", "end_date"],  # New
   }
   ```

2. **Add mapping in `Worker.ENDPOINT_METHODS`**:
   ```python
   ENDPOINT_METHODS = {
       "UserTimeline": "user_timeline",
       "Search": "search",  # New
   }
   ```

3. **Implement in `BrowserSession`**:
   ```python
   async def search(self, query: str, start_date: str, end_date: str):
       # Implementation
   ```

4. **Add API method in `FacebookScraper`**:
   ```python
   async def search(self, query: str, start_date: str, end_date: str):
       await self._ensure_initialized()
       query = Query(endpoint="Search", query={...}, params={})
       future = await self.worker_pool.submit_task(query)
       return await future
   ```

---

## Testing

### Debug Logging

Enable verbose logging to trace execution:

```python
from fbscrape.logger import set_log_level
set_log_level("DEBUG")
```

### Key Log Points

- `WorkerPool initializing N workers` - Worker creation
- `Worker X processing UserTimeline` - Task assignment
- `BrowserSession.initialize() starting` - Browser setup
- `Scrolled N times, intercepted M posts` - Scraping progress
- `WorkerPool: shutdown complete` - Clean shutdown
