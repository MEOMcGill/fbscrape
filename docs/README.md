# fbscrape Documentation

Index of the docs in this directory. The top-level `README.md`,
`CONTRIBUTING.md`, and `CLAUDE.md` stay at the repo root — start there if you're
new to the project.

## Endpoints

One guide per endpoint — inputs, scrape strategy, every option, usage recipes,
and gotchas.

- [`endpoints/`](endpoints/README.md) — index and the eleven guides
  (UserTimeline, Search, GroupTimeline, CommentsList, PostDetail,
  PageTransparency, ProfileAuthenticity, ProfileInfo, ProfileAbout, GroupInfo,
  GroupAbout).

## Reference

- [`results_and_errors.md`](results_and_errors.md) — what a scrape returns, the
  exception → Worker-action table, and what a `gather()` loop yields vs. raises.
- [`search_filters.md`](search_filters.md) — Search filter dict/CLI usage and
  how to add a new filter.

## Media

- [`media_streaming.md`](media_streaming.md) — collecting media *during* a
  scrape instead of after it: the `runtime_options` → per-batch-hook chain, the
  immediate vs. manifest-handoff sinks, the manifest line format, filename
  authority, and the cost model. Read this before touching `downloaders.py` or
  the hook firing points.

## Architecture

Reference for someone modifying the core scrape flow.

- [`architecture/overview.md`](architecture/overview.md) — design goals,
  component breakdown (`FacebookScraper` → `WorkerPool` → `Worker` →
  `BrowserSession`), data flow, and why concurrency / lazy init / rotation work
  the way they do.
- [`architecture/account_management.md`](architecture/account_management.md) —
  account state machine, lifecycle, and which exception → DB-write paths fire
  account rotation, marking inactive, locking for cooldown, etc.

## Adding endpoints

- [`adding_endpoints.md`](adding_endpoints.md) — playbook for onboarding a new
  scrape endpoint: the touch points across registry / browser session / worker /
  scraper / interceptor / parser / CLI, plus the required test artifacts.
