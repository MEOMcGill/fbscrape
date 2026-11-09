# Account Management

Accounts live in `db/accounts.db` (schema in `fbscrape/db.py:63-168`). The pool
(`fbscrape/accounts_pool.py:AccountsPool`) is the single source of truth for
account state — workers borrow accounts via `pool.get_available_or_wait()` and
return them via `release_account()` or `rotate_account()`. Login outcomes
(success / checkpoint / banned / transient flake) drive the state machine
defined below; the worker's `except` clauses (`fbscrape/worker.py:282-347`)
translate exceptions into the corresponding pool calls.

## Account States

Every account is in exactly one of four logical states. Transitions are labeled
with the method that performs the change.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> Inactive: row created (active=false)

    Inactive --> ActiveFree: set_active(True, None)<br/>(from _on_login_success)

    ActiveFree --> ActiveInUse: _get_and_mark_in_use<br/>(via get_available)
    ActiveInUse --> ActiveFree: release_account()<br/>(from Worker.close)

    ActiveInUse --> ActiveLocked: lock_until('+5 min')<br/>(rotate_account cooldown)
    ActiveInUse --> ActiveLocked: lock_until('+1 hour')<br/>(RateLimitError)

    ActiveLocked --> ActiveFree: locks.locked_until < now<br/>(implicit, query-driven)

    ActiveInUse --> Inactive: set_active(False, msg)<br/>(checkpoint detector)
    ActiveInUse --> Inactive: mark_inactive(msg)<br/>(worker FailedLoginError /<br/>AccountBannedError)
    ActiveLocked --> Inactive: same writers above<br/>(rare path)

    note right of Inactive
        active=false, error_msg set.
        Re-entered to ActiveFree only
        on a successful login that
        sets active=true and clears
        error_msg.
    end note
```

**Notes**:
- `error_msg` is **set** when an account becomes Inactive and **cleared** when
  `_on_login_success` calls `set_active(True, None)` (`browser_session.py:435`).
- `ActiveLocked → ActiveFree` is not effected by an explicit method call. The
  `get_available()` SQL filters out rows whose `locked_until` is still in the
  future; once it has passed, the row is selectable again.
- `Inactive` accounts are never `in_use` (the `mark_inactive` UPDATE also sets
  `in_use=false`, see `accounts_pool.py:406-414`).

## Task Execution Flow

What happens when a worker pulls a task off the queue:

```mermaid
flowchart TD
    A[task pulled from queue<br/>by _worker_loop] --> B{current_account<br/>is None?}
    B -- yes --> C[pool.get_available_or_wait]
    C --> D{returned None?}
    D -- yes --> E[raise NoAccountError<br/>future.set_exception]
    E --> A
    D -- no --> F[set self.current_account<br/>scroll_count=0]
    B -- no --> G
    F --> G{scroll_count<br/>>= threshold?}
    G -- yes --> H[rotate_account]
    G -- no --> I
    H --> I[retry loop<br/>max 3 attempts]
    I --> J[async with BrowserSession<br/>execute scraping method]

    J -- success --> K[update scroll_count<br/>return result]

    J -- AccountDisabledError --> L[rotate_account<br/>NO retry++]
    J -- CheckpointError --> M[rotate_account<br/>retry++]
    J -- TransientLoginError --> N[rotate_account<br/>retry++<br/>account stays Active]
    J -- FailedLoginError --> O[mark_inactive<br/>rotate_account<br/>retry++]
    J -- AccountBannedError --> P[mark_inactive<br/>rotate_account<br/>retry++]
    J -- RateLimitError --> Q["lock_until('+1 hour')<br/>rotate_account<br/>retry++"]

    L --> I
    M --> I
    N --> I
    O --> I
    P --> I
    Q --> I

    I -- 3 retries exhausted --> R[raise RuntimeError]
```

**Notes**:
- The `current_account is None` guard at the top of `execute_task` exists
  because a previous task's `rotate_account()` may have raised `NoAccountError`
  (pool empty) and left `current_account = None`. The guard re-acquires (with
  wait, since locked accounts may free up) before falling through to
  `BrowserSession`.
- `rotate_account()` itself: `lock_until('+5 min')` on the current account →
  `release_account()` → set `current_account=None` → `initialize()` to acquire
  a new one (raises `NoAccountError` if pool empty).
- Detector→worker contract: `AccountDisabledError` and `CheckpointError` are
  raised by `_wait_for_log_in_outcome` *after* it has already written the
  account's `error_msg` and flipped `active=false`. The worker's clauses for
  those two **do not** call `mark_inactive` again — they would clobber the
  specific message. `TransientLoginError` and `FailedLoginError` are
  detector-doesn't-write contracts, so the worker's clause writes the DB.

## DB Field Reference

State-bearing columns of the `accounts` table (`db.py:63-168`). Credentials,
proxy, and fingerprint columns are omitted — they don't drive state.

| Column | Type | Meaning | Written by | Read by |
|---|---|---|---|---|
| `active` | BOOLEAN | Account is permitted for scraping. False ⇒ Inactive (banned, disabled, checkpointed). | `set_active()` (200), `mark_inactive()` (406) | `get_available()` SQL (306) |
| `in_use` | BOOLEAN | A worker currently holds the account. | `_get_and_mark_in_use()` (278), `release_account()` (382), `mark_inactive()` (406) | `get_available()` SQL |
| `locks` | JSON | Holds key `$.locked_until` (ISO 8601 datetime) when the account is rate-limited / cooling down. `NULL`/missing ⇒ not locked. | `lock_until()` (234), `unlock()` (256) | `get_available()` SQL, `next_available_at()` (360) |
| `error_msg` | TEXT | Human-readable reason an account became Inactive. Cleared on next successful login. | `set_active()` (200), `mark_inactive()` (406) | logs / admin queries |
| `last_used` | TEXT (ISO 8601) | Updated on every state-changing pool method touch. | `_get_and_mark_in_use`, `release_account`, `lock_until`, `unlock`, `update_scroll_count`, `update_last_used` | `_get_and_mark_in_use` 24h-stale check (see below) |
| `scroll_count_overall_24h` | INTEGER | Scrolls in the current 24 h window; zeroed during acquisition when the *previous* `last_used` was > 24 h ago. | `update_scroll_count()`, `reset_scroll_counts()`, `_get_and_mark_in_use()` | `get_available()` ordering, worker scroll-threshold check (`worker.py`) |
| `scroll_count_per_endpoint_total` | JSON | Per-endpoint scroll totals. Same lifecycle as `_overall_24h`. | `update_scroll_count()`, `reset_scroll_counts()`, `_get_and_mark_in_use()` | scoping per-endpoint quotas |

All file/line references point to `fbscrape/accounts_pool.py` unless prefixed.

### The 24h scroll-count reset lives in acquisition

`_get_and_mark_in_use` stamps `last_used = now` and returns the **post-update**
row (`RETURNING *`, or a `_tx` re-select on SQLite < 3.35). An account's real
idle time is therefore gone the instant it is acquired, before any caller sees
the `Account` object.

That makes acquisition the only place the reset can be applied, so it happens
there, in the same `UPDATE`:

```sql
UPDATE accounts SET
    scroll_count_overall_24h = CASE WHEN <stale> THEN 0 ELSE scroll_count_overall_24h END,
    scroll_count_per_endpoint_total = CASE WHEN <stale> THEN '{}' ELSE … END,
    last_used = datetime(:now, 'unixepoch'),
    in_use = true
WHERE …
-- <stale> := last_used IS NULL OR last_used <= datetime(:now - 86400, 'unixepoch')
```

Every right-hand side in a SET clause reads the pre-update row, so the CASE
guards test the account's *previous* `last_used` even though the same statement
reassigns it. One timestamp is computed for the whole statement so the cutoff
cannot disagree with the value written.

> **KDD — the reset that never fired.** This logic previously sat in
> `login._on_login_success`, guarded by
> `now - session.account.last_used > timedelta(hours=24)`. Because acquisition
> had already overwritten `last_used`, that delta was always ~0 s and the reset
> never ran. `scroll_count_overall_24h` accumulated for the life of the account
> (237k observed on accounts idle for days), so it tracked *lifetime* rather
> than recent load. The pool's default `scroll_count_overall_24h ASC` ordering
> then meant rest could never move an account up the queue — only a manual
> `reset_scroll_counts` could — and a freshly-reset account would absorb an
> entire batch while rested peers sat idle. Covered by
> `tests/unit/test_accounts_pool_scroll_reset.py`.

### Selection order

`AccountsPool._order_by` defaults to
`scroll_count_overall_24h ASC, last_used ASC`. The primary key is recent scroll
load; `last_used` is a tie-break, needed because the reset puts every rested
account at 0 and SQLite would otherwise be free to return the same member of
that tied group every time.

Callers may override via `get_available(order_by=…)`. `Worker.LAST_USED_ORDER_BY`
(`last_used ASC, scroll_count_overall_24h ASC`) inverts the keys for endpoints
that record no scrolls (`ProfileInfo`, `ProfileAbout`, `GroupInfo`,
`GroupAbout`), where the scroll count never moves and so cannot spread load.

## Exception → State-Change Matrix

Every typed exception that affects account state, with the full chain from
detector → worker handler → final state.

| Exception | Raised by | DB written by | Worker handler | End state | retry++ |
|---|---|---|---|---|---|
| `AccountDisabledError` | `login.py:_wait_for_log_in_outcome` on `/checkpoint/disabled/` | detector via `set_active(False, "Account disabled by Facebook (…)")` | `worker.py` → `rotate_account()` only | **Inactive** | **No** (dead account doesn't burn retry budget) |
| `AutomationCheckpointError` | `login.py:_dispatch_login_outcome` on `/checkpoint/<id>/` + page body matches `_is_automation_suspected_checkpoint` ("suspect automated behavior") | none (worker writes) | `worker.py` → `rotate_account(lock_until='+24 hours', error_msg='automation suspected …')` | **Active**, locked +24h | Yes |
| `CheckpointError` | `login.py:_wait_for_log_in_outcome` on other `/checkpoint/` (after automation refinement) | detector via `set_active(False, "Checkpoint challenge — manual intervention required (…)")` | `worker.py` → `rotate_account()` | Inactive | Yes |
| `TransientLoginError` | `login.py login()` — internal retry exhausted, "URL never settled", "viewer never came through", **or a raw playwright `TimeoutError`/`TargetClosedError` from a bare `page.goto`/`reload` in the login flow (reclassified at the `login()` chokepoint — see note below)** | none (account stays Active) | `worker.py:302` → `rotate_account()` | **Active**, locked +5 min by rotate's cooldown | Yes |
| `FailedLoginError` (generic) | `browser_session.py:_resolve_not_logged_in` last-resort when `login()` returns False | none (worker writes) | `worker.py:313` → `mark_inactive()` + `rotate_account()` | Inactive | Yes |
| `AccountBannedError` | scraping methods (e.g., `user_timeline`) | none (worker writes) | `worker.py:326` → `mark_inactive()` + `rotate_account()` | Inactive | Yes |
| `RateLimitError` | scraping methods | none (worker writes) | `worker.py:337` → `lock_until('+1 hour')` + `rotate_account()` | **Active**, locked +1 hour | Yes |
| `NoAccountError` | `pool.get_available_or_wait()` returning None **or** `Worker.initialize()` failing | none | `worker.py:190` (worker_pool's `_worker_loop` catches via generic `except`) | n/a (no account held) | n/a |

> **`login()` transient-nav chokepoint.** The login helpers (`login_with_cookies`, `check_logged_in`) issue several *bare* `page.goto` / `page.reload` calls. A renderer/network flake there raises a raw playwright `TimeoutError` / `TargetClosedError` — which is **not** one of the typed-retry exceptions above, so it would escape untyped out of `execute_task`, get set on the future by `_worker_loop`, re-raise through `FacebookScraper.user_timeline`'s `await future`, and then through `gather()`'s `yield await c` — tearing down the **entire batch** (one bad navigation killing every other handle; observed in production 2026-06-02). `login()` wraps its orchestration in a narrow `except (TimeoutError, TargetClosedError)` that reclassifies *only* those two into `TransientLoginError`, so the worker rotates + retries instead. The catch is deliberately narrow: checkpoint/ban/disabled typed exceptions and the terminal `FailedLoginError` pass straight through, never swallowed.

> **Failed-task isolation (`worker_pool._worker_loop`).** Any exception that still reaches the worker loop (e.g. `RetryBudgetExhaustedError` after 3 rotations, or an unexpected error) is **resolved**, not raised: the generic `except` sets the future to a failed `ScrapingResult` (`result="task_failed: <ExcName>"`, `data=[]`) instead of `set_exception`. This stops one bad handle from propagating through `gather()` and killing the whole batch — the caller records the failure and the pool keeps serving. `NoAccountError` is exempt (keeps its requeue + worker-exit semantics). Partial data isn't preserved (no outcome on exception); failed handles are recoverable via `--continue`.

**Hierarchy** (`exceptions.py`):

```
FacebookScraperError
├── NoAccountError
├── FailedLoginError
│   ├── CheckpointError
│   │   ├── AccountDisabledError
│   │   └── AutomationCheckpointError
│   └── TransientLoginError
├── AccountBannedError
└── RateLimitError
```

The clause order in `worker.execute_task` is **most-specific first**:
`AccountDisabledError → AutomationCheckpointError → CheckpointError →
TransientLoginError → FailedLoginError → AccountBannedError → RateLimitError`.
Reordering will silently break the state-machine — the broader subclass catches
first and clobbers the detector-specific behavior.

## Pool Selection Rules

`get_available()` (`accounts_pool.py:306-323`) is the only path that returns an
account to a worker. Its SQL:

```sql
SELECT COALESCE(email, phone_number) FROM accounts
WHERE active = true
  AND in_use = false
  AND (
       locks IS NULL
    OR json_extract(locks, '$.locked_until') IS NULL
    OR json_extract(locks, '$.locked_until') < datetime('now')
  )
ORDER BY {self._order_by}
LIMIT 1
```

It then atomically marks the row `in_use=true` (in `_get_and_mark_in_use`).

`get_available_or_wait()` (`accounts_pool.py:329-354`) wraps that with a
5-second poll loop. It distinguishes two failure modes:

- **All accounts inactive** (no row satisfies `active=true`): `next_available_at()`
  returns `None` → `get_available_or_wait` returns `None`. Callers (the
  `execute_task` recovery guard) translate that to `NoAccountError`.
- **Active accounts exist but all are locked or in-use right now**: the loop
  blocks, polling every 5 s, logging "No account available. Next available at
  HH:MM:SS" once. Returns the account when one frees up.

Override: setting `FB_RAISE_WHEN_NO_ACCOUNT=1` (or constructing the pool with
`_raise_when_no_account=True`) makes the wait variant raise `NoAccountError`
immediately on any unavailability — useful for CI.

## Cooldowns at a glance

| Reason | Lock duration | Set by |
|---|---|---|
| Post-rotation cooldown | **5 min** | `worker.py:rotate_account()` via `lock_until('+5 minutes')` |
| Rate limit hit during scraping | **1 hour** | `worker.py except RateLimitError` via `lock_until('+1 hour')` |

Lock state lives in the `locks` column under `$.locked_until` (ISO 8601 UTC).
The lock auto-expires — there is no scheduled unlock; the next
`get_available()` query simply selects the row again once `locked_until` has
passed. `unlock()` (`accounts_pool.py:256`) exists for manual admin use; no
runtime path calls it.
