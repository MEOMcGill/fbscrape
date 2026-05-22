# Adding PostgreSQL to fbscrape — learning roadmap

## How to use this plan

Each milestone names **what to build** and **what to learn doing it** — no
code, no SQL snippets, no implementation details. Work through them in order;
ask Claude for an implementation pass (or a focused explanation) only when
needed. When a milestone is done, the reader should be able to explain *why*
each piece exists, not just that it works.

**Decisions already made:**
- Driver: `asyncpg`
- JSON-bearing columns: native `JSONB`
- Both backends supported (SQLite stays, Postgres is added alongside)
- Test infra: docker-compose + `DATABASE_URL` env var

---

## Milestone 0 — PostgreSQL fluency

**Goal:** be comfortable reasoning about a Postgres instance before wiring it
into fbscrape.

**What to do:**
1. Run an ephemeral `postgres:16` container with the official image (env-var
   credentials, host port mapping). Connect with `psql`.
2. In `psql`: list databases, list tables, describe a table, quit. Internalize
   the *server → database → schema → table* hierarchy.
3. Memorize the DSN shape: `postgresql://user:pass@host:port/db`.
4. Hands-on with `JSONB`: create a small table with a JSONB column; query it
   with `->`, `->>`, `@>`; mutate it with `jsonb_set` and `||`. Map each one to
   its SQLite JSON1 equivalent (`json_extract`, `json_set`).
5. Hands-on with `FOR UPDATE SKIP LOCKED`: open two `psql` sessions, both
   `BEGIN` and select the same row with this clause. Convince yourself the
   second session skips what the first locked. This is the single primitive
   that replaces the SQLite `_tx`-column hack in `accounts_pool.py`.
6. Hands-on with `TIMESTAMPTZ` + `INTERVAL` arithmetic. Convince yourself it
   replaces SQLite's `datetime('now', '+24 hours')` string-building.

**Done when:** all 6 steps doable unaided and explainable to someone else.

**Ask for help if:** any of the concepts feel hand-wavy.

---

## Milestone 1 — Persistent local Postgres via docker-compose

**Goal:** a Postgres instance that survives reboots and is shared by every
process on the host that points `DATABASE_URL` at it.

**What to build:**
- A `pg` service in the existing `docker-compose.yml` (don't replace the
  `scraper` service — add alongside).
- Named volume for data persistence.
- Healthcheck so anything that depends on it can wait for readiness.
- A `.env.example` documenting `DATABASE_URL` and the PG credentials.
- `.env` in `.gitignore`.

**What you'll learn:** docker-compose service definitions, named volumes,
healthchecks, environment variable substitution, and how containers on the
same compose network resolve each other by service name.

**Done when:** `docker compose up -d pg` followed by `psql "$DATABASE_URL"`
drops you into a working shell against the container.

**Ask for help if:** you want a starter compose snippet, or to discuss
whether to expose 5432 to the host or keep it on the internal network only.

---

## Milestone 2 — Survey the existing DB layer

**Goal:** before touching code, know exactly what's being refactored. Don't
take a pre-built plan — build the mental model.

**What to do:**
- Read `fbscrape/db.py` end-to-end. Note the migration system, the
  `@lock_retry` decorator, the `DB` context manager, and the four module-level
  helpers (`execute`, `fetchone`, `fetchall`, `executemany`).
- Read `fbscrape/accounts_pool.py`. List every public method on `AccountsPool`
  (there are ~25). For each, note: what SQL it runs, what it returns, whether
  it mutates state.
- Read `fbscrape/account.py`. Pay attention to `from_rs` / `to_rs` — those are
  the (de)serialization boundary that needs to change shape under Postgres.
- Grep for `from .accounts_pool` and `AccountsPool(` across the codebase. The
  call sites are exactly what the abstraction must keep working.
- Pick *one* SQLite-only feature used in `accounts_pool.py` (e.g.
  `json_extract`, `datetime('now', …)`, the `_tx` fallback, `COLLATE NOCASE`)
  and write yourself a one-line note on its Postgres equivalent.

**Done when:** you can draw the call graph on paper and could re-derive a
detailed survey unaided.

**Ask for help if:** you want a second opinion on your mental model, or
you want a walkthrough of the trickier bits (`_get_and_mark_in_use`,
`lock_until` with its SQL-expression `until` parameter).

---

## Milestone 3 — Backend abstraction (SQLite-only at first)

**Goal:** refactor `AccountsPool` so two backends can plug into the same
public API. Zero behavior change in this step — existing tests must pass
unchanged.

**Design decisions:**
- ABC vs. duck-typing — how strict should the contract be?
- Where the URI-dispatch lives (constructor `__new__`, factory function,
  separate `AccountsPool.from_uri(...)` classmethod, etc.).
- What stays on the base class as concrete orchestration vs. what's abstract.
  (Some methods are pure orchestration over other methods and don't need
  per-backend code — find them.)
- How to handle the `NoAccountError` defined in `accounts_pool.py` vs. the
  one in `fbscrape.exceptions` (yes, there are two — decide whether to
  consolidate now or document and defer).

**What you'll learn:** Python ABC patterns, `__new__` for polymorphic
constructors, isinstance contracts, and where the boundary between
"orchestration" and "I/O" really sits in this code.

**Done when:** existing `pytest tests/unit/` passes with no edits to the test
files; `fbscrape account stats` still works against the SQLite DB; and
`isinstance(pool, AccountsPool)` returns True for whatever concrete class the
constructor returned.

**Ask for help if:** you want a second opinion on the abstraction shape
before committing, or want a mechanical implementation pass once the design
is decided.

---

## Milestone 4 — Postgres backend implementation

**Goal:** make `AccountsPool("postgresql://…")` actually work, end-to-end.

**Sub-decisions:**
- How to lazy-init the asyncpg pool (per-instance vs. per-process; when to
  open, when to close).
- How to express the schema — inline `CREATE TABLE` strings, a `.sql` file,
  per-version migration files, an ORM. Pick the simplest thing that lets you
  evolve the schema later.
- How to translate each SQLite SQL string. Teaching density is highest here —
  every method touches at least one PG concept:
  - `json_extract` → `->>` / `->`
  - `json_set` → `jsonb_set` (mind the path-as-text-array vs. dotted-string
    difference)
  - `datetime('now', '+X')` → `NOW() + INTERVAL 'X'` (or just store a
    `TIMESTAMPTZ` and stop hand-building expressions)
  - `:name` placeholders → asyncpg's positional `$1`, `$2`, …
  - The string-interpolated `_identifier_condition` / `_identifiers_condition`
    helpers → parameterized queries (fixes a latent SQLi while you're there).
- How to implement `get_available()` atomically. This is THE example for the
  whole port — design it to use `FOR UPDATE SKIP LOCKED` and notice how the
  entire `_tx`-column dance from SQLite collapses to nothing.
- How to handle the `lock_until(identifier, until, …)` API where `until` is
  currently a *SQL expression string* — a leaky abstraction that doesn't port
  cleanly. Decide whether to translate, change the API to take a `timedelta`,
  or build a thin DSL.

**What you'll learn:** asyncpg's pool + transaction + record model, JSONB
operators in anger, `FOR UPDATE SKIP LOCKED`, why "store the right type"
(`TIMESTAMPTZ`) beats "store text + parse later" for datetimes, and how
parameterized queries close SQL-injection holes that the SQLite code papers
over.

**Done when:** every method on `PostgresAccountsPool` is implemented (no
`NotImplementedError` left) and `fbscrape account stats` / `fbscrape account
list` work against a Postgres `DATABASE_URL`.

**Ask for help if:** any specific SQL translation is unclear, you want a
review of `get_available()` for race-condition correctness, or you'd like an
implementation pass on a stuck method while you work on others.

---

## Milestone 5 — Lifecycle plumbing

**Goal:** the asyncpg pool gets cleanly opened and closed instead of leaking.

**What to think about:**
- Where pool lifecycle belongs (in `FacebookScraper.__aexit__`? In each CLI
  command? Both?).
- Whether the SQLite backend needs a matching no-op `aclose` for symmetry.
- What error message you'd see if this is wrong (run the code without it and
  observe — that's the lesson).

**Done when:** no `Pool was not closed` warnings on CLI exit or pytest
teardown.

**Ask for help if:** you want a pointer to the right hook in `scraper.py` or
`cli.py`.

---

## Milestone 6 — Tests against both backends

**Goal:** the same contract test runs against SQLite and (when `DATABASE_URL`
is set) Postgres, plus one PG-only test that proves `SKIP LOCKED` actually
prevents double-claim under concurrency.

**What to design:**
- How to parameterize a pytest fixture over two backends.
- How `tests/conftest.py` should decide whether to skip PG tests (DATABASE_URL
  unset? unreachable? wrong schema?).
- What the concurrency test looks like — spawn N tasks all calling
  `get_available()`, assert each gets a distinct account or `None`.

**What you'll learn:** parameterized fixtures, write-one-test-run-twice
patterns, and how to prove concurrency correctness with a deterministic-ish
test.

**Done when:** `pytest tests/` is green with no `DATABASE_URL`;
`DATABASE_URL=… pytest tests/` is green with both; the concurrency test
demonstrates the property it claims to.

**Ask for help if:** the concurrency test design is unclear (it has a subtle
race-window-narrowing trick).

---

## Milestone 7 — SQLite → Postgres data migration

**Goal:** one-shot porter so the existing `~/db/accounts.db` accounts move
into the Postgres instance without re-adding them by hand.

**What to think about:**
- Where this lives — a new CLI subcommand under `fbscrape utils …`, or a
  standalone script in `scripts/`.
- Idempotency — what should happen if you run it twice? (PG `INSERT … ON
  CONFLICT DO NOTHING` is the relevant primitive.)
- Type translation — JSON-string columns in SQLite have to become real
  JSONB; ISO-string `last_used` becomes a native `TIMESTAMPTZ`.

**Done when:** running the migrator then `fbscrape account list` against
Postgres shows the same rows as the SQLite original.

**Ask for help if:** you hit a type-conversion edge case.

---

## Milestone 8 — Documentation

**Goal:** future readers (and anyone else on the project) can pick up the PG
backend without re-deriving any of this.

**What to update:**
- `CLAUDE.md` — new section near "Account lifecycle / rotation"; a new Key
  Design Decision entry for "URI-dispatched backend; PG uses FOR UPDATE SKIP
  LOCKED instead of the SQLite `_tx` hack."
- `README.md` — docker-compose quickstart, `DATABASE_URL`, the "running
  against Postgres" usage block.
- `docs/architecture/account_management.md` — append PG-specific notes.
- `docs/postgres.md` (new) — schema rationale, why JSONB over normalized
  tables, the SKIP LOCKED demo, useful psql snippets for inspecting pool
  state.

**Done when:** someone landing on the repo cold could stand up Postgres,
point fbscrape at it, and understand why each piece exists.

---

## Verification — end-to-end check after all milestones

1. **SQLite parity:** `pytest tests/` with no `DATABASE_URL` → green.
   `fbscrape account list` against `~/db/accounts.db` works unchanged.
2. **Postgres works:** `docker compose up -d pg && DATABASE_URL=…
   pytest tests/` → green, including the concurrency test.
3. **Migration:** the SQLite→PG migrator copies the real DB; row count
   matches.
4. **Real scrape against PG:** `DATABASE_URL=… fbscrape scrape user-timeline
   zuck --start-date 2025-01-01 --headless` completes; account state changes
   are visible via `psql`.
5. **Multi-process concurrency:** two simultaneous scrape processes against
   one PG instance never double-claim an account.
6. **Clean exit:** no asyncpg `Pool was not closed` warnings anywhere.

---

## Out of scope (followups, not this plan)

- Hosted Postgres (RDS / Supabase / Neon) — the DSN shape stays the same;
  just point at the hosted DSN when ready.
- Retry / backoff for transient PG outages — add when production exposure
  warrants it.
- Schema-level normalization (promoting `locked_until` out of JSONB into a
  dedicated TIMESTAMPTZ column for indexability) — defer; current JSONB shape
  is parity-preserving across backends.
- The wider job-queue / API / MinIO service — a separate project that depends
  on this one.
- Cleanup of the duplicate `NoAccountError` (one in `accounts_pool.py`, one
  in `fbscrape.exceptions`) — noted, not load-bearing.
