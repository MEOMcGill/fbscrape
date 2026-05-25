# Project Context

**Last Updated:** 2026-05-15

`fbscrape` is a Facebook timeline scraper built on Camoufox (stealth Firefox) with persistent SQLite-backed account rotation, support for parallel browser sessions, and two pluggable scrape strategies per endpoint.

This document describes how the codebase works *today*. For evolving design decisions, deeper rationale, and historical research, see [`docs/`](docs/).+

**Notes**:
- if an update is made to the codebase (e.g. an endpoint is added, behavior is modified, or something is remove), update the CLAUDE.md file where necessary and the README.md documentation and the various documentation in the ```docs``` folder so it stays up to date and tracks all changes made to the codebase.
- do not make updates to the codebase unless specifically asked to or given permission to
- **always add tests when adding endpoints or non-trivial logic.** The suite under `tests/` is three-tiered (unit / integration / e2e — see [`tests/README.md`](tests/README.md)) and codifies the public contract; new code without tests rots silently the next time FB shifts shape or the registry grows. Concretely:
  - **New endpoint** — mandatory: capture a fixture in `tests/_capture_fixtures.py` (add to `TARGETS` + `CAPTURERS`, re-run the script), add `tests/unit/test_flatten_<endpoint>.py` modeled on the existing single-shot flatteners, add `tests/integration/test_<endpoint>.py` mirroring the matching `(endpoint, mode)` integration test, and bump `EXPECTED_KEYS` in `tests/unit/test_query_registry.py` (deliberate tripwire on registry growth).
  - **Unit additions** — any new pure function (parser strategy, registry validator, CLI helper, IO codec, flattener aspect-extractor) gets a `tests/unit/test_*.py` covering at minimum: the happy path on a captured fixture, the validation/error path if it raises, and the round-trip / golden-output check if it serializes. Unit tests run on every bare `pytest` invocation, so they're cheap to add and the first to catch regressions.
  - **Behavior changes to existing endpoints** — extend the matching integration test rather than adding a new file; add a unit test if a new pure function was introduced as part of the change.
---

## How the codebase works today

### The data flow

```
caller code
    │
    ▼
FacebookScraper.user_timeline(handle, start_date, end_date, mode="hybrid", **params)
    │
    │  builds a Query from the (endpoint, mode) registry; validates;
    │  fills param defaults
    ▼
WorkerPool.submit_task(query) → asyncio.Future
    │
    │  lazily inits N workers (N = min(max_workers, active_accounts));
    │  each worker pulls (query, future) tuples off a shared queue
    ▼
Worker.execute_task(query)
    │
    │  acquires/owns an account; creates a fresh BrowserSession per task;
    │  catches typed exceptions and decides rotation policy
    ▼
BrowserSession.user_timeline_{mode}(...)
    │
    │  per-mode method dispatched via Worker.ENDPOINT_MODE_METHODS;
    │  performs the actual scrape; returns ScrapeOutcome (Query-agnostic)
    ▼
Worker composes ScrapingResult.from_outcome(query, outcome)
    │
    ▼
Future resolves; caller awaits the result
```

### Two scrape strategies for `UserTimeline`

Both produce identical `ScrapingResult` shape — caller code doesn't care which one was used.

**`mode="manual"`** — scroll-driven path.
- Navigates to the profile, scrolls in a loop, intercepts natural `ProfileCometTimelineFeedRefetchQuery` (PCTFRQ) responses via `ResponseInterceptor`, populates posts via auto-callback (`extract_posts=True`).
- Stops on: target start_date reached, no-new-posts streak, GraphQL silence (`stall_timeout_seconds`), DOM error condition (private profile, etc.).
- Wraps known-risky awaits in `asyncio.wait_for(operation_timeout_seconds)` to bound renderer-wedge damage.

**`mode="hybrid"`** *(default)* — page.request.post() driven path.
- Navigates, fires one bootstrap scroll to provoke a natural PCTFRQ, captures its form body + headers as a replay template (via `ResponseInterceptor.latest_pctfrq_request`).
- Disables post auto-extraction (`extract_posts=False`) so unfiltered natural responses don't leak off-range posts into the result.
- Replay loop: every replay sends `cursor=null` (first iter) or `end_cursor` (subsequent), `beforeTime=min(end_of_day(end_date), now_utc)`, `count=N`. `afterTime` stays at the captured `null` (matches FB's UI; including it is a fingerprint).
- Stop conditions: drives via pluggable `StopCondition` objects (see [`fbscrape/stop_conditions.py`](fbscrape/stop_conditions.py)). Default set for UserTimeline/Search includes: (a) `EndOfFeed` (`end_cursor` null in response), (b) `OldestInBatchBelowStartDate` (oldest post in batch < `start_unix`; skipped on iter 1 — first batch's bootstrap edge can be an out-of-order highlight; see GroupTimeline section), (c) `NoNewPostsStreak` (no-progress streak hits `max_no_progress_streak`), (d) `MaxPostsReached` (`max_posts` cap; batch-boundary, over-delivers by up to `pagination_count - 1`; default `-1` = disabled), (e) **`CursorReset`** (oldest post jumps newer by > `HYBRID_CURSOR_RESET_JUMP_SECONDS`, signaling FB silently degraded the response stream — see Key Design Decision 16), (f) **`ResponseShapeError`** — parser found posts but every one had an unrecognized timestamp metadata-strategy typename, terminal/non-retryable (see Key Design Decision 18), (g) `MaxPaginations` safety cap, (h) `GraphQLError` (non-auth, non-rate-limit in-body `errors[]`; dumps forensic window). Auth / rate-limit errors short-circuit inline before the framework walk (typed-exception raise / `'rate_limit'` return).
- Token splicing: `__csr` / `__dyn` from any natural GraphQL POST (organic scroll bursts every N paginations refresh them) overridden into replay bodies.
- HTTP errors mapped to typed exceptions (see *Account lifecycle* below). 5xx retried with backoff.

See [`docs/hybrid/overview.md`](docs/hybrid/overview.md) for empirical evidence behind each design rule.

### Paginated scrape strategy for `GroupTimeline`

`mode="hybrid"` only — targets `GroupsCometFeedRegularStoriesPaginationQuery` (GCFRSPQ). Same overall shape as UserTimeline hybrid (navigate → bootstrap scroll → capture template → replay loop), with three deliberate differences:

- **No server-side date filter.** GCFRSPQ variables carry no `beforeTime` / `afterTime` — date bounding is purely client-side via parser-extracted `creation_time` vs. `start_unix`. `end_date` is therefore advisory in this endpoint (it only bounds the stop check; FB always returns from the current head of feed when cursor is null). Key Design Decision 12 (always-set `beforeTime`) does NOT apply. **Both dates are also optional** — see KDD 20: GroupTimeline's CLI defaults are `require_start=False, require_end=False, default_end_to_today=False`, mirroring FB UI's "no date filter at all" fingerprint. When both are omitted the scrape relies on `MaxPostsReached` / `NoNewPostsStreak` / `EndOfFeed` / `MaxPaginations` for termination.
- **Sort override (TOP_POSTS default).** FB's UI default is `sortingSetting="TOP_POSTS"` (algorithmic ranking), and the hybrid path injects exactly that into every replay body via `_hybrid_pagination_loop(static_variable_overrides=…)`. **This is the lowest-fingerprint choice and the empirically-validated safer option for sustained scraping** — `CHRONOLOGICAL` correlates with FB suspending the account on this endpoint (verified: UserTimeline runs for hours without bans, GroupTimeline+CHRONOLOGICAL does not). Callers can override via the `sorting_setting` param / `--sorting-setting` CLI flag. Known-valid values: `"TOP_POSTS"` (default — algorithmic ranking; termination via `ConsecutiveOutOfRange` stop condition since posts arrive non-monotonically by creation_time), `"CHRONOLOGICAL"` (opt-in only — stream-line tail descending by post `creation_time`, closer to true creation-time order but ban-correlated; per-batch bootstrap edge sometimes out of order, looks like a "highlight" slot FB injects regardless of sort), `"RECENT_ACTIVITY"` (sorts by most recent comment/reaction — treated as non-chronological). The default stop-condition set adapts to the sort — see [`fbscrape/stop_conditions.py`](fbscrape/stop_conditions.py) `assemble_default_stop_conditions`.
- **First-batch date-stop guard (CHRONOLOGICAL only).** Because the first batch's bootstrap edge can carry an out-of-order "highlight" post (e.g. CHRONOLOGICAL mode dropped a 2026-05-10 post into batch 1 alongside two 2026-05-14 stream-line posts), the `OldestInBatchBelowStartDate` stop condition skips the check when `cursor_sent is None` (iter 1). Once the cursor advances past iter 1, FB anchors its responses to that cursor's chronological position and subsequent bootstrap edges fall back in line with the stream tail. Shared with UserTimeline (which gets at most one wasted pagination if its `start_date` falls inside the first batch). Not relevant under TOP_POSTS / RECENT_ACTIVITY since `OldestInBatchBelowStartDate` is dropped from the default set on non-chronological sorts.
- **Cursor-reset detector uses 2nd-oldest as anchor (CHRONOLOGICAL only).** The same bootstrap-edge highlight pattern fires the cursor-reset detector (Key Design Decision 16) — observed empirically as a +9.7-day "oldest jumped newer" trigger every ~150-200 paginations on long scrapes, even though FB wasn't actually degrading the stream. The fix is endpoint-scoped in the `CursorReset` stop condition: when `state.endpoint == "GroupTimeline"`, it compares the per-batch `second_oldest_in_batch` against the prior anchor; falls back to absolute oldest when a batch has fewer than 2 timed posts. UserTimeline / Search continue to use absolute oldest. The detector warning log includes an `anchor=2nd_oldest|oldest` annotation so it's clear which anchor fired. Dropped entirely from the default set under non-chronological sorts (the chronological-monotonicity premise doesn't hold).
- **Auto-unstick on resumed `no_new_posts_streak`.** A `--continue` resume can bail on `no_new_posts_streak` when the saved cursor anchors at a position where FB serves only already-collected posts (verified empirically: GroupTimeline cursors are valid 24h later — proven by `tmp/cursor_validity_experiment/` — but a stuck cursor at a dedup-saturated position produces deterministic 0-progress loops). When this happens during a `--continue` scrape (CLI `scrape group-timeline` / `scrape user-timeline`), the CLI auto-swaps `last_cursor` to the rank-3 chronologically-oldest cursored post in the merged data before saving — picking a chronologically-deeper anchor that's almost always outside the dedup set. The next `--continue` then resumes into uncovered territory. The same logic is exposed manually via `fbscrape unstick-cursor <paths>` (`--rank N` to tune depth, `--only-if-stuck` to skip clean files, `--dry-run` to preview). Helper: `cli._find_unstick_cursor(data, endpoint, rank=3)`. Skipping rank-1 dodges the bootstrap-edge highlight outlier; falls forward chronologically if the rank-N post is a cursorless bootstrap-edge.
- **No multi-leg cursor_reset resume.** UserTimeline can advance `end_date` backward on a cursor_reset because `beforeTime` filters server-side; GroupTimeline has no such handle. `cursor_reset` is terminal: partial data is preserved on `ScrapingResult.result`, no fresh leg is submitted.

Other notes:
- `handle` accepts either a vanity group handle (e.g. `"albertaseparatism"`) or the numeric group id; both resolve via `/groups/<handle>/`. The canonical numeric id is captured automatically from the natural request's `variables.id` and inherited into the replay body.
- Per-page count is 3 (matches FB UI). Bootstrap scroll is required (GCFRSPQ does not fire on raw navigation).
- Response shape: each line carries a fanned-out subset of stories. The bootstrap line uses `data.node.group_feed.edges[].node = Story` (Shape A variant); subsequent stream lines use `data.node = Story` directly (Shape B). `FacebookGraphQLParser.parse_timeline_response` now fans Shape A into per-Story entries so every Story flows through `_flatten_grouptimeline_post` (a thin alias over `_flatten_pctfrq_post` — same Comet Story shape).

### Single-shot scrape strategy for `PageTransparency`

`mode="hybrid"` only — there is no pagination, no scroll, no date filter. `Query.query` carries the numeric `page_id`; `handle` is optional (when supplied, it's used for the navigation URL; otherwise FB redirects `/<page_id>/` to the canonical page).

- Navigates to `https://www.facebook.com/<handle or page_id>/` (warm-up + natural traffic). Skips bootstrap scroll — the natural `ProfileTransparencyDialogQuery` only fires from a UI click, so waiting for it is pointless.
- Captures `{post_data, headers}` from *any* natural GraphQL POST (`ResponseInterceptor.latest_natural_graphql_request`). Cross-cutting auth-bearing fields (`fb_dtsg`, `lsd`, `__user`, `__csr`, `__dyn`, etc.) are session-wide, not per-query.
- Synthesizes the transparency body by overriding `fb_api_req_friendly_name=ProfileTransparencyDialogQuery`, `variables={"pageID": <page_id>, "scale": 3}`, `doc_id=PAGE_TRANSPARENCY_DOC_ID` on the captured template; everything else inherits. Splices freshest `__csr` / `__dyn` (same as the paginated paths). Header `x-fb-friendly-name` is overridden to match the form field.
- Single `page.request.post()` to `/api/graphql/`. HTTP-status classification reuses `_hybrid_send_replay` (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → bounded retry).
- Returns `ScrapeOutcome(result='success', data=[transparency_dict])` — `data` is a 1-element list, not a post stream.

### Single-shot scrape strategy for `ProfileAuthenticity`

Same shape as `PageTransparency` — single-shot, no pagination, no scroll, no date filter. `Query.query` carries the numeric `user_id` only; no handle is needed (FB redirects `/<user_id>/` to the canonical profile).

- Navigates to `https://www.facebook.com/<user_id>/` (warm-up + natural traffic). Skips bootstrap scroll — the natural `ProfileCometDirectoryAuthenticityModalQuery` only fires from a UI click on FB's "About this profile / authenticity" modal.
- Reuses the same `latest_natural_graphql_request` template-capture path as PageTransparency (cross-cutting auth-bearing fields are session-wide).
- Synthesizes the authenticity body by overriding `fb_api_req_friendly_name=ProfileCometDirectoryAuthenticityModalQuery`, `variables={"scale": <scale>, "userID": <user_id>}`, `doc_id=PROFILE_AUTHENTICITY_DOC_ID`. Splices freshest `__csr` / `__dyn`. Header `x-fb-friendly-name` is overridden to match the form field.
- Single `page.request.post()` to `/api/graphql/`. HTTP-status classification reuses `_hybrid_send_replay`.
- Returns `ScrapeOutcome(result='success', data=[authenticity_dict])` — the dict is `data.user` from the GraphQL response. Top-level fields include `id`, `name`, `delegate_page_id`, and a nested `profile_directory_authenticity_modal` with `header_fields[]` (profile join date, profile-updated-since, category, transparency link), `meta_verified_section`, and `about_fields[]`. The flattener dispatches `header_fields[]` by `profile_field_type` (`PROFILE_JOIN_DATE` → `profile_join_date`, `PROFILE_UPDATED_SINCE` → `profile_updated_since`, `CATEGORY` → `category`, `TRANSPARENCY` → `transparency_present` bool).

### Post → ProfileAuthenticity → PageTransparency chain

The three endpoints `UserTimeline`, `ProfileAuthenticity`, and `PageTransparency` form a natural pipeline when you start from posts and want Page-side transparency info, but the input/output identifiers DON'T line up the way the type system suggests:

- **`UserTimeline` post's `author_id` IS the `user_id`** that `ProfileAuthenticity` takes. The post-flattener writes the same numeric id under both names (`actors[0].id` / `feedback.owning_profile.id`). The only edge case is legacy short-id accounts (Zuck = `4`); for those the post-level `author_id` and the modern `ProfileAuthenticity.user_id` live in different namespaces and don't cross-reference. Modern 15-digit ids (`100…`, `61…`) are safe.
- **`author_id` is NOT a valid `PageTransparency.page_id`.** PageTransparency expects the *linked Page's* id, which differs from the User id for any personal-profile-with-linked-Page account. Passing the user_id to PageTransparency returns `data.page = null` → `result="parse_error"`. The Page id only comes from `ProfileAuthenticity.delegate_page_id` (or a separately-sourced Page id, e.g. from a FB UI URL).
- **`actors[0].__typename` is always `User` on UserTimeline posts** — even when the account has a linked Page. Empirically: 142/161 distinct authors in the canadian-fb-slop dataset have a populated `delegate_page_id` (so they're Page-backed), yet 100% of their posts report `author_type == "User"`. FB doesn't expose the linked-Page distinction at the post layer. There is no post-level signal to short-circuit the ProfileAuthenticity step — you have to call it on every author_id and let `delegate_page_id` decide whether PageTransparency is applicable.

So the pipeline is:

```
post.author_id  ──►  ProfileAuthenticity(user_id=author_id)
                                │
                                ▼
                         delegate_page_id?
                          ╱            ╲
                  populated            null
                       │                  │
                       ▼                  ▼
       PageTransparency(             stop — plain User,
       page_id=delegate_page_id,     no linked Page
       handle=author_id)
```

The `handle=author_id` on the PageTransparency call is purely for the navigation URL warm-up; the GraphQL body sent to FB carries `delegate_page_id` as `variables.pageID`. See [`README.md`](README.md) — "Two-stage pipeline: user_id → page_id → transparency" — for the executable example.

### Endpoint × mode registry (`Query.ENDPOINT_REGISTRY`)

Single source of truth for what scrape requests are valid:

```
ENDPOINT_REGISTRY[endpoint] = {
    "query_required": [field, ...],          # required keys in `query`
    "modes": {
        mode_name: {
            "params": {param_name: default}, # default = None means required
        },
    },
}
```

Today's registered endpoints: `UserTimeline` (`manual` + `hybrid`), `Search` (`hybrid` only), `GroupTimeline` (`hybrid` only — paginated, client-side date filter), `PageTransparency` (`hybrid` only — single-shot, no pagination), `ProfileAuthenticity` (`hybrid` only — single-shot, no pagination). Adding a new endpoint = one nested dict entry + a `BrowserSession` method per mode + a row in `Worker.ENDPOINT_MODE_METHODS` + a flattener entry + test additions (fixture capture, unit flatten test, integration test, registry tripwire bump — see top-of-file Notes and [`tests/README.md`](tests/README.md)). Full playbook in [`docs/adding_endpoints.md`](docs/adding_endpoints.md).

`Query.__post_init__` validates the endpoint, mode, required query fields, and param keys; fills defaults from the registry. Unknown params raise `ValueError`.

### Key types (`models.py`)

- `Query(endpoint, mode, query, params)` — the scrape spec. Validated at construction. `query` carries required fields (`handle`, `start_date`, `end_date`); `params` carries mode-specific tunables.
- `ScrapeOutcome(result, data, time_started, time_taken)` — Query-agnostic outcome from `BrowserSession`. Doesn't know which Query produced it. `data` is `list[dict]` always — post-stream endpoints (UserTimeline / Search) populate one element per post; single-record endpoints (PageTransparency) populate a 1-element list.
- `ScrapingResult(query, result, data, time_started, time_taken)` — final result. Composed by `Worker` via `ScrapingResult.from_outcome(query, outcome)` so the canonical Query is constructed exactly once and never rebuilt downstream. (Saved JSON files use `"data": [...]` on the wire; the CLI `flatten` / `download-media` loaders also accept the legacy `"posts": [...]` shape from pre-rename outputs.)

### Account lifecycle / rotation (`Worker.execute_task` + `accounts_pool.py`)

`Worker` owns an account, creates a fresh `BrowserSession` per task, and catches typed exceptions to decide rotation policy:

| Exception | Action | Counts as retry? |
|---|---|---|
| `AccountDisabledError` | rotate (no retry burn) | no |
| `AutomationCheckpointError` | lock 24h + rotate (account stays active) | yes |
| `CheckpointError` | rotate + retry | yes |
| `TransientLoginError` | rotate (account stays active) | yes |
| `FailedLoginError` | mark inactive + rotate | yes |
| `AccountBannedError` | mark inactive + rotate | yes |
| `RateLimitError` (HTTP 429) | lock 24h + rotate | yes |
| `NoAccountError` | put task back; stop | — |

Hybrid mode raises these from `_hybrid_send_replay` based on HTTP status (401 → `FailedLoginError`, 403 → `AccountBannedError`, 429 → `RateLimitError`, 5xx → bounded retry then bail) and from response-body shape (HTML body or auth-ish errors[] → `FailedLoginError`).

`AutomationCheckpointError` is raised by the login flow only: when FB redirects to `/checkpoint/<id>/` AND the page body contains "We suspect automated behavior on your account", `_dispatch_login_outcome` in `login.py` peeks at the page text and refines the generic `CheckpointError` into this subclass. Unlike other checkpoint variants the account is NOT marked inactive — the worker catches the typed exception and calls `rotate_account(lock_until='+24 hours', error_msg='automation suspected …')` so the account is recoverable after the lock expires. Must be caught BEFORE `CheckpointError` in `Worker.execute_task` due to subclass ordering.

In-body GraphQL rate-limits (HTTP 200 + `errors:[{code:1675004, message:"Rate limit exceeded", severity:"CRITICAL"}]`) are caught separately inside `_hybrid_pagination_loop` via `_hybrid_is_rate_limit_error` (matches FB's internal code 1675004 first, then falls back to substring on the message). The loop returns the result string `'rate_limit'` with partial data preserved; `Worker.execute_task` special-cases it to lock the account 24h + rotate, mirroring the HTTP-429 path but without consuming a retry slot (the task completes cleanly with whatever was collected before the limit fired). Verified manually: FB throttles at the account-cookie level, not the request, so locking the account for an extended window is the correct response.

Other in-body GraphQL errors (HTTP 200 + `errors[]` that aren't auth or rate-limit — e.g. `"A server error field_exception occured"`) bail with the result string `'graphql_error: <msg>'` and partial data preserved. The loop also dumps the rolling iter window + the errored iter to `tmp/hybrid/graphql_error/<handle>/<UTC_ts>/` (mirrors the `cursor_reset` dump structure: `window.jsonl` with prior iterations and `summary.json` with the structured `{message, code, severity}` and trigger pagination index) so the full request/response can be inspected post-incident. `Worker.execute_task` does NOT rotate or burn a retry slot on `graphql_error` — the condition signals an FB-side or shape issue, not an account problem.

Account rotation has a 5-minute cooldown lock to prevent immediately re-acquiring the same account.

Independently of exception-driven rotation, `Worker` also rotates pre-task when its current account has accumulated ≥ `scroll_threshold` scrolls (default 500). The counter (`Worker.scroll_count`) is endpoint-agnostic and scoped to a single account-ownership: it sums `BrowserSession.scrolls_recorded` from every fresh session this worker spun up under the current account, and zeroes on `initialize` / `close` / `rotate_account`. DB scroll columns (`scroll_count_per_endpoint_total`, `scroll_count_overall_24h`) keep updating via `record_scroll` but are NOT consulted for the rotation decision — they're cumulative-lifetime and would over-count across worker instances; their concrete consumer is `AccountsPool._order_by = "scroll_count_overall_24h ASC"` for account-selection prioritization. See Key Design Decision 23.

### `ResponseInterceptor` state (`response.py`)

Set up on every `BrowserSession`. Hooks into `page.on("response")`. Tracks:

- `posts: list[dict]` — accumulator. Auto-populated by `parse_timeline_response` when `extract_posts=True` (default; manual mode keeps it on, hybrid disables).
- `add_posts(posts)` — public API hybrid uses to append manually-parsed replay results.
- `graphql_request_count`, `last_response_time` — drive the manual-mode stall watchdog.
- `viewer_seen` — `True` once any GraphQL response body contains a non-null `data.viewer` (canonical login-success marker, doesn't depend on DOM).
- `latest_csr` / `latest_dyn` — freshest tokens parsed from any natural GraphQL POST (manual replays via `page.request.post` bypass the page event stream and don't pollute these). Hybrid splices into replay bodies.
- `latest_pctfrq_request` / `latest_scrq_request` / `latest_gcfrspq_request` — `{post_data, headers}` of the most recent natural PCTFRQ / SCRQ / GCFRSPQ. UserTimeline / Search / GroupTimeline hybrid poll the matching field for template capture.
- `latest_natural_graphql_request` — `{post_data, headers}` of the most recent natural GraphQL POST regardless of friendly-name. Used by single-shot endpoints (PageTransparency, ProfileAuthenticity) that synthesize their own body and only need cross-cutting auth-bearing fields.
- `network_capture` — full request+response of every observed response. **Off by default**; opt-in via `FB_NETWORK_CAPTURE_ALL=1` env var. Used for offline forensic analysis (see `tmp/hybrid/`).
- `flush()` — resets transient state between scrapes; preserves `extract_posts` (it's a behavior flag, not transient state).

### Post flattening (`response.py` + `cli.py flatten`)

Raw `ScrapingResult.data` records are deeply nested GraphQL trees. `FacebookGraphQLParser.flatten(record, endpoint)` collapses one record into a row dict; the CLI `flatten` command writes a directory of these as csv / jsonl / parquet.zstd via polars.

**Layered architecture.** Per-aspect `_extract_*` methods on `FacebookGraphQLParser` each consume a Story dict and return a partial dict (ids/urls, times, audience, author, message+entities, music, flags, engagement, top_comments, attachments, shared_post). Endpoint orchestrators (`_flatten_<endpoint>_post`) compose them into the full row. Most FB surfaces share the same Story shape (the "Comet" UI), so adding a new endpoint flattener is mostly an orchestration call plus a registry entry.

**Endpoint registry.**
```python
FacebookGraphQLParser.ENDPOINT_FLATTENERS = {
    "UserTimeline":         "_flatten_pctfrq_post",
    "GroupTimeline":        "_flatten_grouptimeline_post",
    "PageTransparency":     "_flatten_pagetransparency_record",
    "ProfileAuthenticity":  "_flatten_profile_authenticity_record",
    # Search: "_flatten_search_result_post" later
}
```
The CLI reads `query.endpoint` from the saved JSON and routes through this registry; `--endpoint` overrides it.

**Field coverage (UserTimeline orchestrator).** Per row: ids/urls/times, privacy, author (id/name/url/type/promode_badge), text + `hashtags` / `mentions` / `external_urls` (extracted from `message.ranges` typed entities), `music_artist`/`music_title` (when posted with audio attribution), `is_reel`/`is_live`/`is_repost`, full reaction breakdown (like/love/haha/wow/sad/angry/care + total), shares, comments, video_views, video_duration_sec, `top_comments` (list of dicts), `attachments` (recursive list — see below), `shared_post` (recursive dict — abbreviated by FB so usually only ids/urls/times/author).

**Uniform attachment shape.** Every attachment fills the same keys regardless of type — type-specific extras get None when not applicable. Types: `photo`, `video`, `link`, `album`, `reel_share`, `unavailable`, `unknown`. Photos/album-covers carry `image_url` (high-res) + `image_lowres_url`; videos carry `video_url` (progressive mp4) + `thumbnail_url` + `video_permalink_url` + `video_duration_sec` + `video_captions_url`; link previews carry `link_title`/`link_description`/`link_source`/`link_destination_url` + a `thumbnail_url`. Albums recurse via `subattachments[]` (mixed-media albums populate `video_url` on video subnodes); reel shares hoist the inner reel's permalink/thumbnail/duration/video_url to the outer attachment for ergonomic single-level access. `download-media` consumes this shape directly via `FacebookGraphQLParser._extract_attachments`, so URL discovery stays in one place.

**Metadata dispatch by `__typename`.** `comet_sections.context_layout.story.comet_sections.metadata[]` is a non-deterministic list of typed strategies. `_metadata_by_typenames(story, typenames)` dispatches by typename rather than positional index. Each constant is a **tuple of candidate typenames** (`_METADATA_TIMESTAMP_TYPENAMES`, `_METADATA_AUDIENCE_TYPENAMES`, `_METADATA_MUSIC_TYPENAMES`) checked in order — first match wins. This absorbs FB's sibling strategy renames without code changes: e.g. the timestamp surface returns `CometFeedStoryLongerTimestampStrategy` for some renderings and `CometFeedStoryMinimizedTimestampStrategy` for others, with an identical `story.creation_time` payload. New strategies (location, sponsored, …) plug in either as new constants or as extra entries in an existing tuple.

**Output flow (CLI).** Rows go through `pl.json_normalize(rows, separator='__', infer_schema_length=None)` so nested dicts (`shared_post.*`) become `__`-separated columns; lists stay typed (`pl.List[pl.Struct]`). Parquet writes natively with `compression='zstd'`. CSV serializes List/Struct cells as JSON strings (round-trippable via `json.loads`). JSONL writes the raw pre-normalized row dicts to preserve original nesting.

### Optional debug capture

Set `FB_NETWORK_CAPTURE_ALL=1` to record every browser response (XHR + JS + CSS + images, with binaries metadata-only) into `ResponseInterceptor.network_capture`. Dump to JSONL via `save_network_capture_to_jsonl(path)`. Off by default to keep production memory tight; hybrid does **not** rely on it.

---

## Key Design Decisions

1. **Email OR Phone** — accounts identified by either, enforced by SQL CHECK constraint. `Account.identifier` returns email if present, else phone.
2. **GraphQL viewer detection over DOM** — login status checked by intercepting the first GraphQL response with non-null `data.viewer`. No DOM polling; no race against page render.
3. **Human-like typing** — login form keystrokes use normal-distribution delays (`mean=100ms, std=30ms`).
4. **Cookies saved before browser close** — context errors otherwise corrupt the save.
5. **`wait_until="domcontentloaded"`** — Facebook's load event never fires on infinite-scroll feeds.
6. **Lock for lazy init** — `asyncio.Lock` around `WorkerPool` lazy-init prevents `gather()`-induced double-init.
7. **5-minute rotation cooldown** — `lock_until` on rotation prevents immediately re-acquiring the same account.
8. **Stall watchdog on GraphQL silence (manual only)** — keyed off `last_response_time`. Fires whether FB silences the endpoint or returns empty bodies. Hybrid uses different stop conditions (`end_cursor` null, oldest post < `start_unix`, no-progress streak).
9. **Self-signed fbcdn URLs, short TTL** — media URLs in scraped posts carry an `oh=` HMAC signature and an `oe=` expiry (hex-encoded unix seconds); no cookies/auth needed to fetch them. Empirically the TTL is **~4-5 days from scrape time**, not 30 — verified across 12k URLs from a 2026-05 scrape batch (uniform 4d for ~91%, 5d for ~9%, across all hosts and media kinds). Expired URLs return HTTP 403 with `Bad URL hash`. Practical consequences: (a) run `download-media` within ~3 days of the scrape or pipeline it into the same run, (b) raw post JSONs older than ~5 days are media-irrecoverable without re-scraping the handle. The `time_started` in each saved file's outer dict marks the issue moment; per-URL expiry can be decoded with `int(url.split('oe=')[1].split('&')[0], 16)`.
10. **Endpoint × mode registry** — `Query.ENDPOINT_REGISTRY` is the single source of truth for endpoints, modes, and per-(endpoint, mode) param defaults. Per-mode methods on `BrowserSession` (`user_timeline_manual`, `user_timeline_hybrid`) handle dispatch via `Worker.ENDPOINT_MODE_METHODS`.
11. **Query is constructed exactly once** — `FacebookScraper.user_timeline` builds the canonical Query, validates, fills defaults. `BrowserSession` returns a `ScrapeOutcome` (Query-agnostic); `Worker` attaches the original Query via `ScrapingResult.from_outcome`. No drift between caller-spec and recorded-spec.
12. **Hybrid: `cursor=null` + `beforeTime` always set** — empirically confirmed FB's UI uses `cursor=null` whenever a date filter is active. Replays mirror that: every replay carries a non-null `beforeTime` (= `min(end_of_day(end_date), now_utc)`) so FB honors `cursor=null` on the first replay, returning the most-recent in-range batch including SSR-equivalent posts. `afterTime` stays at the captured `null` (FB's UI never sets it). See [`docs/hybrid/overview.md`](docs/hybrid/overview.md).
13. **Hybrid: post auto-extraction off** — `ResponseInterceptor.extract_posts = False` for the duration of a hybrid scrape. Prevents the natural bootstrap PCTFRQ (no date filter) from leaking off-range posts into the result. All posts come from replays with explicit filters.
14. **`__csr` / `__dyn` are HasteBitMap telemetry, not auth** — from FB's bundled JS, both are bitmaps of bootloaded resources / dynamic JS modules. FB's own request builder will conditionally `delete v.__csr` before sending. We currently live-splice the freshest values from natural GraphQL POSTs; the `freeze_tokens` experiment in TODOs validates whether splicing matters.
15. **Per-call timeouts wrap renderer-prone awaits** — every `scroll()`, `check_error_conditions()`, `page.request.post()`, etc. is wrapped in `asyncio.wait_for(operation_timeout_seconds)` so a wedged renderer can't hang a scrape forever. Per-call patch; the proper fix (external watchdog task) is in TODOs.
16. **Hybrid: cursor-reset detection + multi-leg resume** — empirically, after a variable number of paginations (median ~33 across 21 dumped scrapes, range 2–214) FB silently returns a degraded response: `has_next_page: true` and a fresh `end_cursor`, but the response shape collapses (e.g. 28 JSONL → 4 lines, ~5× smaller body) and the oldest creation_time jumps newer with zero post overlap vs. the prior batch. Cursor handoff is intact and there are no `errors[]` — neither existing stop fires, so the loop would run forever against a degraded stream. Detector lives in `_hybrid_pagination_loop`: if the per-batch `detector_anchor` jumps newer by more than `HYBRID_CURSOR_RESET_JUMP_SECONDS` (= 7 days) vs. the previous iter, dump a 20-iter rolling window to `tmp/hybrid/cursor_reset/<handle>/<UTC_ts>/` and return `'cursor_reset'`. **Endpoint-aware anchor**: UserTimeline / Search use absolute `oldest_in_batch`; **GroupTimeline uses 2nd-oldest** because its batches carry a per-batch bootstrap-edge "highlight" outlier (an FB-injected anchor post often chronologically out of order vs. the cursor's real position) that would otherwise trip ~once every 150-200 paginations as a false positive. Both anchors are extracted via `_hybrid_iter_wrapping_creation_times`, which routes through the parser's `_extract_times` (same wrapping-only metadata-strategy lookup the flattener uses — it walks the timestamp typenames in `_METADATA_TIMESTAMP_TYPENAMES`, currently `Longer` + `Minimized`), so a recent share of an older post yields only the share's own date — no false-positive triggers from `attached_story.creation_time`. The diagnostic dump records both `oldest_unix` and `detector_anchor_unix` so post-incident forensics see both. `Worker.execute_task` recognizes the cursor_reset result string and locks the account for 30 min, then `FacebookScraper.user_timeline` resumes via a fresh `WorkerPool.submit_task` with `end_date` advanced backward to the oldest collected post's day, capped at `MAX_CURSOR_RESET_RESUMES` (= 5) resumes per high-level scrape. (GroupTimeline does NOT multi-leg-resume — it has no server-side date filter; cursor_reset is terminal with partial data preserved.) Cross-leg post-id dedup prevents boundary-day duplicates. Terminal result strings: `'cursor_reset_max_retries'`, `'cursor_reset_no_progress'` (oldest post day didn't advance past current end_date), `'cursor_reset_no_posts'` (reset on a leg that collected nothing). Diagnostic dump stays on across legs to keep observing the symptom.
17. **Post-id dedup at `ResponseInterceptor.add_posts`** — `add_posts` filters by `post_id` against `self.seen_post_ids` before appending. The auto-extract path in `intercept_response` routes through `add_posts`, so manual mode benefits identically. Without dedup, FB cursor-degraded responses (which can re-serve overlapping posts) would inflate `len(self.posts)` and let the no-progress backstop fail to fire. Posts without a `post_id` are appended as-is — defensive against parser quirks.
18. **Response-shape error is terminal, not retryable** — when the hybrid loop sees `posts_in_resp > 0` but `oldest_in_batch is None` (the parser accepted posts as Story-shaped, but every one carried a timestamp metadata-strategy typename outside `_METADATA_TIMESTAMP_TYPENAMES`), the loop returns `'response_shape_error'`. `Worker.execute_task` recognizes this result string and **preserves partial data, does not rotate, does not mark inactive, does not burn a retry slot** — the condition signals a structural bug (FB shape change / unrecognized strategy typename), not an instance-specific failure, so rotating accounts wouldn't help. `FacebookScraper.user_timeline`'s multi-leg loop terminates naturally because it only resumes on `'cursor_reset'`. The typed exception `ResponseShapeError` exists in `exceptions.py` as a signal for callers that want to pattern-match, but the loop returns a result string rather than raising so the partial-data flow stays consistent with other terminal classifications (`ETIMEDOUT`, `cursor_reset`, etc.).
19. **Pluggable stop-condition framework + sort-aware defaults** — `_hybrid_pagination_loop` builds a `StopState` snapshot per iter and walks an ordered list of `StopCondition` objects (see [`fbscrape/stop_conditions.py`](fbscrape/stop_conditions.py)). Each condition is stateful per scrape (counters / `prev_anchor` live on `self`) and returns `None` to continue or a result-string to terminate. Default sets are assembled per (endpoint, mode, sorting_setting) by `assemble_default_stop_conditions`. Auth GraphQL errors and in-body rate-limits are short-circuited BEFORE the framework walk because they map to non-result-string side effects (raise typed exception / return `'rate_limit'`). **Sort default flip for GroupTimeline**: changed from `CHRONOLOGICAL` to `TOP_POSTS` — empirically validated that CHRONOLOGICAL correlates with FB suspending accounts on this endpoint (UserTimeline runs for hours unaffected; GroupTimeline+CHRONOLOGICAL does not). TOP_POSTS matches FB's UI default and is the lowest-fingerprint choice. **Sort-aware defaults**: chronologically-ordered responses (UserTimeline, Search, GroupTimeline+CHRONOLOGICAL) get `OldestInBatchBelowStartDate` + `CursorReset`; non-chronological sorts (GroupTimeline+TOP_POSTS / +RECENT_ACTIVITY) drop those (premises don't hold under non-monotonic ordering) and rely on `ConsecutiveOutOfRange(20)` instead — bail after N posts in a row outside `[start_unix, end_unix]`. `ConsecutiveOutOfRange` is also enabled on GroupTimeline+CHRONOLOGICAL as belt-and-suspenders against bootstrap-edge highlights. Programmatic callers can override the entire set by passing `stop_conditions=[...]` to `_hybrid_pagination_loop`; the CLI exposes the most common knobs as flags (`--max-consecutive-out-of-range`, `--max-no-progress-streak`, `--max-paginations`, `--max-posts`, `--sorting-setting`). The completeness tradeoff under TOP_POSTS is intentional: FB surfaces what its algorithm chooses, so the scraper is best-effort coverage, not exhaustive.
20. **Optional dates per endpoint, FB-UI-fingerprint-aligned defaults.** `start_date` / `end_date` are optional for `UserTimeline` and `GroupTimeline` (dropped from `query_required`); `Search` still requires both pending the no-date URL form. Per-endpoint policy mirrors what FB's own UI sends on the wire so the scraper's fingerprint matches a real user navigating to the same surface:
    - **UserTimeline** — FB's UI always sends `beforeTime` in PCTFRQ replays. So `end_date` auto-fills to today UTC at the CLI layer (`default_end_to_today=True`) and the BrowserSession injects `beforeTime` per Key Design Decision 12. `start_date` has no wire equivalent (FB never sends `afterTime`); it stays None when omitted and the date-bounded stops (`OldestInBatchBelowStartDate`, `ConsecutiveOutOfRange`) no-op via their existing None guards. Direct API callers can pass `end_date=None` explicitly to opt out of `beforeTime` injection.
    - **GroupTimeline** — FB's UI sends no date filter on group feeds (GCFRSPQ has no `beforeTime` / `afterTime` variable at all). Both dates stay None when omitted (`default_end_to_today=False`) — sending an arbitrary today-default would be a fingerprint deviation. The hybrid loop already runs with `inject_before_time=False` regardless.
    - **Search** — FB's UI defaults to "any time" (no date filter blob in URL), but `_build_search_url` currently requires both dates baked into the filter. Until the no-date URL form is verified + plumbed, Search keeps `require_start=True, require_end=True` and `default_end_to_today=True` at the CLI layer (status quo). See the corresponding TODO.

    **Filename consequence.** Saved JSON stems are `<handle>_<endpoint>_<mode>.json{,.gz}` — no date segment ever, regardless of what dates were passed. Helpers `_build_stem()` and `_existing_output_for_stem()` in `cli.py` centralize this so `--continue` / `--skip-existing` match on a single stem per (handle, endpoint, mode) — a rolling archive across runs. The actual scrape parameters live in the saved file's `query.query` field; users inspect there to recall what was scraped. This intentionally trades per-run uniqueness for resumability across days (you can `--continue` a scrape a week later under different date bounds without renaming files).

    **Cursor_reset multi-leg interaction.** `FacebookScraper.user_timeline`'s multi-leg cursor_reset resume engages only when `end_date is not None`. With no upper bound there's nothing to advance backward — cursor_reset becomes terminal with partial data preserved (mirrors GroupTimeline's policy).

21. **Hybrid: `end_cursor` extraction is chunk-path aware, not first-match** — FB's @stream/@defer pagination responses are JSONL streams of patches; each chunk declares the response-tree location it plugs in at via a top-level `path` field. The page-level pagination cursor (the only one FB's pagination resolver will accept on the next replay) lives in the chunk at the shortest `path` — `["node","group_feed"]` for GroupTimeline, `["node","timeline_list_feed_units"]` for UserTimeline. Nested attachments (Reels mini-feed, in-stream video ad, etc.) ship their OWN `page_info.end_cursor` values in chunks at much deeper paths (5+ elements). Empirically (May 2026, 45/50 graphql_error dumps from a foreign-engagement-farming scrape), FB's stream order puts the Reels-attachment deferred chunks BEFORE the page-level `page_info` chunk whenever a post in the batch contains a Reel, so a naive "first non-empty `end_cursor`" extractor silently picked the 90-char Reels sub-stream cursor instead of the 528-char group-feed cursor. Sending the Reels cursor back as `variables.cursor` on the next `GroupsCometFeedRegularStoriesPaginationQuery` triggers a server-side `field_exception` on `node.group_feed.edges` (response: empty edges + null page_info + `errors[]`), the loop bails via the `GraphQLError` stop condition with partial data preserved. Fix: `_hybrid_extract_end_cursor` walks each JSONL chunk, collects every `end_cursor` along with the chunk's `path`-length (treats absent path as length 0 for the initial non-deferred chunk), and returns the cursor from the shortest-path chunk; falsy cursor at the shortest path is the legitimate end-of-feed signal (returns None). The rule is endpoint-agnostic and strictly more correct than first-match: in any response where first-match was right, shortest-path picks the same value; the two only diverge in the bug cases. Why the bug is invisible in manual scrolling: FB's own Relay client routes each `end_cursor` back to its own connection's refetch query — it never sends a Reels cursor to the group-feed query. Why the bug is rare for UserTimeline: personal-profile posts seldom contain `fb_shorts_story` attachments, so most PCTFRQ responses have only one `end_cursor` to choose from.

22. **Resume reads are streamed, not loaded; post-scrape merge runs off the event loop.** `--continue` re-reads saved scrape files in two distinct places, both of which used to block on multi-minute `json.load`s of hundreds-of-MB gzipped files:

    - **Pre-scrape (resume state).** `scraper._stream_resume_state` uses ijson (yajl2_c backend) to walk only the `last_cursor` scalar and per-record `post_id` (mirroring `node.post_id` → top-level `post_id` precedence), avoiding materialization of the full posts array. `user_timeline` / `group_timeline` invoke it via `asyncio.to_thread` so per-file decompression happens off the event loop and across-target reads parallelize on the default thread pool. Without this, a batch of 12 `--continue` targets froze for ~3 minutes before any browser opened. Verified ~1.8× per-file (31.6s → 17.5s on a 283 MB / 7452-post file).

    - **Post-scrape (merge + save).** `cli._finalize_continue_result` consolidates the prior load + `prior + new` concatenation + auto-unstick + compressed save into one synchronous helper, dispatched via `asyncio.to_thread` + `asyncio.create_task` per yielded result. After the gather loop, `await asyncio.gather(*finalize_tasks)` joins them all before the scraper context exits. Without the dispatch, a single ~10k-post merge blocked the next yielded result behind a 6-7 minute `json.load` + `gzip.open('wt')` write, turning concurrent target completions into hours of serialized post-processing. The merge itself still uses stdlib `json.load` (it needs every prior record to rewrite the file), so per-file time is unchanged — the win is across-target overlap. Caveat: stdlib `json.load` holds the GIL during decode, so cross-thread parallelism is bounded by GIL contention on the parse phase; gzip decompression (zlib, C) and ijson (yajl2_c, C) both release the GIL, so a future "stream prior records via ijson" optimization could lift that ceiling.

    The on-disk format is unchanged.

23. **Scroll-based rotation reads a per-session counter, not the DB.** `Worker` rotates when its current account has accumulated ≥ `scroll_threshold` scrolls. The counter is per-account-ownership (zeroed on rotation / initialize / close), endpoint-agnostic, and fed by `BrowserSession.scrolls_recorded` — a per-session integer bumped in `record_scroll` alongside the existing DB write. Worker reads `session.scrolls_recorded` after each task (a natural per-task delta since BrowserSession is fresh per task) and adds it to `self.scroll_count`. The DB columns are NOT a rotation signal: `scroll_count_per_endpoint_total` is cumulative-lifetime and `scroll_count_overall_24h` doesn't actually roll on 24h, so reading either as a running total over-counts across worker instances and across runs. Pre-fix the worker did `self.scroll_count += await session.get_scroll_count(task.endpoint)` where the right-hand side was the DB lifetime total — every task on the same endpoint counted every prior task's scrolls again, growing quadratically. DB scroll tracking is preserved as-is for its real consumer: `AccountsPool._order_by = "scroll_count_overall_24h ASC"` prioritizes low-scroll accounts when selecting from the pool.

For account state, lifecycle, and exception → DB-write semantics: [`docs/architecture/account_management.md`](docs/architecture/account_management.md).

For deeper hybrid-mode design rules and open questions: [`docs/hybrid/overview.md`](docs/hybrid/overview.md).

For speed/memory improvement proposals: [`docs/proposals/speed_and_memory.md`](docs/proposals/speed_and_memory.md).

For onboarding a new endpoint (paste-in checklist + wiring touch points): [`docs/adding_endpoints.md`](docs/adding_endpoints.md).

---

## File Structure

```
fbscrape/
├── __init__.py          # Package exports
├── account.py           # Account dataclass with identifier property
├── accounts_pool.py     # SQLite account management with locking
├── browser_session.py   # Browser lifecycle, login, scrape methods (manual + hybrid)
├── cli.py               # Click-based CLI
├── db.py                # Database with migration system
├── downloaders.py       # Async media downloader (images, videos, thumbnails)
├── exceptions.py        # Custom exceptions
├── logger.py            # Loguru-based logging
├── models.py            # Query, ScrapeOutcome, ScrapingResult; ENDPOINT_REGISTRY
├── response.py          # ResponseInterceptor + FacebookGraphQLParser (parse_timeline_response; layered _extract_* + ENDPOINT_FLATTENERS)
├── scraper.py           # FacebookScraper high-level API
├── stop_conditions.py   # Pluggable StopCondition framework + assemble_default_stop_conditions + dump helpers
├── utils.py             # Helpers (gather, cookies, etc.)
├── worker.py            # Worker for account lifecycle + dispatch (ENDPOINT_MODE_METHODS)
└── worker_pool.py       # WorkerPool for concurrency (Future-based)
```

---

## Usage Examples

### High-Level API (recommended)

```python
from fbscrape import FacebookScraper, gather

async with FacebookScraper(db="db/accounts.db", max_browser_sessions=2) as scraper:
    handles = ["zuck", "meta"]

    async for result in gather(
        scraper.user_timeline(h, "2024-01-01", "2025-01-01")  # mode="hybrid" by default
        for h in handles
    ):
        print(f"{result.query.query['handle']}: {len(result.data)} posts")
        result.save(f"output/{result.query.query['handle']}.json")
```

To force the scroll-driven path or override hybrid params:

```python
result = await scraper.user_timeline(
    "zuck", "2024-01-01", "2025-01-01",
    mode="manual",
    stall_timeout_seconds=600,  # manual-only
)

result = await scraper.user_timeline(
    "zuck", "2024-01-01", "2025-01-01",
    mode="hybrid",
    pagination_count=10,
    scroll_burst_every=5,
    max_paginations=200,
)
```

### Low-Level API (`BrowserSession`)

Direct callers must use the per-mode method (`user_timeline_manual` or `user_timeline_hybrid`) and bypass Query validation — pre-validate inputs yourself if you care:

```python
from fbscrape.browser_session import BrowserSession
from fbscrape.accounts_pool import AccountsPool

pool = AccountsPool("db/accounts.db")
account = await pool.get_available()

async with BrowserSession(account, pool, headless=True) as session:
    outcome = await session.user_timeline_hybrid("zuck", "2024-01-01", "2025-01-01")
    print(f"Scraped {len(outcome.data)} posts")

await pool.release_account(account.identifier)
```

Returns `ScrapeOutcome` (no `query` field). To get a `ScrapingResult`, build a `Query` yourself and call `ScrapingResult.from_outcome(query, outcome)`.

### CLI

Account management:
```bash
fbscrape account add --phone +1234567890 --password secret123
fbscrape account add --email user@example.com --password secret123
fbscrape account list -v
fbscrape account stats
```

Scraping:
```bash
# Defaults to --mode hybrid
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01

# Multiple handles, parallel
fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --end-date 2025-01-01 \
  --headless --max-sessions 2

# Force scroll-driven path
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01 \
  --mode manual --stall-timeout-seconds 600

# Tune hybrid pagination
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01 \
  --pagination-count 10 --scroll-burst-every 5 --max-paginations 200

# Read targets from a file (csv / parquet / yaml/yml / json / jsonl/ndjson)
fbscrape scrape user-timeline --input-file targets.csv
fbscrape scrape user-timeline --input-file handles.yaml --start-date 2024-01-01

# Search (hybrid only)
fbscrape scrape search 'mark carney' --start-date 2025-01-01 --end-date 2025-12-31
fbscrape scrape search --input-file queries.csv

# Group timeline (hybrid only — handle accepts vanity OR numeric group id;
# end_date is advisory since FB has no server-side date filter for group feeds)
fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape group-timeline 787909081545196 --start-date 2024-01-01
fbscrape scrape group-timeline --input-file groups.csv --headless

# Page transparency (single-shot — pass bare `page_id` args (or `handle:page_id`
# when you have the vanity handle), or --input-file with a `page_id` column
# (handle column optional); no date range)
fbscrape scrape page-transparency 899800046546098
fbscrape scrape page-transparency habsfanhub:899800046546098
fbscrape scrape page-transparency --input-file pages.csv --headless

# Profile authenticity (single-shot — pass bare user_id args or --input-file
# with a `user_id` column; no date range, no handle needed)
fbscrape scrape profile-authenticity 100044331674441
fbscrape scrape profile-authenticity --input-file profiles.csv --headless
```

`--input-file` recognizes the columns `handle` (required), `start_date`, and `end_date`; everything else is ignored. If the file supplies `start_date` / `end_date` for any row, the matching CLI flag (`--start-date` / `--end-date`) must NOT be set. Rows missing `start_date` fall back to the `--start-date` flag; rows missing `end_date` fall back to `--end-date` and ultimately to today (UTC).

Hybrid-mode flags (all optional, fall back to registry defaults): `--pagination-count`, `--scroll-burst-{every,min,max}`, `--max-paginations` (default `-1` = no cap), `--pagination-sleep-{mean,std}`, `--template-capture-timeout`, `--post-nav-sleep-seconds`, `--request-timeout-ms`, `--max-no-progress-streak`, `--operation-timeout-seconds`. `--stall-timeout-seconds` is manual-only.

Post-processing (both `flatten` and `download-media` accept `.json` or `.json.gz`,
single file or directory of either):
```bash
# Flatten raw JSON into tabular dataset. Endpoint inferred from saved query.endpoint;
# overridable with --endpoint. Parquet uses zstd (auto-derived filenames carry the
# .parquet.zstd suffix; explicit --output filenames are honored literally). CSV
# serializes list/struct columns as JSON strings (round-trippable via json.loads).
fbscrape flatten data/posts/foo.json --format all
fbscrape flatten data/posts/foo.json.gz --format parquet
fbscrape flatten data/posts/2025-06-01_2026-02-17/ --format parquet
fbscrape flatten data/posts/foo.json --endpoint UserTimeline --format jsonl

# --output may be a file path or a folder. For directory inputs it must be a folder
# unless --concat is set (in which case all inputs are merged into one file).
# Heuristic: existing dir or trailing "/" → folder; .parquet/.csv/.jsonl suffix → file;
# otherwise → folder (created if absent).
fbscrape flatten data/posts/ --output data/flat/ --format parquet           # per-file → folder
fbscrape flatten data/posts/ --output data/merged.parquet --concat          # all → one file
fbscrape flatten data/posts/ --output data/all.parquet --concat --format all  # → all.{csv,jsonl,parquet}

# Download images/videos/thumbnails. URLs expire ~4-5 days after scrape — run within
# ~3 days or expect HTTP 403 ("Bad URL hash") on the stale subset.
fbscrape download-media data/posts/foo.json --include-thumbnails
fbscrape download-media data/posts/2025-06-01_2026-02-17/ --concurrency 12
```

Developer utilities:
```bash
# Inspect a cURL copied from DevTools "Copy as cURL". Default output is a
# structured GraphQL summary (method/URL, friendly_name, doc_id, decoded
# variables JSON, key non-telemetry headers). Cookie / fb_dtsg / lsd /
# jazoest are redacted by default.
fbscrape utils parse-curl "curl 'https://www.facebook.com/api/graphql/' -X POST ..."

# Show every header and body field (including __dyn/__csr/__hsdp telemetry).
fbscrape utils parse-curl "curl ..." --full

# Disable redaction (Cookie / fb_dtsg / lsd / jazoest pass through verbatim).
fbscrape utils parse-curl "curl ..." --raw
```

---

## TODO / Future Work

- Add more scraping endpoints (EventDiscussion, etc.). Pattern documented in [`docs/adding_endpoints.md`](docs/adding_endpoints.md): one new entry in `Query.ENDPOINT_REGISTRY`, per-mode methods on `BrowserSession`, row in `Worker.ENDPOINT_MODE_METHODS`, flattener orchestrator + `ENDPOINT_FLATTENERS` row, high-level wrapper on `FacebookScraper`, CLI subcommand, **plus test additions** (fixture in `tests/_capture_fixtures.py`, unit flatten test, integration test for each mode, `EXPECTED_KEYS` bump — see top-of-file Notes). Use `GroupTimeline` (paginated, client-side date filter), `Search` (paginated, URL-based date filter), or `PageTransparency` (single-shot) as reference depending on response shape.
- **Search: no-date URL form.** Search is the one paginated endpoint that still requires both dates (`query_required = ["query_text", "start_date", "end_date"]`). FB's search UI lets you pick "any time" which strips the date filter blob from the URL; verifying that the bare URL still returns paginated results, then wiring it through `_build_search_url` + the CLI's `_resolve_targets` (Search keeps `require_start=True, require_end=True`), would close the optional-dates story across all paginated endpoints. Pattern to follow: the UserTimeline / GroupTimeline `_resolve_targets` calls in `cli.py` + Key Design Decision 21 below.
- Implement ban detection heuristics.
- **Add proxy rotation / failover.** Per-account proxy is already wired (`BrowserSession._get_proxy_dict()` reads `account.proxy_server` / `proxy_username` / `proxy_password` and hands them to Playwright). Missing: a rotation policy layer — e.g. round-robin a pool of proxies independent of accounts, retry a different proxy on connection-class failures, mark proxies dead after N consecutive timeouts. Today an account is permanently bound to one proxy via DB columns.
- **Capture profile-header metadata (`ProfileCometTimelineHeaderQuery`).** Followers, page name, intro/bio, profile pic, cover photo, verified badge live in this query, which already fires automatically on profile navigation — we intercept it and discard it. Add an extraction branch to `ResponseInterceptor.intercept_response` that detects the friendly-name and stashes the parsed payload on `BrowserSession.profile_info`; surface it as a new `profile_info` field on `ScrapingResult`. No extra HTTP needed. About-tab fields (page creation date, category, contact info) are a separate follow-up — they live in `CometProfileTabAbout*` queries that we never trigger today (would need either a dedicated `page.request.post` replay or a brief nav to `/<handle>/about`).
- **Auto-restart on stall.** Across-session dedupe + cursor-based resume primitives already exist (`--continue` flag, `_stream_resume_state`, `seen_post_ids` seeded from the prior file on resume — KDD 22, KDD 17). What's missing is the *orchestration*: when a scrape bails on `no_new_posts_streak` / `ETIMEDOUT` / `hang`, automatically issue the equivalent of a `--continue` rerun without operator involvement (with a retry cap so a genuinely-empty handle doesn't loop forever). Today the operator has to re-invoke the CLI.
- **Cursor-reset resume cap counts productive legs the same as stuck legs** (`scraper.py`, `MAX_CURSOR_RESET_RESUMES = 5`). The current `for leg_idx in range(MAX_CURSOR_RESET_RESUMES + 1)` terminates after 5 resumes regardless of whether each leg was making real progress. A long scrape (e.g. 10 years back) that gets cursor-reset every ~1 year of pagination would terminate after covering only ~6 years even though every leg was advancing the frontier and pulling fresh posts. The existing `cursor_reset_no_progress` early-stop only catches degenerate non-advancing legs; it does NOT distinguish "making progress slowly" from "stuck forever." Fix: track `consecutive_no_progress` instead of total leg count — reset to 0 whenever a leg advances `end_date` by more than a small threshold (e.g. >1 day), cap that counter (e.g. 3). Productive legs would then continue indefinitely; only true stalls trip the cap.
- **External watchdog task for hang detection.** Today the in-loop stall watchdog can't fire if an `await` itself is stuck — both the watchdog code and the hung await live in the same task. Current mitigation (`operation_timeout_seconds`) wraps known-risky awaits in `asyncio.wait_for`, but it's a per-call patch — any *new* await we add in the loop is unprotected by default. Proper fix: run the scrape loop as a child `asyncio.Task` with a sibling watchdog task that owns the GraphQL-silence + wall-clock checks and calls `task.cancel()` when conditions fire. Cancellation breaks any pending await regardless of where it's stuck.
- **Hybrid: mid-scrape session invalidation — richer detection.** HTML-body and auth-error-marker detection both raise `FailedLoginError` already, and `data.viewer` is captured at login (`ResponseInterceptor.viewer_seen`). Stronger detection (mid-scrape `data.viewer == null` polling on natural GraphQL responses, marker-set expansion) is still on the roadmap.
- **Hybrid: GraphQL `errors[]` with partial data — marker set.** We drain posts before bailing, but the `_HYBRID_AUTH_ERROR_MARKERS` list is incomplete; expand as new auth-ish error strings are observed.
- **Hybrid: `freeze_tokens` experiment.** FB's bundled JS strongly suggests `__csr` / `__dyn` are HasteBitMap telemetry, not auth tokens — `RelayFBNetwork` will conditionally `delete v.__csr`. If empirically validated, drop the live-splicing path and the organic-scroll bursts whose only purpose is token refresh. Add a `freeze_tokens: bool` param to `user_timeline_hybrid` that captures the tokens once from the bootstrap template and never updates them; run a 200+ pagination scrape; if it succeeds we have evidence to simplify.
- Add an endpoint to get the information of on a post
- Better INFO logging so it looks cleaner