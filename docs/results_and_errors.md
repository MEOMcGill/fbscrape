# Results & error handling

What a scrape call returns, what your `gather()` loop yields, and how the
`Worker` reacts to each failure. For the account state machine behind the
rotation decisions, see
[`architecture/account_management.md`](architecture/account_management.md).

## What a scrape returns

Each high-level call resolves to a `ScrapingResult` with a `result` string:

- **Success set:** `"success"` (and the paginated variants that still carry
  usable data, e.g. a cap was hit).
- **Non-rotation outcomes** — the future still resolves *successfully* with one
  of these; they describe a per-task condition, not a crash:
  `"account is private"`, `"logged out while scraping"`, `"rate_limit"` (partial
  data preserved, account locked), `"parse_error"` (e.g. a `user_id` sent to
  `PageTransparency`), `"graphql_error: …"`, `"error: …"`.

Always branch on `result` before trusting `data` — a resolved future does not
imply a full, clean scrape.

## What your `gather()` loop yields

```python
from fbscrape import FacebookScraper, gather
from fbscrape.exceptions import NoAccountError, RetryBudgetExhaustedError

async with FacebookScraper(db="db/accounts.db") as scraper:
    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01")
        for h in handles
    ):
        # `result` is a ScrapingResult on success. The loop body instead
        # RAISES when a task exhausts its options:
        #   - RetryBudgetExhaustedError: this handle failed 3 times
        #   - NoAccountError: the pool drained mid-run
        print(result.query.query["handle"], result.result, len(result.data))
```

`RendererHangError` (a page-level await exceeding `operation_timeout_seconds`)
is caught internally and triggers a same-account restart with a fresh
`BrowserSession`; it only surfaces to your code — as
`RetryBudgetExhaustedError` — if it blows the retry budget.

## Worker policy per exception

The `Worker` owns one account and reacts to each exception as follows (full
state machine in
[`architecture/account_management.md`](architecture/account_management.md)):

| Exception | Action | Counts as retry? |
|---|---|---|
| `AccountDisabledError` | rotate to a new account | no |
| `CheckpointError` | rotate to a new account | yes |
| `TransientLoginError` | rotate, account stays active | yes |
| `RendererHangError` | restart task on **same** account, fresh `BrowserSession` | yes |
| `FailedLoginError` | mark account inactive + rotate | yes |
| `AccountBannedError` | mark account inactive + rotate | yes |
| `RateLimitError` | lock account + rotate | yes |
| `NoAccountError` | re-queue task, worker exits cleanly | — |

After 3 retries on the same task, `Worker.execute_task` raises
`RetryBudgetExhaustedError`, which surfaces as the raised value in your
`gather()` loop.

## Exception reference

```python
from fbscrape.exceptions import (
    # Pool-level
    NoAccountError,             # No accounts available in the pool

    # Login / account state (raised in BrowserSession, caught by Worker)
    FailedLoginError,           # Login attempt failed
    CheckpointError,            # FB redirected to /checkpoint — manual action
    AccountDisabledError,       # FB redirected to /checkpoint/disabled — dead
    TransientLoginError,        # Playwright/page flake — account stays active
    AccountBannedError,         # HTTP 403 mid-scrape, account flagged
    RateLimitError,             # HTTP 429 mid-scrape

    # Browser / runtime
    RendererHangError,          # A page await exceeded operation_timeout_seconds
    RetryBudgetExhaustedError,  # Task failed its 3-retry budget
)
```
