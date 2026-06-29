# TODO / Roadmap

Open work items, ordered roughly by readiness. For completed design decisions see [`docs/design_decisions.md`](../design_decisions.md).

---

## Endpoints

- **Add more endpoints** (EventDiscussion, post-info, etc.). Pattern: one entry in `Query.ENDPOINT_REGISTRY`, per-mode methods on `BrowserSession`, row in `Worker.ENDPOINT_MODE_METHODS`, flattener orchestrator + `ENDPOINT_FLATTENERS`, high-level wrapper on `FacebookScraper`, CLI subcommand, plus full test additions (fixture in `tests/_capture_fixtures.py`, unit flatten test, integration test, `EXPECTED_KEYS` bump). Reference: `GroupTimeline` (paginated, client-side date filter), `Search` (URL-based date filter), `PageTransparency` (single-shot). Playbook: [`docs/adding_endpoints.md`](../adding_endpoints.md).
- **Add an endpoint to get the information of a post.**
- **Search: no-date URL form.** Search is the one paginated endpoint that still requires both dates. FB's search UI lets you pick "any time"; verifying the bare URL still returns paginated results, then wiring it through `_build_search_url` + CLI's `_resolve_targets`, would close the optional-dates story. Pattern: UserTimeline / GroupTimeline `_resolve_targets` calls in `cli.py` + KDD 20.

## Account management

- **Implement ban detection heuristics.**
- **Add proxy rotation / failover.** Per-account proxy is already wired (`BrowserSession._get_proxy_dict()` reads `account.proxy_server` / `proxy_username` / `proxy_password`). Missing: a rotation policy layer — round-robin a pool of proxies independent of accounts, retry a different proxy on connection-class failures, mark proxies dead after N consecutive timeouts.

## Scraping robustness

- **Auto-restart on stall.** Resume primitives already exist (`--continue`, `_stream_resume_state`, `seen_post_ids`). What's missing: when a scrape bails on `no_new_posts_streak` / `ETIMEDOUT` / hang, automatically issue the equivalent of a `--continue` rerun without operator involvement (with a retry cap so a genuinely-empty handle doesn't loop forever).
- **Cursor-reset resume cap counts productive legs the same as stuck legs** (`scraper.py`, `MAX_CURSOR_RESET_RESUMES = 5`). A 10-year scrape getting cursor-reset every ~1 year terminates after 6 years. Fix: track `consecutive_no_progress` instead of total leg count — reset to 0 whenever a leg advances `end_date` by more than ~1 day, cap that counter (e.g. 3). Productive legs continue indefinitely; only true stalls trip the cap.
- **External watchdog task for hang detection.** Today the in-loop stall watchdog can't fire if an `await` itself is stuck. Current mitigation (`operation_timeout_seconds`) wraps known-risky awaits in `asyncio.wait_for`, but any new await added to the loop is unprotected by default. Proper fix: run the scrape loop as a child `asyncio.Task` with a sibling watchdog task that calls `task.cancel()` when conditions fire.
- **Hybrid: mid-scrape session invalidation — richer detection.** HTML-body and auth-error-marker detection both raise `FailedLoginError` already. Stronger: mid-scrape `data.viewer == null` polling on natural GraphQL responses, expanded marker set.
- **Hybrid: GraphQL `errors[]` with partial data — marker set.** `_HYBRID_AUTH_ERROR_MARKERS` list is incomplete; expand as new auth-ish error strings are observed.

## Experiments

- **Hybrid: `freeze_tokens` experiment.** FB's bundled JS strongly suggests `__csr`/`__dyn` are HasteBitMap telemetry, not auth. If empirically validated, drop the live-splicing path and organic-scroll bursts whose only purpose is token refresh. Add `freeze_tokens: bool` to `user_timeline_hybrid`; run a 200+ pagination scrape; success → evidence to simplify.
- **Capture profile-header metadata (`ProfileCometTimelineHeaderQuery`).** Followers, page name, intro/bio, profile pic, cover photo, verified badge already fire on profile navigation — we intercept and discard. Add an extraction branch in `ResponseInterceptor.intercept_response` that stashes the parsed payload on `BrowserSession.profile_info`; surface as a `profile_info` field on `ScrapingResult`. No extra HTTP needed.

## UX / DX

- **Better INFO logging** so it looks cleaner.
- **`flatten --concat`: streaming / bounded-memory path.** Today `--concat` accumulates every record into one `all_rows` list — O(total corpus) RAM. On a 493-file corpus (~28 GB compressed) this OOMs an 18 GB machine. Fix: stream inputs through `iter_posts`, batch-normalize to a bounded buffer, write shards + `pl.concat([scan_parquet(s)...], how='diagonal_relaxed').sink_parquet(out)`. `diagonal_relaxed` handles per-file schema drift. Keep in-memory path for small inputs; switch to streaming above a threshold (or always — `sink_parquet` is strictly safer). See [`docs/proposals/speed_and_memory.md`](speed_and_memory.md).
