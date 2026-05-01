# fbscrape Documentation

Index of the docs in this directory. The top-level `README.md`, `CONTRIBUTING.md`, and `CLAUDE.md` stay at the repo root — start there if you're new to the project.

## Architecture

Reference for someone modifying the core scrape flow.

- [`architecture/overview.md`](architecture/overview.md) — design goals, component breakdown (`FacebookScraper` → `WorkerPool` → `Worker` → `BrowserSession`), data flow, and why concurrency / lazy init / rotation work the way they do.
- [`architecture/account_management.md`](architecture/account_management.md) — account state machine, lifecycle, and which exception → DB-write paths fire account rotation, marking inactive, locking for cooldown, etc.

## Hybrid mode

Reverse-engineering of FB's request shapes. Useful only if you're touching the hybrid scrape path or debugging anti-bot behavior.

- [`hybrid/overview.md`](hybrid/overview.md) — design rules for the production hybrid mode (`mode="hybrid"`): what runs, why each rule (`cursor=null` first replay, `beforeTime` always set, `afterTime=null`, post auto-extraction off, etc.), and open questions / future work.
- [`hybrid/request_anatomy.md`](hybrid/request_anatomy.md) — every form field on a `ProfileCometTimelineFeedRefetchQuery` POST, where each token comes from in FB's JS, how often it rotates, and harvest difficulty per field.
- [`hybrid/token_generation.md`](hybrid/token_generation.md) — deep dive on `__csr` and `__dyn`: HasteBitMap-of-loaded-resources mechanics, `BootloaderConfig.csrOn` toggle, the conditional `delete v.__csr` finding in `RelayFBNetwork`. Reproducer script in `tmp/hybrid/find_token_generators.py`.
- [`hybrid/scroll_flow.md`](hybrid/scroll_flow.md) — capture-driven inventory of "what real scrolling generates that pure replay would not": per-anchor recurring requests, sporadic engagement-driven traffic, page-load-only requests. Primary source for the anti-bot risk story.

## Proposals

Speculative, not describing current behavior.

- [`proposals/speed_and_memory.md`](proposals/speed_and_memory.md) — historical brainstorm of speed/memory improvement options (date filters, GraphQL replay, DOM cleanup, etc.). The hybrid mode was one of these proposals; others remain open.
