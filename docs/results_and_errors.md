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

- **Stop-condition outcomes** (paginated endpoints) — how the pagination loop
  decided to stop, all carrying usable data: `"end_of_feed"` (FB returned a
  null `end_cursor`), `"scraped until user-specified starting date was reached"` (the oldest post in a batch fell below
  `start_date` — chronological feeds only), `"consecutive_out_of_range"`,
  `"max_posts_reached"`, `"no_new_posts_streak"`, `"cursor_reset"`,
  `"response_shape_error"`.

  `end_of_feed` and the date-stop string are **not** interchangeable: the first
  means FB stopped serving, which implies nothing about whether `start_date`
  was reached. They shared one return string before, making the two
  indistinguishable; results saved by older versions still carry the date-stop
  string for both cases.

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
| `TransientLoginError` | rotate, account stays active — until 3 consecutive (per process, reset on success), then mark inactive | spends the **login** budget (`MAX_LOGIN_RETRIES`, 5), not `max_retries` |
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

## Two retry budgets

`Worker.execute_task` counts login rotations separately from scrape retries:

| budget | constant | spent by |
|---|---|---|
| scrape | `max_retries` (3) | checkpoints, rate limits, renderer hangs, retryable result strings |
| login | `MAX_LOGIN_RETRIES` (5) | `TransientLoginError` — an account that would not let us in |

They are independent in both directions: a run of accounts that refuse to log
in cannot consume the scrape's retries, and a flaky scrape cannot consume the
login budget. `RetryBudgetExhaustedError` names whichever one ran out, because
"no account completed a login" and "the scrape kept erroring" are diagnosed in
completely different places.

The login budget is the larger of the two, and deliberately so. `AccountsPool`
orders candidates by `scroll_count_overall_24h ASC`, and an account that never
completes a login never scrolls — so it keeps the lowest count in the pool and
keeps getting picked FIRST. The weakest accounts therefore collect at the head
of the queue, and the budget has to be deep enough to walk past them. Observed
2026-11-16: a group chain ended after three consecutive login timeouts on the
three lowest-scroll accounts, with four healthy accounts sitting untouched
behind them.

### Which account each attempt asks for

Raising the budget alone would treat the symptom: the same weak accounts would
simply absorb five attempts instead of three. So the ordering escalates as
attempts are spent.

| attempt | `ORDER BY` | intent |
|---|---|---|
| 1-3 | `scroll_count_overall_24h ASC, last_used ASC` (pool default) | most rested first, record ignored — a healthy account that had a bad night still gets tried on merit |
| 4 | `login_fail_streak ASC, scroll_count_overall_24h ASC, last_used ASC` | proven logins lead, rest breaks ties |
| 5 | `login_ok_total / (login_ok_total + login_fail_total + 1) DESC, login_fail_streak ASC` | best login history outright; scroll count ignored entirely |

The `+ 1` is Laplace smoothing: without it an account with no login record
scores `0/0`, and an account that has never been tried would outrank one with a
long clean history. With it, unproven ranks below proven.

Tier 5 deliberately ignores scroll count, so a heavily used but reliable
account is reachable as a last resort. Over-use stays bounded by the scroll
threshold, which rotates such an account out on its own schedule.

Both of the columns this ranks on are persisted (`migrate_v4`). They had to be:
the consecutive-failure streak that retires a chronically broken account used
to live in memory on the pool object, so a caller that runs one process per
unit of work (as the group chain does, one per leg) reset it to zero every time
and never reached the threshold. Persisting it fixes the retirement path and
supplies the selection signal in the same stroke.
