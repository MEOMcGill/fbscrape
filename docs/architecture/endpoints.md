# Endpoint Strategy Deep Dives

Per-endpoint scrape strategy reference. For the compact registry table and wiring checklist, see `CLAUDE.md`. For the new-endpoint onboarding playbook, see [`adding_endpoints.md`](../adding_endpoints.md).

---

## UserTimeline

Targets `ProfileCometTimelineFeedRefetchQuery` (PCTFRQ).

**`mode="manual"`** — scroll-driven.
- Navigates, scrolls in a loop, intercepts natural PCTFRQ responses via `ResponseInterceptor` with `extract_posts=True`.
- Stops on: `start_date` reached, no-new-posts streak, GraphQL silence (`stall_timeout_seconds`), DOM error condition.
- Wraps known-risky awaits in `asyncio.wait_for(operation_timeout_seconds)`.

**`mode="hybrid"`** *(default)* — `page.request.post()` driven.
- Navigates, re-scrolls while waiting for a natural PCTFRQ to capture as a replay template (`_hybrid_wait_for_template(rescroll=True)`). Keeps scrolling deeper every few seconds until the query fires or `template_capture_timeout` (default 45 s) elapses.
- `extract_posts=False` — prevents bootstrap leak (KDD 13).
- Replay loop: `cursor=null` (first iter) or `end_cursor` (subsequent), `beforeTime=min(end_of_day(end_date), now_utc)`, `count=N`. `afterTime` stays `null` (KDD 12).
- Default stop conditions: `EndOfFeed`, `OldestInBatchBelowStartDate` (skipped on iter 1), `NoNewPostsStreak`, `MaxPostsReached`, `CursorReset`, `ResponseShapeError`, `MaxPaginations`, `GraphQLError`.
- `CursorReset` triggers multi-leg resume: advances `end_date` backward, capped at `MAX_CURSOR_RESET_RESUMES` (= 5). See KDD 16.
- `__csr`/`__dyn` spliced from organic scroll bursts every N paginations.

See [`docs/hybrid/overview.md`](../hybrid/overview.md) for empirical evidence.

---

## Search

`mode="hybrid"` only — targets `SearchCometResultsPaginatedResultsQuery` (SCRQ). Same overall shape as UserTimeline hybrid with four differences:

- **URL-filter-driven, no required dates.** Only `query_text` is required. All filtering (sort order, date range, author/source) is optional and baked into the navigation URL as a base64 JSON blob via `_build_search_url`. With no filters, FB returns results under its default ranking. The replay body inherits `args.filters` from the captured template; only `cursor` + `count` are overridden. `inject_before_time=False`. No date-based stop conditions (`OldestInBatchBelowStartDate`, `CursorReset` excluded) — search results are not strictly chronological within a range, so termination is exhaustion/cap-only.
  - Filters are passed as a single `filters` dict. Known names live in `_SEARCH_FILTER_REGISTRY` (each maps a user-facing key → FB outer key, inner `name`, and an `args` encoder): `recent_posts` (sort by latest), `creation_time` (`{"start","end"}` YYYY-MM-DD, one-sided OK), `posts_from` (`{"source": "public"|"me"|"friends"|"groups_and_pages"}`). Unknown keys are passed through verbatim as raw blob entries (`{"city:0": {"name": ..., "args": ...}}`), so new FB filters work without code changes — decode the `filters=` param from a UI URL to discover the shape. Full usage + the add-a-filter recipe: [`../search_filters.md`](../search_filters.md).
- **Different response shape.** SCRQ uses `data.serpResponse.results.edges[]`; stories sit at `edge.rendering_strategy.view_model.click_model.story`. Non-story edges are skipped. Dedicated `parse_search_response` parser; flattener aliases `_flatten_pctfrq_post` (same Comet Story shape once extracted).
- **Non-null initial cursor.** SCRQ's first request carries `cursor={"page_number":0,...}` (JSON string). `_hybrid_capture_template` always sets `template["cursor"]=None`, so `search_hybrid` extracts the real initial cursor from `template["form"]["variables"]` and passes it as `initial_cursor`.
- **No `--continue` support.** No auto-unstick, no resume path. `cursor_reset` is terminal.

Per-page count: 5. `_hybrid_extract_end_cursor` path-length heuristic works: SCRQ's `page_info.end_cursor` lives in the initial non-deferred chunk (path_len 0), which is always shortest.

---

## GroupTimeline

`mode="hybrid"` only — targets `GroupsCometFeedRegularStoriesPaginationQuery` (GCFRSPQ). Three deliberate differences from UserTimeline hybrid:

- **No server-side date filter.** GCFRSPQ has no `beforeTime`/`afterTime` variables. Date bounding is purely client-side. KDD 12 does NOT apply. Both dates are optional (`default_end_to_today=False`). Without dates, termination relies on `MaxPostsReached` / `NoNewPostsStreak` / `EndOfFeed` / `MaxPaginations`.
- **Sort override.** Default `sortingSetting="TOP_POSTS"` (FB UI default; lowest-fingerprint). `CHRONOLOGICAL` correlates with account suspensions on this endpoint. Known-valid values: `TOP_POSTS` (algorithmic; termination via `ConsecutiveOutOfRange`), `CHRONOLOGICAL` (opt-in; descending by creation_time; ban-correlated), `RECENT_ACTIVITY` (by most recent comment/reaction; non-chronological). Stop-condition set adapts to sort.
- **First-batch date-stop guard (CHRONOLOGICAL only).** `OldestInBatchBelowStartDate` skips when `cursor_sent is None` (iter 1) because the bootstrap edge can carry an out-of-order "highlight" post.
- **Cursor-reset uses 2nd-oldest as anchor (CHRONOLOGICAL only).** Prevents false positives from the per-batch bootstrap-edge highlight (~once every 150-200 paginations). Falls back to absolute oldest when batch has < 2 timed posts. Dropped under non-chronological sorts.
- **Auto-unstick on resumed `no_new_posts_streak`.** CLI auto-swaps `last_cursor` to rank-3 chronologically-oldest cursored post. Exposed manually via `fbscrape unstick-cursor`. Helper: `cli._find_unstick_cursor(data, endpoint, rank=3)`.
- **No multi-leg cursor_reset resume.** `cursor_reset` is terminal; partial data preserved.

Other: `handle` accepts vanity or numeric group id; resolves via `/groups/<handle>/`. Per-page count: 3. Response shape: bootstrap line uses `data.node.group_feed.edges[].node = Story` (Shape A); subsequent stream lines use `data.node = Story` (Shape B). Parser fans Shape A into per-Story entries.

---

## CommentsList

`mode="hybrid"` only — targets `CommentsListComponentsPaginationQuery` (CLCPQ). Top-level comments only (`depth=0`). Each record carries `replies_total_count`. Four differences from GroupTimeline hybrid:

- **Response is single-chunk JSON, not JSONL.** CLCPQ doesn't use `@stream`/`@defer`. End-cursor read straight from `page_info.end_cursor`. Termination driven by `page_info.has_next_page: false`.
- **No date filter, no Story shape.** FB orders comments "Most Relevant" (algorithmic). Default stop set: `EndOfFeed`, `NoNewPostsStreak`, `MaxPostsReached` (driven by `max_results`), `MaxPaginations`, `GraphQLError`. Date-bound and cursor-reset stops dropped.
- **Dedicated pagination loop.** `_hybrid_comments_pagination_loop` uses `commentsAfterCursor`/`commentsAfterCount` as pagination variables and `parse_comments_response` as the per-iter parser. `feedLocation` (default `"POST_PERMALINK_DIALOG"`) injected on every replay.
- **No multi-leg cursor_reset resume.**

Other notes:
- **Identifier**: `handle` + `post_id`. `post_id` accepts numeric OR pfbid form. The base64 `feedback:<numeric_post_id>` needed as `variables.id` is captured automatically from the natural CLCPQ request.
- **Per-page count**: -1 (FB picks, ~10 empirically). Override via `--comments-after-count`.
- **Reactions**: `feedback.top_reactions.edges[]` ships `{node:{id}, reaction_count}` without `localized_name`. Flattener maps known ids via `_REACTION_ID_TO_NAME`; unknowns land in `reactions_other`.
- **Filename stem**: `<handle>_<post_id_truncated>_CommentsList_hybrid` (pfbid truncated to 24 chars).

---

## PageTransparency

`mode="hybrid"` only — single-shot, no pagination, no scroll, no date filter. `Query.query` carries `page_id`; `handle` is optional (navigation URL only).

- Navigates to `/<handle or page_id>/`. Skips bootstrap scroll — `ProfileTransparencyDialogQuery` only fires from a UI click.
- Captures `{post_data, headers}` from any natural GraphQL POST (`latest_natural_graphql_request`).
- Synthesizes body: `fb_api_req_friendly_name=ProfileTransparencyDialogQuery`, `variables={"pageID": <page_id>, "scale": 3}`, `doc_id=PAGE_TRANSPARENCY_DOC_ID`. Splices `__csr`/`__dyn`. Header `x-fb-friendly-name` overridden.
- Single `page.request.post()`. HTTP-status classification via `_hybrid_send_replay`.
- Returns `ScrapeOutcome(result='success', data=[transparency_dict])`.

---

## ProfileAuthenticity

Same shape as PageTransparency. `Query.query` carries `user_id` only.

- Navigates to `/<user_id>/`. Skips bootstrap scroll — `ProfileCometDirectoryAuthenticityModalQuery` only fires from a UI click.
- Reuses `latest_natural_graphql_request` template capture.
- Synthesizes body: `fb_api_req_friendly_name=ProfileCometDirectoryAuthenticityModalQuery`, `variables={"scale": <scale>, "userID": <user_id>}`, `doc_id=PROFILE_AUTHENTICITY_DOC_ID`.
- Returns `ScrapeOutcome(result='success', data=[authenticity_dict])`. Top-level fields: `id`, `name`, `delegate_page_id`, nested `profile_directory_authenticity_modal`. Flattener dispatches `header_fields[]` by `profile_field_type`.

---

## Post → ProfileAuthenticity → PageTransparency pipeline

The three endpoints form a natural pipeline when starting from posts and wanting Page-side transparency info. The identifiers do NOT line up automatically:

- `post.author_id` **IS** the `user_id` for `ProfileAuthenticity`.
- `author_id` is **NOT** a valid `PageTransparency.page_id`. PageTransparency needs `delegate_page_id` from `ProfileAuthenticity`.
- `actors[0].__typename` is always `User` on UserTimeline posts even when the account has a linked Page — no post-level signal to skip `ProfileAuthenticity`.

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

`handle=author_id` on the PageTransparency call is for navigation URL warm-up only; the GraphQL body uses `delegate_page_id` as `variables.pageID`. See `README.md` — "Two-stage pipeline" — for an executable example.
