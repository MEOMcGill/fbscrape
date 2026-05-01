# How `__csr` and `__dyn` Are Generated

Companion to [`overview.md`](overview.md) and [`request_anatomy.md`](request_anatomy.md). Documents what the form-body fields `__csr` and `__dyn` actually are, where in Facebook's JS they get built, and how to verify any of this from the captured network log without trusting the writeup. For the *full* roster of every form field on a `ProfileCometTimelineFeedRefetchQuery` request — `fb_dtsg`, `jazoest`, `lsd`, `__hsi`, `__rev`, the spin/comet/route fields, etc., each annotated with source module and harvest difficulty — see [`request_anatomy.md`](request_anatomy.md).

**Status:** answered from a single capture (`data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl`, 1,206 records, 72 JS bodies). Re-run on a fresh capture if you suspect FB changed the wiring — the script `tmp/hybrid/find_token_generators.py` reproduces the analysis.

---

## TL;DR

- **`__csr`** is a `HasteBitMap` — a compressed-string serialization of the set of *bootloaded JS resource indexes* in the current page session. The `Bootloader` module flips a bit in this bitmap each time it finishes loading a resource (when `BootloaderConfig.csrOn` is true and the resource was marked client-side).
- **`__dyn`** is a separate bitmap maintained by the `ServerJSDefine` module — a compressed-string serialization of the set of *defined JS modules*. Every `__d(name, deps, factory, idx)` call in the loaded bundles flips a bit. `ServerJSDefine.getLoadedModuleHash()` returns the current compressed string.
- Both are attached to outgoing request bodies by **`getAsyncParams`**, the universal Comet request-param builder. Every authenticated FB AJAX / GraphQL / fetch request that goes through Comet's async layer carries them.
- The `RelayFBNetwork` GraphQL fetcher uses `getAsyncParams` and then conditionally **deletes `__csr`** for persisted queries (the `if(f&&delete v.__csr,a.id?v.doc_id=a.id:v.doc=...)` snippet). For our scrape, `f` is false (post-bearing query is non-persisted in the path we hit), so `__csr` stays on the body.

The bitmaps are **monotone-growing** within a page session: bits only get set, never cleared. That's why `__csr` and `__dyn` rotate ~steadily during a scrape rather than randomly — they reflect cumulative loads, not random tokens. A new bit appears whenever a new lazy-loaded chunk arrives or a new module gets defined.

```
                page session
─────────────────────────────────►
  PCTFRQ #1     PCTFRQ #2     PCTFRQ #3
   __csr=A       __csr=A       __csr=B   ←── changed when Bootloader pulled in a new chunk
   __dyn=X       __dyn=X       __dyn=X   ←── unchanged: no new modules defined
```

---

## 1) The method we used

**Premise.** Facebook minifies JS aggressively, but **string literals survive minification** because they are object keys / form-field names. The literal strings `"__csr"` and `"__dyn"` therefore appear verbatim in whichever bundle defines them, whichever bundle reads them, and any dependency-array entries that name them. Find those bundles and you've found the generator.

**Steps the script (`tmp/hybrid/find_token_generators.py`) performs:**

1. Iterate every record in the JSONL capture.
2. Filter to JS bodies (`request.resource_type == "script"` or URL ends in `.js`).
3. For each JS body, count literal occurrences of:
   - **Primary tokens**: `__csr`, `__dyn`.
   - **Co-occurrence tokens**: `fb_dtsg`, `lsd`, `jazoest`, `doc_id`, `__hsi`, `__rev`, `__spin_*`. A bundle that builds GraphQL POST bodies will reference *all* of these in close proximity, even if it doesn't directly contain `__csr` (because it pulls them in via constants from a sibling module).
4. Rank bundles by score (`primary_hits + 0.25 * cooccurrence_hits`); break ties by smaller body size (denser hits = more likely the actual generator, not a transitive importer).
5. Print short context windows (`±80 chars`) around the densest hits.

**Why this ranks well in practice.** The bundle that *defines* the token names (`HasteBitMapName`) contains the literal strings `"__csr"` and `"__dyn"`. The bundle that *attaches them to request bodies* (`getAsyncParams`) references the names indirectly via constants and so won't grep for `"__csr"` literally — but it *does* contain `fb_dtsg`, `lsd`, `__hsi`, `__rev`, `__spin_r`, etc., so the co-occurrence score finds it. The bundle that *populates `__csr`* (`Bootloader`) contains both the literal `"__csr"` (it calls `HasteBitMap.add("__csr", ...)` directly) and a high concentration of the surrounding mechanics (`HasteResourceIndexUtil`, `BootloaderConfig.csrOn`, etc.).

**Limitations.**

- Only finds modules whose source is in the capture. JS that's already cached in the browser before the recording started won't appear. (Mitigation: run the capture on a fresh Camoufox launch with no warm cache, which is what `tmp/hybrid/capture_one_scrape.py` does.)
- Treats string occurrences as evidence of involvement — doesn't prove control flow. To go from "this module mentions `__csr`" to "this module sets `__csr`", you read the surrounding code (the script prints context windows for exactly this reason).
- Tokens get reused for unrelated things. `lsd` matches `lsd` in unrelated identifiers; the co-occurrence score is heuristic, not exact. Read the snippets, don't trust the score blindly.

---

## 2) What we were looking for

In order of "answers that move the investigation":

1. **The bucket-name registry.** Where in JS is the literal string `"__csr"` defined as a constant? That tells us what `__csr` *is conceptually* (a bucket name in some lookup), and the surrounding code tells us what other buckets exist.
2. **The mutator.** What code path *adds* bits to the `__csr` bucket? That tells us the rotation trigger ("new bit appears whenever X happens").
3. **The encoder.** What turns the bitmap into the compressed-string form we see on the wire? That tells us whether we could re-mint values from outside the browser.
4. **The attacher.** What code path actually puts `__csr=...&__dyn=...` on the GraphQL POST body? That's the universal "make a request" function and worth knowing because it's also where every other dynamic token is set (`fb_dtsg`, `lsd`, `jazoest`, `__rev`, `__hsi`).
5. **The reader at the call site.** Specifically for the GraphQL caller: does it ever override or delete these tokens? If it does, that's a clue about which tokens FB *actually* requires versus which it tolerates.

The doc below addresses all five, with the supporting JS excerpts.

---

## 3) Findings

All snippets below were extracted by `find_token_generators.py` from the capture. Module IDs (`HasteBitMap`, `Bootloader`, `getAsyncParams`, etc.) are FB's internal Haste names, preserved through minification because Haste resolves modules by string name at runtime.

### 3.1 `HasteBitMapName` — bucket-name registry

```js
__d("HasteBitMapName", [], (function (t, n, r, o, a, i) {
  var e = Object.freeze({
    CSR:  "__csr",
    HSDP: "__hsdp",
    HBLP: "__hblp",
    SJSP: "__sjsp"
  });
  i.default = e;
}), 66);
```

Four HasteBitMap buckets exist. All four are populated during a scrape (see §4 — `__hsdp` had 13 unique values, `__hblp` 8, `__sjsp` 13 across the 51 PCTFRQs in our capture). The bucket name initials likely stand for "haste server-defined", "haste big-pipe", "server-JS-pipe" — separate accounting for different JS-load pathways. The mechanism is the same for all four: the bucket gets `add()` calls from various code paths during a session.

### 3.2 `HasteBitMap` — the in-memory store

```js
__d("HasteBitMap", ["BitMap"], (function (t, n, r, o, a, i, l) {
  var e = new Map;                          // bucketName -> BitMap
  function s(t, n) {                        // add(bucketName, bitIndex)
    var o;
    e.has(t) || e.set(t, new (r("BitMap")));
    (o = e.get(t)) == null || o.set(n);
  }
  function u(t) {                           // toCompressedString(bucketName)
    var n, r;
    return (n = (r = e.get(t)) == null ? void 0 : r.toCompressedString()) != null ? n : "";
  }
  l.add = s;
  l.toCompressedString = u;
}), 98);
```

Stateless wrapper around per-bucket `BitMap` instances. Two operations: `add(name, idx)` flips a bit, `toCompressedString(name)` serializes to wire format.

### 3.3 `BitMap.toCompressedString` — the encoder

```js
// Inside the BitMap class definition, same bundle:
// (variable names minified; readable form annotated)
$2_buildCompressed: function () {
  // Walk the bit array, run-length encode runs of equal bits.
  // Each run length L is emitted as: ("0" repeated len(toBin(L))-1) + toBin(L).
  // Concatenate all run-length codes into a binary string.
  // Pad to multiple of 6, chunk into 6-bit groups, map each group through
  // a 64-character alphabet to produce the final compressed string.
  ...
}
```

Two takeaways:
- **Run-length encoded** then **6-bit-packed** with a custom 64-char alphabet. Cheap to compute. Reproducing it in Python is feasible (~50 LOC).
- **Stateful, monotone within a page session.** The `BitMap` lives in the closure of the `HasteBitMap` module; bits accumulate; nothing resets it short of a page reload.

### 3.4 `Bootloader` — the writer of `__csr`

```js
function le(e, t, n) {
  if (I.set(e, t), !(t.type === "async" || t.type === "csr")) {
    var a = t.p;
    if (a != null)
      for (var i of o("HasteResourceIndexUtil").parseResourceIndexes(a))
        i !== o("HasteResourceIndexUtil").UNKNOWN_RESOURCE_INDEX
          && ((!T.has(i) || n) && T.set(i, e),
              t.c && r("BootloaderConfig").csrOn && o("HasteBitMap").add("__csr", i));
    se(e);
  }
}
```

`le()` is called by `Bootloader` whenever a JS resource is registered. For each resource index `i` parsed out of the resource manifest, *if* the resource is marked client-side (`t.c`) and the `csrOn` global is true, it adds `i` to the `__csr` bucket.

**Implication for rotation cadence.** `__csr` advances whenever `Bootloader` registers a new resource that wasn't already in the bitmap. During a scroll, FB lazy-loads chunks for newly visible content (image renderers, video players, comment composers); those chunks call into `Bootloader`, which flips new bits, which changes the compressed output. A scroll that hits already-loaded code paths flips no bits — `__csr` is stable across those paginations. That matches the observed "rotates every 3–4 paginations" cadence.

### 3.5 `ServerJSDefine` — the writer of `__dyn`

```js
__d("ServerJSDefine", ["BitMap", "replaceTransportMarkers"], (function (t, n, r, o, a, i, l) {
  var u = new (r("BitMap"));
  var c = {
    getLoadedModuleHash: function () { return u.toCompressedString(); },
    handleDefine: function (n, o, a, i, l) {
      i >= 0 && u.set(i);
      define(n, o, a, i, l);
    },
    ...
  };
}), 98);
```

Same encoding as `__csr`, different bucket. The bitmap here is a **closure-local `BitMap`** (not in the `HasteBitMap` registry). Every `__d(name, deps, factory, moduleIndex)` call goes through `handleDefine`, which flips bit `moduleIndex` and then forwards to the real `define`.

**Implication for rotation cadence.** `__dyn` advances when *new modules* are defined, which is rarer than new resources being loaded — most modules are defined inline in the entry-point bundles at page load. A few late-bound modules get defined as the user interacts (e.g., the first time you open a comment composer). That matches the observed "rotates every 10–25 paginations" — slower than `__csr` because most module definitions land at page boot.

### 3.6 `getAsyncParams` — the attacher

```js
__d("getAsyncParams",
  [..., "DTSGUtils", ..., "HasteBitMap", "HasteBitMapName", ..., "LSD",
   "ServerJSDefine", "SiteData", ..., "StaticSiteData", "WebSession", ...],
  (function (...) {

    // ... build base param dict d ...
    l.__rev = r("SiteData").client_revision;
    l.__s   = o("WebSession").getId();
    l[r("StaticSiteData").haste_session_id_key] = r("SiteData").hsi;  // -> __hsi

    if (a || (
      d[r("StaticSiteData").jsmod_key] = r("ServerJSDefine").getLoadedModuleHash(),  // -> __dyn
      r("objectValues")(r("HasteBitMapName")).forEach(function (e) {
        var t = o("HasteBitMap").toCompressedString(e);
        t !== "" && (d[e] = t);                                                       // -> __csr, __hsdp, __hblp, __sjsp
      })
    ));
    ...
  }), 98);
```

This is the universal Comet request-param builder. Every async XHR / fetch / GraphQL call that goes through Comet's async layer pulls its body params from here. Notable side effects:

- Sets `__rev` from `SiteData.client_revision` (per-deploy constant; rotates when FB ships).
- Sets `__hsi` from `SiteData.hsi` (per-page-session, set at page load).
- Sets `__dyn` from `ServerJSDefine.getLoadedModuleHash()`.
- Iterates `HasteBitMapName` and sets `__csr`, `__hsdp`, `__hblp`, `__sjsp` from each non-empty bucket.

The `if (a || (...))` guard is the only place these can be skipped — a flag passed in by the caller. We did not chase the call-site that sets `a=true`, but it's almost certainly the bnzai code path (see 3.8).

### 3.7 `RelayFBNetwork` (GraphQL fetcher) — call site

```js
// In the createRelayFBNetworkFetch function body:
... server_timestamps: !0, variables: JSON.stringify(i) });
if (f && delete v.__csr,
    a.id ? v.doc_id = a.id
         : v.doc = r("nullthrows")(a.text, "RelayFBNetwork: A query should..."));
```

The local `v` is the merged param dict for the outgoing POST. The GraphQL fetcher post-processes `v` after `getAsyncParams` populates it:

- If `f` is truthy: **delete `v.__csr`**. The flag `f` is set when the query is being sent in a way that doesn't need the bitmap. Worth verifying empirically whether the post-bearing pagination query (`ProfileCometTimelineFeedRefetchQuery`) ever falls into this branch — if so, FB itself is happy to omit `__csr`.
- If `a.id` (a numeric persisted-query id) exists: send `doc_id`. Otherwise send the raw `doc` text.

**Practical implication for replay.** The `delete v.__csr` line is a strong hint that `__csr` is not strictly required by the GraphQL endpoint — at least not in some configurations. A first replay experiment worth running: drop `__csr` from a captured PCTFRQ body and see if FB still serves the response.

### 3.8 `BanzaiAdapterComet` (`/ajax/bnzai`) — explicit deletion

```js
__d("BanzaiAdapterComet", [..., "HasteBitMapName", ..., "StaticSiteData", ...,
                           "getAsyncParams", ...], (function (...) {
  ...
  getEndPointUrl: function (t) {
    var e = r("getAsyncParams")(_);
    r("objectValues")(r("HasteBitMapName")).forEach(function (t) {
      return delete e[t];                          // strip __csr, __hsdp, __hblp, __sjsp
    });
    delete e[r("StaticSiteData").jsmod_key];       // strip __dyn
    e.ph = r("SiteData").push_phase;
    ...
  }
}), 98);
```

Independent corroboration: the `bnzai` telemetry endpoint *deliberately strips* the bitmap fields after `getAsyncParams` populates them. The fields are session-fingerprint data; bnzai doesn't carry them. Two things follow:

1. The bitmap fields are not load-bearing for *all* FB endpoints. `bnzai` works without them.
2. `bnzai` request bodies are a useful "what does FB look like with no `__csr`/`__dyn`?" reference — they're the closest natural example of a stripped-down FB request.

---

## 4) Empirical rotation cadence (across 51 PCTFRQs)

The mechanism in §3 predicts how rotation *should* behave. This section measures how rotation *actually* behaved in one captured scrape, to answer the practical question: **how often do we need to refresh these tokens, and does scrolling itself cause rotation?**

**Method.** Walk the capture chronologically. For each consecutive pair of `ProfileCometTimelineFeedRefetchQuery` POSTs, record (a) which of the five HasteBitMap tokens changed value and (b) how many JS resources were fetched in the interval between the two POSTs. Reproduce with `tmp/hybrid/analyze_token_rotation.py`:

```bash
python3 tmp/hybrid/analyze_token_rotation.py \
    data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl
```

**Sample.** 51 PCTFRQs over a single ~3-minute manual-scroll session against `FilomenaTassi`. 50 consecutive pairs analyzed. 72 JS resources fetched total.

### 4.1 Unique values per token

| Token | Unique values across 51 PCTFRQs |
|---|---:|
| `__csr` | 12 |
| `__dyn` | **2** |
| `__hsdp` | 13 |
| `__hblp` | 8 |
| `__sjsp` | 13 |

`__dyn` is essentially static — only 1 change in 51 requests. The other four rotate roughly 8–13 times across a 3-minute session.

### 4.2 Stable plateaus

Long stretches of zero rotation appear repeatedly even during active scrolling:

| Plateau | Length (consecutive pairs with zero token change) |
|---|---:|
| Pairs 5–11 | 7 |
| Pairs 18–28 | **11** |
| Pairs 42–50 | 9 |

So under manual-scroll conditions the bitmap fields stayed completely stable for **up to 11 paginations in a row**. That's a hard practical floor: a captured value is good for at least ~10 successive replays without refresh, even in the worst case (full DOM rendering active).

### 4.3 Correlation with JS chunk loads

The §3.4 / §3.5 mechanics predict that token rotation should coincide with JS resource fetches (Bootloader registers a resource, ServerJSDefine defines a module). For each consecutive PCTFRQ pair, we count whether each token changed AND whether any JS was fetched in the interval:

| Token | changed & JS-in-window | changed & no-JS | same & JS | same & no-JS |
|---|---:|---:|---:|---:|
| `__csr` | 5 | 6 | 5 | 34 |
| `__dyn` | 1 | 0 | 9 | 40 |
| `__hsdp` | 6 | 6 | 4 | 34 |
| `__hblp` | 4 | 3 | 6 | 37 |
| `__sjsp` | 6 | 6 | 4 | 34 |

**Reading the table.**

- `__dyn` correlates perfectly: 1 change, 1 JS fetch in window. Hypothesis confirmed for module definitions.
- For the four HasteBitMap fields, about half of changes coincide with JS fetches and half don't. The simple "JS fetch → token rotation" hypothesis is only partially right.

### 4.4 Why some rotations have no JS fetch in the window

Six pairs show a `__csr` change with zero captured JS resources in the immediate interval. Three plausible explanations, in order of likelihood:

1. **Late module registration.** A JS bundle fetched in an *earlier* window can register modules lazily — its `__d(name, deps, factory, idx)` factories run when other code first requires them, which can be much later than the network fetch. The `__dyn` bit flips at factory-run time, not fetch time. `__csr` similarly: `Bootloader.le()` runs after the parse step, not at the network response.
2. **HTML-inlined `__d(...)` calls.** Facebook's HTML response embeds `<script>` blocks containing literal `__d(...)` calls and Bootloader registrations. Those produce bitmap mutations without ever causing a network fetch — the bytes were already in the page response.
3. **Edge-of-window timing.** A fetch that landed milliseconds before `t_prev` may have completed its registration just after `t_prev`, so the change appears in the *next* pair rather than the one containing the fetch.

These don't invalidate §3 — the mechanism is correctly described. They just add nuance: **token rotation tracks "module activation," not "module download."** The two are correlated but not identical.

### 4.5 Practical implications

**For Path B-lite (current implementation):** Path B-lite issues GraphQL requests via `page.request.post(...)` without rendering new content in the DOM. No new post renderers load; no new modules get activated. The captured token values should stay stable for the full duration of the scrape.

**For the manual-scroll path:** Plateaus of 7–11 paginations are common. Refreshing tokens every ~5–10 paginations is more than enough margin. Refreshing every request is overkill.

**For the "drop them" experiment:** This empirical data strengthens the case. `__dyn` changes only once per session; FB clearly isn't using it as a per-request anti-replay signal. The four HasteBitMap fields rotate slowly with long stable plateaus; if they were load-bearing, mid-plateau requests would fail. They don't. Combined with the `delete v.__csr` finding (§3.7) and the explicit stripping in `BanzaiAdapterComet` (§3.8), the evidence suggests these fields are informational/telemetry rather than load-bearing.

The cleanest next experiment: drop all five HasteBitMap fields from a captured PCTFRQ body, replay it, and see if FB still serves the response. If yes, the entire token-rotation question collapses.

---

## 5) What this means for Path B / replay

The investigation question that triggered this dig was: "do we need to mint `__csr` / `__dyn` ourselves, or can we harvest them?"

**Three options, in order of complexity:**

1. **Reuse the captured value across N requests.** The bitmaps are stateful and grow slowly. Empirically (§4.2), the same `__csr` is sent unchanged for **up to 11 successive paginations** even during active manual scrolling, and likely indefinitely under Path B-lite conditions. If we replay using a recently-harvested template, the value stays valid for many requests in a row. Cheapest possible approach.
2. **Read live values from the page via `page.evaluate`.** Inside Camoufox, call:
   ```js
   require("HasteBitMap").toCompressedString("__csr")
   require("ServerJSDefine").getLoadedModuleHash()  // -> __dyn
   ```
   These are the exact functions `getAsyncParams` calls. This guarantees fresh values matching the current session. Already what Path B-lite gets for free since it stays attached to a live page.
3. **Re-implement the encoder + maintain a synthetic bitmap state in Python.** Possible (`BitMap.toCompressedString` is ~50 LOC) but requires also knowing (a) the resource-index → resource-name mapping the server uses, and (b) which indexes correspond to a "plausible" loaded set. Brittle; FB rotates indexes per release. Only worth pursuing if pure out-of-browser replay is needed.

The `delete v.__csr` finding in §3.7 raises a fourth option worth testing first: **just don't send `__csr`** and see if FB still serves the response. The empirical rotation data in §4 strengthens this case — if the bitmaps were load-bearing, mid-plateau requests (which reuse the same value across 7–11 paginations) would fail; they don't. Try omitting all five HasteBitMap fields.

---

## 6) How to run this check yourself

### 6.1 Quick re-run on the existing capture

```bash
cd /Users/mikad/MEOMcGill/fbscrape
python3 tmp/hybrid/find_token_generators.py \
    data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl \
    --top 12 --snippets-per-bundle 3
```

What to look for in the output:

- **A bundle whose snippet contains `__d("HasteBitMapName"`** with the `CSR:"__csr"` / `HSDP:"__hsdp"` literal. This is §3.1.
- **A bundle whose snippet contains `__d("HasteBitMap"`** with the `add` / `toCompressedString` definitions. Same bundle as above; this is §3.2.
- **A bundle whose snippet contains `o("HasteBitMap").add("__csr", i)`**. This is the Bootloader writer (§3.4).
- **A bundle whose snippet contains `delete v.__csr`** with `doc_id` / `nullthrows("...RelayFBNetwork...")` nearby. This is the GraphQL fetcher (§3.7).
- High co-occurrence-score bundles (lots of `fb_dtsg`/`lsd`/`jazoest` hits but zero `__csr` literal) are *importers* of these constants, not the source — read past them.

If the script's top-12 doesn't surface bundles matching the four bullet points above, FB has changed something. Re-grep with explicit needles:

```bash
python3 - <<'PY'
import json, re
PATH = "data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl"
NEEDLES = ['__d("HasteBitMap"', '__d("HasteBitMapName"',
           'HasteBitMap").add("__csr"', 'delete v.__csr',
           '__d("ServerJSDefine"', '__d("getAsyncParams"']
with open(PATH) as fh:
    for line in fh:
        rec = json.loads(line)
        if rec.get("request",{}).get("resource_type") != "script": continue
        body = rec.get("response",{}).get("body") or ""
        url = rec.get("url","")
        m = re.search(r"/([^/?#]+\.js)", url)
        short = m.group(1) if m else url[-40:]
        for n in NEEDLES:
            if n in body:
                i = body.find(n)
                ctx = re.sub(r"\s+", " ", body[max(0,i-200):i+len(n)+200])
                print(f"[{short[:30]:30}] {n}\n    {ctx}\n")
PY
```

### 6.2 Re-run on a fresh capture (if you suspect FB changed things)

The investigation framework already produces capture files. To get a new one:

```bash
# uses FB_NETWORK_CAPTURE_DIR + FB_NETWORK_CAPTURE_ALL=1
python tmp/hybrid/capture_one_scrape.py
# -> data/hybrid/<handle>_<UTC-ts>/network_<...>.jsonl
```

Then point `find_token_generators.py` at the new file. Cache-warmed bundles won't appear in the capture — capture works best on a freshly-launched Camoufox session.

### 6.3 Verify the live-page-call approach (optional sanity check)

Inside a Camoufox session attached to a logged-in FB page:

```python
csr = await page.evaluate("require('HasteBitMap').toCompressedString('__csr')")
dyn = await page.evaluate("require('ServerJSDefine').getLoadedModuleHash()")
```

Compare these to the `__csr` / `__dyn` values on the next captured `ProfileCometTimelineFeedRefetchQuery` POST body. If they match (they should, modulo a flush race), §3.6's account is confirmed end-to-end. This is also the implementation path for "harvest live values" in §5 option 2.

---

## 7) When this doc goes stale

Triggers to re-run the analysis:

- A scrape starts failing in a way that suggests token rejection (401/403 with no obvious cause, empty pagination responses with status 200).
- FB's Comet bundles are visibly different (different `__d("HasteBitMapName"` content, missing `getAsyncParams` module name).
- We get serious about pure-out-of-browser replay (§5 option 3) and need fresh `BitMap.toCompressedString` source to port.

If FB rotates module names (`HasteBitMap` → something else), the grep needles in §6.1 will return zero hits and you'll know immediately.
