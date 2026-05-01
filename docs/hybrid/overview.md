# Hybrid Mode — Design Notes

**Status:** ✅ Shipped as `mode="hybrid"` in `fbscrape.user_timeline`. The original "GraphQL replay viability" investigation concluded GO; this doc keeps only the design rules and open questions for future maintenance.

## What hybrid does

- One bootstrap scroll on the profile page provokes a natural `ProfileCometTimelineFeedRefetchQuery` (PCTFRQ).
- That request's form body + headers are captured as a **template** for replay (static tokens: `fb_dtsg`, `lsd`, `doc_id`, the `__relay_internal__pv__*` flags).
- All subsequent paginations are sent via `page.request.post()` directly. Each replay overrides only `cursor`, `beforeTime`, and `count` in the variables; `afterTime` stays at the captured `null`.
- Posts come **only** from replays. Auto-extraction is turned off (`ResponseInterceptor.extract_posts = False`) so the natural PCTFRQ — which has no date filters — cannot leak off-range posts into the result.

See `BrowserSession.user_timeline_hybrid` and the `_hybrid_*` helpers; the boundary between hybrid-private and shared utilities is documented at the top of that method block.

## Empirical findings the design rests on

These are the observed behaviors that drove specific design choices.

### `cursor=null` is FB's own first-replay pattern when a date filter is active

Captured request shapes (from a logged-in test session):

- **Natural scroll, no filter:** `cursor=<long base64 blob>`, `beforeTime=null`, `afterTime=null`.
- **UI date filter applied (any past date):** `cursor=null`, `beforeTime=<unix>`, `afterTime=null`.
- **UI filter set to "today":** `cursor=null`, `beforeTime=int(datetime.now(UTC).timestamp())`, `afterTime=null`.

So the rule from FB's frontend is: *first request of a filtered range ⇒ `cursor=null`*. We mimic this — every hybrid replay always carries a non-null `beforeTime`, so we always start replay 1 with `cursor=null`. This sidesteps the "duplicate first batch" problem (previously we replayed the natural request's cursor, re-fetching its posts), and gets us the SSR-equivalent batch (most-recent in-range posts) that we'd otherwise miss if we started at the natural request's cursor.

### `afterTime` is never set by the UI; including it is a fingerprint

FB's date-filter UI only exposes a "before this date" control. Every captured natural PCTFRQ has `afterTime=null` regardless of what the user picks. Sending `afterTime=<unix>` in our replay would be a request shape no real user produces. Hybrid leaves it at the captured `null` and enforces the lower bound **client-side** by terminating when a batch's oldest post is older than `start_unix`.

### `beforeTime` derivation matches FB's UI

`beforeTime = min(end_of_day(end_date), now_utc)` — for past `end_date`, end-of-day UTC; for `end_date=today`, the literal current second. Matches FB's UI behavior exactly.

### `__csr` / `__dyn` are HasteBitMaps, not auth tokens

From decompiled FB JS bundles (see `OcNdXjPtAj9.js` for the bootloader and the giant `…BUZLYQhP0_ecvQsd7unsS.js` for `RelayFBNetwork`):

```js
HasteBitMapName: { CSR: "__csr", HSDP: "__hsdp", HBLP: "__hblp", SJSP: "__sjsp" }
keys: { jsmod_key: "__dyn", ... }

// in the bootloader
t.c && r("BootloaderConfig").csrOn && o("HasteBitMap").add("__csr", i)

// in RelayFBNetwork (the request builder)
if (f && delete v.__csr, ...
```

So `__csr` is a bitmap of resource IDs the client has bootloaded; `__dyn` is the bitmap of dynamic JS modules. They "rotate" as new chunks load, not because FB regenerates them on a timer. The fact that FB's own `RelayFBNetwork` will outright `delete v.__csr` from the form before sending strongly suggests they're telemetry/diagnostics, not validating tokens.

We currently still live-splice the freshest values from any natural GraphQL POST that lands during a scrape (organic scroll bursts every N paginations refresh them). The `freeze_tokens` experiment in CLAUDE.md TODOs is the test for whether this is even necessary.

## Production design rules

Concrete defaults live in `Query.ENDPOINT_REGISTRY[("UserTimeline", "hybrid")]["params"]`. The behavioral contracts:

1. **Auto-extraction off for the duration of the scrape** (`response_interceptor.extract_posts = False`). Token tracking, viewer detection, and `latest_pctfrq_request` capture all keep working.
2. **Replay variable overrides**: `cursor` (null on first; `end_cursor` of prior response thereafter), `beforeTime`, `count`. Nothing else.
3. **Termination conditions** (any one ends the loop):
   - `end_cursor` missing / null in response → no more posts in the filter range.
   - Oldest post `creation_time` in batch < `start_unix` → walked past the lower bound.
   - `max_no_progress_streak` consecutive paginations returned no new posts (default 5).
   - `max_paginations` cap reached (default `-1` = no cap).
4. **HTTP error handling** (`_hybrid_send_replay`):
   - 200 + HTML body → `FailedLoginError` (session bounced to login).
   - 200 + auth-ish `errors[]` → `FailedLoginError` (drain posts first).
   - 200 + non-auth `errors[]` → bail with result string, posts drained.
   - 401 → `FailedLoginError`. 403 → `AccountBannedError`. 429 → `RateLimitError`.
   - 500/502/503/504 → bounded retry with backoff (5s / 15s / 45s), then bail.
   - Other 4xx → bail with result string, no rotation.
5. **Token splicing**: `__csr` / `__dyn` overridden with `latest_csr` / `latest_dyn` on every replay if any natural GraphQL POST has populated them. Pending validation of whether this is load-bearing.
6. **Anti-bot camouflage**: organic scroll burst every `scroll_burst_every` paginations (default 10), `(min, max)` scrolls per burst (default `(2, 5)`).

## Optional debug capture

Setting `FB_NETWORK_CAPTURE_ALL=1` in the environment turns on `ResponseInterceptor.network_capture` — every response (XHR, JS, CSS, etc.) is recorded with full request + response bodies for textual types, metadata-only for binaries. Use only for offline forensic analysis; off by default to keep production memory tight.

`save_network_capture_to_jsonl(path)` dumps it to disk on demand. Hybrid does **not** depend on this — it has its own narrower hook (`latest_pctfrq_request`) for capturing the replay template.

## Open questions / future work

These all live as bullet points in `CLAUDE.md` → "TODO / Future Work":

- **HTTP error classification — empirical refinement.** Current mapping is a working hypothesis; needs deliberate-error-trigger tests against known-banned, known-rate-limited, and stale-token scenarios to confirm or correct.
- **`freeze_tokens` experiment.** Run a 200+ pagination hybrid scrape with `__csr` / `__dyn` frozen at bootstrap-template values, no live-splicing. If it succeeds, drop the splicing path and the organic scroll bursts whose only purpose is token refresh.
- **GraphQL `errors[]` with partial data — drained.** Implemented now (we drain posts before raising / bailing); kept on the list because the auth-error marker set is incomplete.
- **Mid-scrape session invalidation — partially implemented.** HTML-body and auth-error detection both raise `FailedLoginError`; richer detection (e.g., `data.viewer == null` mid-scrape) is still on the roadmap.
