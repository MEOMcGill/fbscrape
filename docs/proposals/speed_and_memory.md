# Speed & Memory Improvements

Working document for ideas to make scrapes faster and lighter. Each entry has the rationale, expected impact, effort, detection-risk considerations, and notes for if/when we try it.

**Status legend:**
- 🔵 Idea — not yet evaluated
- 🟡 In progress — being tried
- 🟢 Adopted — landed in code
- 🔴 Rejected — tried or analyzed and ruled out (with reason)

---

## Background: what we're trying to fix

The renderer-side cost of long scrapes grows with the DOM. On a deep timeline (e.g., a politician's Page with 2+ years of history) we observe:

- Scroll latency creeping from <1s → 10s → 60s → 90s+ per call (`evaluate` riding a saturated JS event loop in the renderer).
- Eventual full renderer wedge — `page.evaluate("scrollBy(...)")` blocks indefinitely. Caught only by `OPERATION_TIMEOUT_SECONDS` per-call timeout (`browser_session.py`).
- Browser process RAM growth (e.g., 6GB → 10GB over ~30 min on a deep scrape).

Sources: DOM accumulation (FB doesn't aggressively unmount old posts), decoded image bitmap cache, MSE video buffers, FB's Relay GraphQL store (never evicted within a page lifetime), React reconciliation cost growing with feed length.

**Constraint:** naive periodic page reload doesn't help — the timeline has no positional anchor in the URL, so reload resets to the top and we'd re-walk the same posts and hit the same wall.

---

## Detection-risk baseline: what the current scroll approach looks like to FB

For each option below, "detection risk" is described relative to this status-quo baseline.

The current approach (scroll-to-bottom of `facebook.com/<handle>/`) has these detectable patterns:

- **Action volume.** 500+ scrolls over ~30 minutes. Few real users scroll this deep into a profile.
- **Action variety.** Only scroll. No clicks, no reactions, no profile-photo opens, no comment-thread expansions.
- **Cadence regularity.** Even with `humanize=True`, scroll cadence is rhythmically regular in a way real users aren't.
- **Surrounding telemetry.** Real (browser fires resource loads, FB telemetry pings, sensor data, focus events) — this is one of the strongest things going *for* us.
- **Session shape.** Single-target. No main feed visit, no friend profile browsing, no notifications check, no sidebar interaction.

So the current method already has detection signals. The question for each new option isn't "does it have any signals" — it's "does it trade off signals for ones FB is more or less likely to flag."

Empirically, the current approach hasn't been getting accounts mass-banned, which means either FB's threshold is permissive for these patterns or we're at low enough volume to be statistical noise. New options should be evaluated against that working baseline.

---

## Ideas

### 1. UI-driven date filter (`afterTime` / `beforeTime`) 🔵

**Approach.** FB's profile timeline UI exposes a "Filters" feature that lets users set a date range. Applying the filter does NOT change the URL — `facebook.com/<handle>` stays the same — but it fires a GraphQL request named `ProfileCometTimelineFeedRefetchQuery` whose `variables` include:

- `afterTime: <unix_seconds> | null` — lower bound (oldest)
- `beforeTime: <unix_seconds> | null` — upper bound (newest)

(Empirically confirmed by clicking Filters → date range → Apply on `facebook.com/FordNationDougFord` and inspecting the network tab. Example: filter "before 2025-01-02" produced `beforeTime: 1735793999`.)

By driving this filter programmatically with Playwright (click button, fill dates, click Apply), each session is scoped to a bounded date range. A Page that posts ~5x/day is ~1800 posts/year — a 1-year slice is ~50-150 scrolls, well under the threshold where the renderer wedges.

Loop year-by-year (or quarter-by-quarter for very busy Pages): apply filter, scroll the bounded slice, save, advance to next slice. Accumulate posts across slices and dedupe by `post_id`.

**Expected impact.** Big — directly addresses depth-of-scroll. Per-session memory stays low because each session ends before the DOM gets heavy. Renderer never reaches a wedged state.

**Effort.** Medium. Requires:
- UI-interaction code: locating the Filters button, opening the date picker, filling start/end dates, clicking Apply, waiting for the refetch query to land.
- Slicing loop wrapping `user_timeline`: iterate date ranges, accumulate, dedupe.
- Robustness: FB's UI selectors will drift; needs occasional maintenance.

**Limitations.**
- Selector fragility — FB changes UI element structure regularly.
- Whether this works for non-Page (personal) profiles is unconfirmed. Pages have rich filter UIs; personal-profile filters may be limited or absent.
- Adds wall-clock time per slice transition (open dialog, fill dates, wait).

**Detection risk.**

The act of filtering is a real FB feature, so server-side it looks identical to a user using the date filter — no novel network signature. The risk is **client-side behavioral telemetry**:

- *Filter cadence.* Real users typically apply *one* filter ("posts from when event X happened") and browse, then leave. Applying many filters in succession, exhausting each one, is rare-to-unique behavior. **Higher signal than current scroll approach for this dimension.**
- *Per-slice exhaustion.* Real users rarely scroll to the very end of a filtered view. Mechanically scrolling to bottom of every slice is a tell.
- *Surrounding activity.* Same as current approach — single-target sessions without organic browsing nearby.

Mitigations to consider when implementing:
- Don't always exhaust a slice — stop scrolling once we've covered the date range we wanted.
- Use larger windows (year, quarter) over smaller ones (month) — larger feels more user-shaped.
- Pace between filter applications with realistic delays (minutes, not seconds), mimicking someone reading what they pulled up.
- Mix in non-scraping activity per session (main feed scroll, a couple of friend profile views).
- Spread slices across accounts and sessions — don't have one account exhaust a target's full history in a day.

**Net assessment.** Detection risk profile is *different* from status quo, not necessarily worse. Trades depth-of-scroll signal for filter-cadence signal. Worth A/B testing on real accounts to see whether the cadence pattern actually trips FB's thresholds for the account types we use.

**Next step.** Prototype the UI interaction in a manual Playwright session — confirm we can reliably open the filter dialog, fill dates, and trigger the refetch. Once that works, wire up year-slicing in `user_timeline` and run a side-by-side scrape against a known-deep handle.

---

### 2. Direct GraphQL cursor replay 🔵

**Approach.** Skip the page entirely after bootstrap. Capture the necessary auth state from a real page load, then fire `POST https://www.facebook.com/api/graphql/` requests directly with the right `doc_id`, headers, and variables to walk pagination + dates ourselves.

From an inspected `ProfileCometTimelineFeedRefetchQuery` request we have most of what's needed:

| Field | Example | Source |
|---|---|---|
| Endpoint | `POST https://www.facebook.com/api/graphql/` | URL |
| Friendly name | `ProfileCometTimelineFeedRefetchQuery` | header `X-FB-Friendly-Name` + body `fb_api_req_friendly_name` |
| `doc_id` | `26563935306593088` | body |
| Profile target | `id=100044454308831` | body variables |
| `fb_dtsg` | `<redacted — per-session token, ~50 chars + `:N:unix_ts` suffix>` | body |
| `lsd` | `<redacted — per-session token, ~22 chars>` | body + header `X-FB-LSD` |
| `__rev`, `__spin_*`, `__hsi`, `__dyn`, `__csr`, etc. | various | body, derived from page state |

Pagination would be driven by varying `cursor` (initially `null`, then the `end_cursor` from each response) and date range by `afterTime` / `beforeTime`. `count` can probably be raised from the UI's value of 3 to ~25 for higher throughput.

Stack:
1. Bootstrap: load profile page once via Camoufox to establish cookies and capture dynamic tokens (`fb_dtsg`, `lsd`, `__hsi`, `__rev`) by reading the page HTML or first GraphQL response.
2. Fire GraphQL pagination directly via `aiohttp` or similar, passing through the cookies + tokens.
3. Parse responses through the existing `FacebookGraphQLParser`.
4. Never scroll, never let the DOM grow, never let the renderer wedge.

**Expected impact.** Largest. Removes the renderer from the pagination loop entirely. Memory footprint becomes a flat function of how many post dicts we hold in Python; no longer grows with timeline depth. Throughput jumps from "30s/iter at the slow end" to "however fast FB will let us paginate over GraphQL."

**Effort.** High — real engineering. Requires:
- Identifying the *pagination* query's `doc_id` (the captured request is the *initial refetch*; pagination is likely a sibling query like `ProfileCometTimelineFeedPaginationQuery` — capture one in DevTools).
- Token freshness: `fb_dtsg` rotates (~24h). Need a refresh path, probably a periodic page reload to re-extract.
- Empirically determining which body params are required vs. ignored by stripping them one at a time.
- `count` ceiling discovery — FB rate-limits or 4xx's at some threshold; tune to safe value.
- Maintenance: FB rotates `doc_id`s and adds new required headers periodically. Each break is a debug session.

**Limitations.**
- Brittle to FB changes — page-driven scraping degrades gracefully when FB tweaks the UI; direct GraphQL replay breaks loudly when `doc_id` or required headers change.
- Still need a real browser session for cookies / login / token bootstrap.
- Probably won't work cleanly for non-Page personal profiles (different query, different filter capabilities).

**Detection risk.**

Different profile from status quo and from option 1, with one major weak point:

- *Network signature.* Server-side, the request itself is identical to one fired by the page UI — same headers, same body shape. **Lower per-request signal than current approach** in this dimension.
- *Surrounding telemetry — narrower risk than initially framed.* Empirical finding from [`hybrid overview`](../hybrid/overview.md): the bulk of per-scroll chatter is on CDN domains (`scontent-*.fbcdn.net` images and video chunks) — different infrastructure from the FB application servers that actually gate pagination, and unlikely to feed real-time anti-bot decisions. The only FB-domain ambient endpoints that fire meaningfully during scrolling are `/ajax/bulk-route-definitions/` (~45% of scrolls) and `/ajax/bnzai` (sporadic, session-level — not scroll-coupled). The actual missing-signal risk under Path B-lite is **engagement breadth on the FB application domain**: a session that fires only `ProfileCometTimelineFeedRefetchQuery` for an hour is distinguishable from one that incidentally hovers links or briefly navigates elsewhere. **Still higher signal than status quo, but narrower and more targeted to mitigate than "no chatter at all" suggests.**
- *Cadence.* Replays can be done much faster than scroll-driven pagination. Bursting through a year of pagination in 30 seconds is very hard to disguise. Need explicit throttling matched to plausible user pace, which partly defeats the throughput win.
- *Token shape consistency.* `fb_dtsg`, `lsd`, `__rev`, etc. encode session state. If tokens don't match the cookies' session, FB flags. Token-management bugs will show up as bans, not error responses.

Mitigations:
- Always fire from inside a live Camoufox page context (use `page.evaluate` or `request_context`) so requests piggyback on the real session's network setup — partially recreates the surrounding telemetry.
- Throttle to plausible page-scroll cadence (a few requests/second max), not maximum throughput.
- Periodically reload the bootstrap page to refresh tokens and generate the surrounding chatter.
- Mix replay sessions with occasional full page-driven sessions on the same account, so the account profile has both patterns.
- Every N replay calls, perform a small native page interaction (a real scroll, a hover, a brief navigation) to generate engagement-breadth signal organically rather than synthesizing it. This addresses the narrower risk surface identified in [`hybrid overview`](../hybrid/overview.md) at the lowest engineering cost.

**Net assessment.** *In principle* the cleanest solve for the wedge problem. *In practice* the missing-telemetry signal is a real risk that needs careful engineering to mitigate. Best as a long-term project once option 1 is buying us time. Hybrid stack (page-driven for shallow recent + GraphQL replay for deep historical) is probably the eventual right architecture.

**Next step.** Capture a `ProfileCometTimelineFeedPaginationQuery` (the post-initial pagination query) in DevTools to get its `doc_id` and variables. Then write a single-shot replay script that fires one pagination request with hardcoded tokens and confirms a valid response. That tells us whether the basic mechanic works before investing in full implementation.

---

### 3. DOM cleanup mid-scrape 🔵

**Approach.** Every N scrolls (say 25-50), run a `page.evaluate` that physically removes post nodes far above the viewport from the DOM:

```js
// rough sketch
document.querySelectorAll('[data-pagelet*="FeedUnit"]').forEach(node => {
  const rect = node.getBoundingClientRect();
  if (rect.bottom < window.scrollY - 5000) {  // 5000px above viewport
    node.remove();
  }
});
```

This doesn't reset scroll position, so we keep our place in the timeline.

**Expected impact.** Partial. Reduces React reconciliation cost and layout/paint pressure on each scroll, which is often the immediate driver of slow `evaluate` calls. Does NOT reclaim:
- FB's Relay store memory (the GraphQL cache holds normalized data for every post ever fetched)
- Decoded image cache (Firefox image decoder retains bitmaps independently of DOM presence — though removing the `<img>` may eventually let it GC)
- MSE video buffers (need to explicitly destroy media elements)

Bandage on a deeper issue — probably buys 2-3x more headroom before the renderer chokes, not 10x. Doesn't prevent the wedge, just delays it.

**Effort.** Medium. Implementation is ~20 lines but needs careful testing — removing nodes the wrong way might break FB's intersection observers (the things that trigger pagination on scroll). If FB's pagination depends on observing the bottom of a continuous DOM list, partial deletion could break it.

**Limitations.**
- Mitigation, not solve. Eventually you still hit the wall, just later.
- Risk of breaking pagination if FB's load-more triggers depend on DOM continuity.
- Selector (`[data-pagelet*="FeedUnit"]`) is FB-internal and will change.
- Doesn't help if the dominant cost is JS-heap pressure rather than DOM-tree size.

**Detection risk.**

Different from options 1 and 2 — risk is on the *renderer-state* side rather than network or behavioral:

- *DOM mutation pattern.* Real users never delete feed posts from the DOM. If FB's anti-bot watches for unexpected mutations (via MutationObserver on the feed root), our cleanup is a unique signal. **Higher signal than current scroll approach in this dimension** — though it's an unusual thing for FB to watch for.
- *Console errors.* React may throw "cannot read property of null" type errors when reconciling against deleted nodes. Error-rate increases inside the renderer can be reported back to FB via their telemetry. Per-session error counts that exceed normal-user baselines are a flagable signal.
- *Network/server-side.* Identical to current scroll approach — no new server-visible behavior, since we're only mutating the local DOM.

Mitigations:
- Use only on Page-type profiles where we already accept higher detection risk for archival depth.
- Suppress console errors via `try { ... } catch { }` wrappers around the mutation, and consider catching any error events bubbled to `window`.
- Verify FB doesn't put MutationObservers on the feed root — would show up in DevTools profiler.

**Net assessment.** Lower detection risk than options 1 and 2 in the dimensions FB most likely cares about (network, behavior), but introduces a novel renderer-state signal. Probably the lowest-overall-risk option for short-term mitigation, *if* it actually produces a measurable scroll-latency improvement. Caveat: only delays the wedge, doesn't solve it.

**Next step.** Manual prototype in a Firefox console session against a deep profile (instructions in conversation). Verify (a) which selector matches feed posts in the current FB UI, (b) that pagination still triggers after removing old nodes, (c) that scroll latency visibly improves. If all three hold, port into `BrowserSession` as a periodic call gated by a config flag.

---

## Process notes

- Add new ideas as `### N. <Name> 🔵` sections.
- When trying one, flip to 🟡 and add a "Trial log" subsection with dates, what was tried, what was observed.
- When adopting, flip to 🟢 and link to the implementation commit / PR.
- When ruling out, flip to 🔴 and write a "Why not" subsection — these are valuable so we don't re-evaluate the same idea later.
- Detection-risk subsections are written *relative to the status-quo baseline* at the top of this doc. If the baseline changes (e.g., we adopt account-rotation discipline, mix in organic activity, etc.), revisit the per-option assessments.
