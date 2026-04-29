# Path B Investigation — GraphQL Replay Viability

Working investigation notes. Goal: determine whether we can replace scroll-driven scraping with direct GraphQL requests fired from outside the rendered page, eliminating the renderer-wedge problem entirely.

**Status:** 🟡 In progress.
**Linked option:** [`speed_and_memory.md` — Option 2](speed_and_memory.md).

---

## Hypothesis

We can drive Facebook profile scraping by:

1. **Bootstrapping** with one normal Camoufox page load to establish session cookies and harvest dynamic tokens (`fb_dtsg`, `lsd`, `__hsi`, `__rev`, etc.).
2. **Replaying** `POST /api/graphql/` requests directly with controlled `variables` (target profile id, `cursor`, `count`, optional `afterTime` / `beforeTime`) and the harvested tokens.
3. **Parsing** responses through the existing `FacebookGraphQLParser`.
4. Never scrolling, never letting the DOM grow, never letting the renderer wedge.

If viable, this removes the renderer from the pagination loop entirely. Memory becomes a flat function of how many post dicts we hold in Python; no DOM growth; no scroll latency; no wedge.

## Why we're investigating this

The renderer-wedge bug (page hangs after deep scrolling, only caught by the per-call timeout) is a structural property of the current scrape loop. DOM cleanup (Option 3) buys 2-3x more headroom but doesn't solve it. UI-driven date filtering (Option 1) bounds each session but adds selector fragility and detection-pattern concerns. Path B sidesteps both — *if* we can replay safely without tripping FB anti-bot.

## What we need to find out

These are the unknowns we need data on before committing to implement Path B:

1. **Which GraphQL queries fire during a normal scrape?** What are their friendly names and `doc_id`s? Which carry the post payload vs. ancillary data (sidebar, profile metadata, ads, telemetry)?
2. **What's the request shape for the post-bearing queries?** Required body params, required headers, authentication tokens, format of `variables`.
3. **Which tokens are dynamic vs. static?** Do `fb_dtsg`, `lsd`, `__rev`, `__hsi`, `__spin_*`, `__csr` change per request, per session, or per longer interval? Which are required for FB to accept the request vs. silently ignored?
4. **How does pagination work?** Where does the cursor live in responses? How does it flow back into the next request? What's the `count` ceiling before FB rate-limits or errors?
5. **What surrounding telemetry would we be missing?** What non-GraphQL XHR fires during a real session (FB pixel pings, presence updates, reaction tracking, etc.)? How much of this would direct GraphQL replay need to mimic to look organic?
6. **What's the rate ceiling?** How fast can we fire pagination requests before FB throttles? What does throttling look like (4xx? specific error in body? silent block?)?
7. **What breaks first when we replay?** Run a one-shot replay with captured-and-cloned headers and see what FB accepts vs. rejects.

## Methodology

### Phase 1: Capture (current)

Enable full XHR capture on a real scrape session, then analyze offline.

**Mechanism (already implemented):**

- `ResponseInterceptor` (`fbscrape/response.py`) records network responses with full request (method, headers, body) and response (status, headers, body).
- Two env vars control scope:
    - `FB_NETWORK_CAPTURE_DIR` — destination directory. If unset, capture is in-memory only.
    - `FB_NETWORK_CAPTURE_ALL=1` — capture *every* response (CSS / JS / images / fonts / etc.). Default scope is XHR-only. For binary types (image / font / media) the body bytes are skipped — only metadata + size is recorded — so even with `ALL=1` the capture file size is bounded.
- On `BrowserSession.close()`, if `FB_NETWORK_CAPTURE_DIR` is set, the capture is dumped to `<dir>/network_<UTC-timestamp>_<account-id>.jsonl`. One JSONL per session.

**Ready-made script:**

`tmp/path_b_investigation/capture_one_scrape.py` runs one scrape against a known-shallow handle (`JohnYakabuskiMPP`, ~42 posts) with both env vars set. Output goes to `data/path_b_investigation/<handle>_<UTC-timestamp>/`:

```bash
python tmp/path_b_investigation/capture_one_scrape.py
```

Output folder contents after the run:
- `posts.json` — the scrape result, same shape as production output.
- `network_<timestamp>_<account-id>.jsonl` — every response captured during the session.

**To capture a different handle / scrape pattern manually:**

```bash
export FB_NETWORK_CAPTURE_DIR=/tmp/fb_captures
export FB_NETWORK_CAPTURE_ALL=1   # omit for XHR-only
python tmp/test_meo_scraping.py    # or any scrape entry point
```

(The env-var checks live in `BrowserSession.close()` and `ResponseInterceptor.intercept_response()`.)

Output: one JSONL file per browser session. Each line is one response with this shape:

```json
{
  "url": "https://www.facebook.com/api/graphql/",
  "timestamp": "2026-04-29T18:42:13.901+00:00",
  "is_xhr": true,
  "is_graphql": true,
  "request": {
    "method": "POST",
    "resource_type": "xhr",
    "headers": { ... },
    "post_data": "av=...&__user=...&doc_id=...&variables=..."
  },
  "response": {
    "status": 200,
    "headers": { ... },
    "body": "...",          // null for binary types when body_skipped=true
    "body_size": 145320,    // byte count, always recorded if body could be fetched
    "body_skipped": false   // true for image/font/media — bytes not stored
  }
}
```

**Volume warning.** Full response bodies are stored verbatim for textual resource types (xhr, fetch, document, script, stylesheet). Binary types (images, fonts, media) get metadata + size only, so even with `FB_NETWORK_CAPTURE_ALL=1` the capture file is bounded. That said, a 30-minute scrape can still generate hundreds of MB because GraphQL responses are large and every JS/CSS bundle adds up. Run captures on bounded scrapes (a few minutes, one or two profiles) rather than full backfills.

### Phase 2: Analysis

Process the JSONL file to answer the questions above. Suggested queries / scripts (not yet written — to be added as we work):

1. **Inventory of GraphQL queries.** Group records by `request.headers["x-fb-friendly-name"]` (or extract from `post_data` by parsing the form-encoded body). Count occurrences. Note which carry posts (cross-reference with `parse_timeline_response` returning non-empty).
2. **Inventory of non-GraphQL XHR.** Group by URL path. This tells us what surrounding telemetry exists.
3. **Token rotation analysis.** For each dynamic-looking body param (`fb_dtsg`, `lsd`, `__rev`, `__hsi`, etc.), look at how it changes across requests within a single session. Stable across all requests? Per-burst? Per-second? This tells us how token refresh would need to work.
4. **Pagination cursor flow.** Find the `count` and `cursor` variables in successive pagination requests. Confirm the `end_cursor` in response N becomes the `cursor` in request N+1. Identify any other variables that change between paginated calls.
5. **Required vs. optional headers/params.** A separate one-shot replay test: fire a captured request via `curl` or `aiohttp`, gradually strip headers/params, see what FB accepts.

### Phase 3: One-shot replay

Once Phase 2 has identified the post-bearing pagination query, write a minimal Python script that:

1. Loads cookies from a fresh Camoufox login (or copies them from a captured session).
2. Fires *one* pagination request with hard-coded tokens to that query's endpoint.
3. Logs the response status and parses for posts.

This proves (or disproves) that the basic mechanic works in isolation, before investing in full implementation.

### Phase 4: Decision

Based on Phases 1-3, decide GO / NO-GO / HYBRID. Criteria below.

## Decision criteria

**GO** (full implementation worth pursuing) if:
- One-shot replay returns valid post data with status 200.
- Token refresh path is identified and feasible (not per-request, not requiring solving an unbounded crypto challenge).
- Cursor-based pagination works for `count > 3` (raising throughput is essential).
- Surrounding-telemetry analysis suggests we can either (a) piggyback on a live page session for the ambient traffic, or (b) get away without it for the volumes we run at.

**NO-GO** (path is dead, don't pursue) if:
- Replay is rejected even with all captured headers + cookies (signature-verified somewhere we haven't identified).
- Tokens rotate per-request via a derivation we can't reverse cheaply.
- FB returns "page" but uses a separate write-only stream / WebSocket for actual data delivery (so GraphQL alone isn't sufficient).

**HYBRID** (use Path B for some scenarios, Path A or status quo for others) if:
- Replay works for shallow recent pagination but breaks at depth (e.g., `afterTime` of >1y back returns nothing).
- Replay works only when initiated from inside a live page session (via `page.evaluate` or `page.request`), in which case it's "Path B-lite" — still useful for memory but not throughput.

## Risks worth tracking as we work

- **Burning accounts on replay testing.** Each replay attempt against a real account is a behavioral signal. Use a small set of test accounts and accept they may get flagged. Don't run replay tests on the same accounts used for production scraping until the technique is confirmed.
- **Stale `doc_id`s.** FB rotates these periodically. A captured `doc_id` from today may be invalid in 2 weeks. The investigation should re-capture immediately before each phase.
- **Capture file leakage.** JSONL captures contain cookies, `fb_dtsg`, and other session-level secrets. Treat them as credentials. Don't commit them, don't share them, gitignore the capture directory.

## Working notes

This section is for findings as the investigation proceeds. Add dated entries.

### 2026-04-29 — capture mechanism wired up

- Restored `network_capture` list on `ResponseInterceptor` (formerly `graphql_responses`, now broader).
- `intercept_response` now records all XHR (GraphQL + non-GraphQL) with full request+response.
- `BrowserSession.close()` writes capture to `$FB_NETWORK_CAPTURE_DIR/network_<ts>_<id>.jsonl` if env var set.
- No analysis run yet — pending first capture.

### Next entries

(Findings from first capture go here.)

---

## When this investigation is done

Whether GO, NO-GO, or HYBRID — flip the status at the top, link the implementation PR (if GO), and update [`speed_and_memory.md` — Option 2](speed_and_memory.md) with the conclusion. Then remove the TEMP capture instrumentation from `response.py` and `browser_session.py` (search for `# TEMP:` comments referencing this doc).
