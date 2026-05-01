# Anatomy of a `ProfileCometTimelineFeedRefetchQuery` Request

Companion to [`token_generation.md`](token_generation.md) and [`overview.md`](overview.md). Documents every field in a real PCTFRQ POST body and the relevant request headers, where each token comes from in Facebook's JS, how often it rotates, and how (or whether) we could mint or harvest it without an organic scroll.

**Scope.** Body params + a few headers. **Cookies are out of scope** — they are managed by the browser cookie jar, set by login flow, and orthogonal to the request-construction pipeline studied here. The `cookie` request header was redacted from the analysis.

**Source capture.** `data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl` (51 PCTFRQ requests in one session). Re-derive with `tmp/hybrid/find_token_generators.py` and the targeted greps shown in §5 below.

---

## 0) Quick reference: what a PCTFRQ looks like on the wire

```
POST https://www.facebook.com/api/graphql/
Content-Type: application/x-www-form-urlencoded

x-fb-friendly-name: ProfileCometTimelineFeedRefetchQuery
x-fb-lsd:           pzwHGWeTjZrr7StYjdx89Q
x-asbd-id:          359341
referer:            https://www.facebook.com/FilomenaTassi/
origin:             https://www.facebook.com
user-agent:         Mozilla/5.0 (Macintosh; …)
cookie:             <session cookies — out of scope>

# Body (URL-encoded form fields)
av                          = 61588065262188
__user                      = 61588065262188
__aaid                      = 0
__a                         = 1
__req                       = z                  ← rotates every request (base-36 counter)
__hs                        = 20572.HYP:comet_pkg.2.1...0
dpr                         = 2
__ccg                       = EXCELLENT
__rev                       = 1038422138
__s                         = i7xwob:fzgnzr:zv3cjt
__hsi                       = 7634276045230003643
__dyn                       = 7xeUjGU…           ← see token_generation.md
__csr                       = l0DjMil…           ← see token_generation.md
__hsdp                      = gd11q2reh…         ← see token_generation.md
__hblp                      = 0xDZ0WwyAw…        ← see token_generation.md
__sjsp                      = gd11q2reh…         ← see token_generation.md
__comet_req                 = 15
fb_dtsg                     = NAfu98PfbTb…:37:1777406973
jazoest                     = 25339              ← derived from fb_dtsg
lsd                         = pzwHGWeTjZrr7StYjdx89Q
__spin_r                    = 1038422138
__spin_b                    = trunk
__spin_t                    = 1777493405
__crn                       = comet.fbweb.CometProfileTimelineListViewRoute
fb_api_caller_class         = RelayModern
fb_api_req_friendly_name    = ProfileCometTimelineFeedRefetchQuery
server_timestamps           = true
variables                   = {"afterTime":null,"beforeTime":null,"count":3,"cursor":"…","id":"…",…}
doc_id                      = 26563935306593088
```

The body is built by **two cooperating modules**:

1. **`getAsyncParams("POST")`** (in `OcNdXjPtAj9.js`) returns a dict with all the universal params (`__user`, `__a`, `__req`, `__hs`, `dpr`, `__ccg`, `__rev`, `__s`, `__hsi`, `__dyn`, `__csr`, `__hsdp`, `__hblp`, `__sjsp`, `__comet_req`, `fb_dtsg`, `jazoest`, `lsd`, `__spin_*`, `__crn`).
2. **`createRelayFBNetworkFetch`** (in `SXDXBpBY…js`) merges the result of `getAsyncParams("POST")` with GraphQL-specific fields (`fb_api_caller_class`, `fb_api_req_friendly_name`, `server_timestamps`, `variables`, `doc_id`).

The actual line is in `getAsyncParams`:

```js
var l = babelHelpers.extends({}, o("asyncParams").get(), c,
  (l = {
    __user: r("CurrentUserInitialData").USER_ID,
    __a:    1,
    __req:  r("uniqueRequestID")(),
   },
   l[r("StaticSiteData").hs_key]                            = r("SiteData").haste_session,        // __hs
   l[r("StaticSiteData").dpr_key]                           = r("SiteData").pr,                   // dpr
   l[r("StaticSiteData").connection_class_server_guess_key] = r("WebConnectionClassServerGuess").connectionClass,  // __ccg
   l.__rev                                                  = r("SiteData").client_revision,
   l.__s                                                    = o("WebSession").getId(),
   l[r("StaticSiteData").haste_session_id_key]              = r("SiteData").hsi,                  // __hsi
   l));

if (a || (
  d[r("StaticSiteData").jsmod_key]                          = r("ServerJSDefine").getLoadedModuleHash(),   // __dyn
  r("objectValues")(r("HasteBitMapName")).forEach(function (e) {
    var t = o("HasteBitMap").toCompressedString(e);
    t !== "" && (d[e] = t);                                                                       // __csr, __hsdp, __hblp, __sjsp
  })
));
// … followed by spin, comet, sprinkle (jazoest) blocks
```

Read this once; then the table below maps each field to the line that sets it.

---

## 1) Every field, with source and harvest strategy

Legend for **rotation**:

- 🟫 **deploy** — changes only when FB ships new JS (per-week scale).
- 🟦 **page-session** — set at page load, stable for the life of the tab.
- 🟨 **slow** — rotates within a session as resources/modules accumulate (3–25 paginations).
- 🟧 **per-request** — new value every request.
- 🟪 **derived** — computed from another field; not independently variable.

Legend for **harvest difficulty**:

- 🟢 **trivial** — read once at session start; reuse forever.
- 🟡 **easy** — `page.evaluate(...)` against a live FB page returns it directly.
- 🟠 **moderate** — needs a live FB page and re-read every N requests.
- 🔴 **hard** — would need to re-implement an encoder or model server-side state.

### 1.1 Universal request params (set by `getAsyncParams`)

| Field | Sample value | Rotation | Source module | How to harvest |
|---|---|---|---|---|
| `av` | `61588065262188` | 🟦 page-session | Set by Relay caller; equals `actorID` (the logged-in user). Not in `getAsyncParams`; injected separately. | 🟢 same as `__user` — read from cookies (`c_user` cookie equals this) or `CurrentUserInitialData.USER_ID` once. |
| `__user` | `61588065262188` | 🟦 page-session | `r("CurrentUserInitialData").USER_ID` | 🟢 same as `av`. |
| `__aaid` | `0` | 🟦 page-session (always `0` in our captures) | Not traced in the JS bundles we captured. Likely "alternate account id" / app id; static `0` for personal-account browsing. | 🟢 hardcode `0` until proven otherwise. |
| `__a` | `1` | 🟫 constant | `getAsyncParams` literal: `__a: 1`. Marks the request as async (vs. full page nav). | 🟢 hardcode `1`. |
| `__req` | `z`, `12`, `14`, `1a`, … | 🟧 per-request | `r("uniqueRequestID")()` — a base-36 counter starting at 1, incremented per call. Definition: `function s(){return (l++).toString(36)}` | 🟢 maintain a Python counter. Format: `int.to_string(36)`. Starts at 1 (`"1"`), our capture started at `z` because earlier requests in the same page incremented past 35. Resets per page-session. |
| `__hs` | `20572.HYP:comet_pkg.2.1...0` | 🟫 deploy | `r("SiteData").haste_session`. Identifies the deployed JS package. | 🟡 read once via `page.evaluate(() => require('SiteData').haste_session)`. Stable for hours; refresh on deploy. |
| `dpr` | `2` | 🟦 page-session | `r("SiteData").pr` — the device pixel ratio. | 🟢 hardcode to your viewport's DPR (`2` for a Retina display, `1` for normal). |
| `__ccg` | `EXCELLENT` | 🟦 page-session | `r("WebConnectionClassServerGuess").connectionClass` — server-side guess of client connection quality. | 🟢 hardcode to `EXCELLENT` or `GOOD`. Both are normal values. |
| `__rev` | `1038422138` | 🟫 deploy | `r("SiteData").client_revision` — the rev number of the deployed comet bundle. | 🟡 `page.evaluate(() => require('SiteData').client_revision)`. Tracks `__spin_r` exactly in our captures. |
| `__s` | `i7xwob:fzgnzr:zv3cjt` | 🟦 page-session | `o("WebSession").getId()` — a per-tab session id. | 🟡 `page.evaluate(() => require('WebSession').getId())`. Stable for the life of the tab. |
| `__hsi` | `7634276045230003643` | 🟦 page-session | `r("SiteData").hsi` — Haste Session ID, set at page load. | 🟡 `page.evaluate(() => require('SiteData').hsi)`. |
| `__dyn` | `7xeUjGU…` (301 chars) | 🟨 slow | `r("ServerJSDefine").getLoadedModuleHash()`. See [`token_generation.md` §3.5](token_generation.md#35-serverjsdefine--the-writer-of-__dyn). | 🟠 `page.evaluate(() => require('ServerJSDefine').getLoadedModuleHash())`. Re-read every ~10–25 paginations or when a non-200 response suggests staleness. |
| `__csr` | `l0DjMil…` (650 chars) | 🟨 slow | `r("HasteBitMap").toCompressedString("__csr")`. See [`token_generation.md` §3.4](token_generation.md#34-bootloader--the-writer-of-__csr). | 🟠 `page.evaluate(() => require('HasteBitMap').toCompressedString('__csr'))`. Re-read every ~3–4 paginations. **Possibly droppable** — see the `delete v.__csr` finding in the csr/dyn doc §3.7. |
| `__hsdp` | `gd11q2reh…` (539 chars) | 🟨 slow | `r("HasteBitMap").toCompressedString("__hsdp")`. Same mechanic as `__csr` but a different bucket. | 🟠 same as `__csr`. |
| `__hblp` | `0xDZ0Wwy…` (474 chars) | 🟨 slow | `r("HasteBitMap").toCompressedString("__hblp")`. | 🟠 same as `__csr`. |
| `__sjsp` | `gd11q2reh…` (238 chars) | 🟨 slow | `r("HasteBitMap").toCompressedString("__sjsp")`. | 🟠 same as `__csr`. |
| `__comet_req` | `15` | 🟦 page-session | `r("SiteData").comet_env` — the integer comet route environment. | 🟢 hardcode `15` (or read once via `page.evaluate`). |
| `fb_dtsg` | `NAfu98PfbTb2hH7wSzL3xLlPQokO3C4nHIMf-TO5mjad2mve50mLRCA:37:1777406973` | 🟦 page-session (refreshable) | `r("DTSG").getToken()` — read from `DTSGInitialData.token` at page load. The `:37:1777406973` suffix is `<rotor>:<creation_unix>`. | 🟡 `page.evaluate(() => require('DTSG').getToken())`. FB has `DTSG.refresh()` (`/ajax/dtsg/`) for hours-long sessions; not normally needed for short scrapes. |
| `jazoest` | `25339` | 🟪 derived | `r("DTSGUtils").getNumericValue(fb_dtsg)`. Algorithm: `version + str(sum(ord(c) for c in fb_dtsg))`, where `version = SprinkleConfig.version` (currently `"2"`). | 🟢 compute in Python from `fb_dtsg`: `"2" + str(sum(ord(c) for c in fb_dtsg))`. Verified: `2` + sum-of-charcodes of our captured `fb_dtsg` = `25339`. |
| `lsd` | `pzwHGWeTjZrr7StYjdx89Q` | 🟦 page-session | `r("LSD").token` — set at page load (login session descriptor; secondary CSRF token). | 🟡 `page.evaluate(() => require('LSD').token)`. Also appears verbatim as the `x-fb-lsd` request header. |
| `__spin_r` | `1038422138` | 🟫 deploy | `r("SiteData").__spin_r` — same as `__rev` in our captures. Sprinkle revision. | 🟡 read once via `page.evaluate`. |
| `__spin_b` | `trunk` | 🟫 deploy | `r("SiteData").__spin_b` — sprinkle branch. Always `trunk` for production. | 🟢 hardcode `trunk`. |
| `__spin_t` | `1777493405` | 🟫 deploy | `r("SiteData").__spin_t` — sprinkle timestamp (deploy time, unix). | 🟡 read once via `page.evaluate`. |
| `__crn` | `comet.fbweb.CometProfileTimelineListViewRoute` | 🟦 page-session | `r("CurrentCanonicalRoute")` — derived from the current Comet router state. | 🟢 hardcode `comet.fbweb.CometProfileTimelineListViewRoute` for profile-timeline pagination. |

### 1.2 GraphQL-specific params (set by `createRelayFBNetworkFetch`)

```js
v = babelHelpers.extends({}, u, r("getAsyncParams")("POST"), {
  fb_api_caller_class:      "RelayModern",
  fb_api_req_friendly_name: a.name,
  server_timestamps:        !0,
  variables:                JSON.stringify(i),
});
if (f && delete v.__csr,
    a.id ? v.doc_id = a.id
         : v.doc   = r("nullthrows")(a.text, …));
```

| Field | Sample value | Rotation | Source | How to harvest |
|---|---|---|---|---|
| `fb_api_caller_class` | `RelayModern` | 🟫 constant | Literal in `createRelayFBNetworkFetch`. | 🟢 hardcode. |
| `fb_api_req_friendly_name` | `ProfileCometTimelineFeedRefetchQuery` | 🟫 constant per query | `a.name` from the persisted-query metadata bundled with the page. | 🟢 hardcode for our query. |
| `server_timestamps` | `true` | 🟫 constant | Literal `!0`. | 🟢 hardcode. |
| `variables` | `{"afterTime":…,"count":3,"cursor":"…","id":"…",…}` | 🟧 per-request | `JSON.stringify(i)` — caller-supplied. Contains the actual pagination state. | 🟢 we control this. The `id` comes from the bootstrap; `cursor` comes from the previous response's `page_info.end_cursor`; `count`, `afterTime`, `beforeTime` are our knobs. |
| `doc_id` | `26563935306593088` | 🟫 deploy | `a.id` from the persisted-query metadata. Numeric IDs change when FB redeploys the query. | 🟡 capture from a real request once per deploy; the friendly-name → doc_id mapping is in the captured network log. |

### 1.3 Relevant request headers

| Header | Sample value | Rotation | Source | How to harvest |
|---|---|---|---|---|
| `x-fb-friendly-name` | `ProfileCometTimelineFeedRefetchQuery` | 🟫 constant per query | Set by `createRelayFBNetworkFetch` (or `getAsyncHeaders`) — duplicates the body field. | 🟢 hardcode. |
| `x-fb-lsd` | `pzwHGWeTjZrr7StYjdx89Q` | 🟦 page-session | `r("LSD").token` — same value as the body `lsd` field. Set by `getAsyncHeaders`. | 🟡 same as the body `lsd`. |
| `x-asbd-id` | `359341` | 🟦 page-session (constant in our capture) | Not traced in the captured JS. The literal string is not in our bundles, suggesting it's set as an integer constant injected at build time. | 🟢 hardcode the captured value. Re-capture if scrapes start failing. |
| `content-type` | `application/x-www-form-urlencoded` | 🟫 constant | Set by `getAsyncHeaders` / fetch wrapper. | 🟢 hardcode. |
| `origin` | `https://www.facebook.com` | 🟫 constant | Browser-set. | 🟢 hardcode. |
| `referer` | `https://www.facebook.com/<handle>/` | 🟦 page-session | Browser-set from current page URL. | 🟢 set to the profile URL we're scraping. |
| `user-agent` | `Mozilla/5.0 (Macintosh; …) Firefox/146.0` | 🟦 page-session | Browser-set. | 🟢 must match the browser that established the cookies (Camoufox UA). |
| `accept-language`, `accept-encoding` | … | 🟫 constant | Browser-set. | 🟢 hardcode plausible values. |
| `sec-fetch-*` | `empty` / `cors` / `same-origin` | 🟫 constant | Browser-set. | 🟢 hardcode. |
| `cookie` | (out of scope) | 🟦 page-session | Cookie jar. | — |

---

## 2) Categorization for Path B

Reading the table by **harvest difficulty** gives the cleanest planning view:

### 2.1 🟢 Trivial — hardcode or compute, no live page needed

`__a`, `__aaid`, `__comet_req`, `__crn`, `__spin_b`, `dpr`, `__ccg`, `fb_api_caller_class`, `fb_api_req_friendly_name`, `server_timestamps`, `__req` (Python counter), `jazoest` (computed from `fb_dtsg`), most headers.

These are constants for the duration of a scrape session; we set them ourselves.

### 2.2 🟡 Easy — `page.evaluate` once at session start

`av`, `__user`, `__hs`, `__rev`, `__s`, `__hsi`, `fb_dtsg`, `lsd`, `__spin_r`, `__spin_t`, `doc_id`, `x-asbd-id`.

Read at bootstrap time, stash in memory, reuse. Refresh only if a request fails in a way consistent with staleness.

### 2.3 🟠 Moderate — re-read every N requests (or live-page only)

`__dyn`, `__csr`, `__hsdp`, `__hblp`, `__sjsp`. The five HasteBitMap fields. They grow slowly within a session (see [`token_generation.md`](token_generation.md)) but they do grow. Two strategies that both work:

1. **Reuse the captured value verbatim** for several paginations, then re-read from `page.evaluate` every ~3–4 paginations (`__csr` cadence).
2. **Drop `__csr` entirely** — the `delete v.__csr` line in `RelayFBNetwork` proves FB itself sometimes omits it. Worth a one-shot replay test (drop `__csr`, see if FB still serves). Likely OK for `__hsdp`/`__hblp`/`__sjsp` too, since they're empty in many captures.

The **`variables` field** is per-request but we control it — it's the actual pagination state.

### 2.4 🔴 Hard — would need to re-implement encoder + maintain server-shaped state

Pure out-of-browser minting of `__dyn`/`__csr`. Would need (a) `BitMap.toCompressedString` ported (~50 LOC), (b) the resource-index registry the server uses (rotates per deploy), (c) a model of which indexes we'd plausibly have loaded. Not worth it given that 2.3 strategies are cheaper and Path B-lite stays attached to a live page anyway.

---

## 3) Minimal-body experiment: which fields are actually required?

We have not run this experiment yet, but the analysis above gives a strong prior on which fields can probably be dropped:

**Highly likely droppable** (one of: explicit deletion in FB's own code, or empty/zero in our capture):

- `__csr` — `RelayFBNetwork` explicitly deletes it for some queries (`if(f && delete v.__csr, …)`).
- `__hsdp`, `__hblp`, `__sjsp` — sibling HasteBitMap buckets; `BanzaiAdapterComet` strips them all by name when building bnzai requests, proving the server tolerates absence on some endpoints.
- `__aaid` — `0` in every observed PCTFRQ.
- `dpr` — display metadata; unlikely to gate.
- `__ccg` — connection-class hint; unlikely to gate.

**Almost certainly required** (CSRF / session-binding):

- `fb_dtsg`, `jazoest`, `lsd`, `__user`, `av`, `doc_id`, `variables`.

**Probably required** (server reads them for routing / consistency):

- `__hs`, `__rev`, `__spin_r/b/t`, `__hsi`, `__crn`, `fb_api_*`, `server_timestamps`.

**Unclear** (worth testing):

- `__dyn`, `__s`, `__req`, `__a`, `__comet_req`.

A 30-minute experiment: grab one captured PCTFRQ body, replay it via `page.request.post(...)` after stripping each candidate field one at a time, observe which omissions still return 200 with valid posts. The result becomes section 3.5 of [`overview.md`](overview.md).

---

## 4) Token-generation summary, by source module

The same information re-grouped by module, for verifying against the JS source:

| Module (Haste name) | Bundle | Provides |
|---|---|---|
| `CurrentUserInitialData` | comet runtime | `USER_ID` → `__user`, `av` |
| `uniqueRequestID` | comet runtime | base-36 counter → `__req` |
| `SiteData` | comet runtime | `haste_session` → `__hs`; `pr` → `dpr`; `client_revision` → `__rev`; `hsi` → `__hsi`; `comet_env` → `__comet_req`; `__spin_r/b/t` → `__spin_*` |
| `WebConnectionClassServerGuess` | comet runtime | `connectionClass` → `__ccg` |
| `WebSession` | comet runtime | `getId()` → `__s` |
| `HasteBitMap` + `HasteBitMapName` | comet runtime | `__csr`, `__hsdp`, `__hblp`, `__sjsp` (see csr/dyn doc) |
| `ServerJSDefine` | comet runtime | `getLoadedModuleHash()` → `__dyn` (see csr/dyn doc) |
| `DTSG` (+ `DTSGInitialData`) | comet runtime | `getToken()` → `fb_dtsg` |
| `DTSGUtils` (+ `SprinkleConfig`) | comet runtime | `getNumericValue(fb_dtsg)` → `jazoest` |
| `LSD` | comet runtime | `token` → body `lsd` and header `x-fb-lsd` |
| `CurrentCanonicalRoute` | comet runtime | `__crn` |
| `StaticSiteData` | comet runtime | the static **key-name** registry (`hs_key="__hs"`, `dpr_key="dpr"`, `jsmod_key="__dyn"`, …) |
| `getAsyncParams` | comet runtime | composes everything above into a dict |
| `getAsyncHeaders` | route bundle | `x-fb-lsd`, content-type, fetch headers |
| `createRelayFBNetworkFetch` | comet GraphQL bundle | merges in `fb_api_caller_class`, `fb_api_req_friendly_name`, `server_timestamps`, `variables`, `doc_id`; conditionally `delete v.__csr` |

---

## 5) How to verify each entry yourself

### 5.1 Re-derive the body fields from a capture

```bash
python3 - <<'PY'
import json
from urllib.parse import parse_qsl
PATH = "data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl"
with open(PATH) as fh:
    for line in fh:
        rec = json.loads(line)
        post = rec.get("request",{}).get("post_data") or ""
        if "ProfileCometTimelineFeedRefetchQuery" in post:
            for k,v in parse_qsl(post, keep_blank_values=True):
                print(f"{k:30} = {v[:80]}")
            break
PY
```

### 5.2 Find the JS module that generates a given field

The `getAsyncParams` line is the rosetta stone — once you've located it, every body field has a clear assignment statement. Grep the capture's JS bodies:

```bash
python3 - <<'PY'
import json, re
PATH = "data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl"
NEEDLE = '__d("getAsyncParams"'   # change as needed
with open(PATH) as fh:
    for line in fh:
        rec = json.loads(line)
        if rec.get("request",{}).get("resource_type") != "script": continue
        body = rec.get("response",{}).get("body") or ""
        if NEEDLE in body:
            i = body.find(NEEDLE)
            print(re.sub(r"\s+", " ", body[max(0,i-100):i+1500]))
            break
PY
```

Useful needles to start from:

- `__d("getAsyncParams"` — the master assignment line.
- `__d("StaticSiteData"` — the key-name registry (`hs_key`, `dpr_key`, etc).
- `__d("HasteBitMap"` / `__d("HasteBitMapName"` — `__csr` / `__hsdp` / `__hblp` / `__sjsp` machinery.
- `__d("ServerJSDefine"` — `__dyn` machinery.
- `__d("DTSG"` / `__d("DTSGUtils"` — `fb_dtsg` / `jazoest`.
- `__d("uniqueRequestID"` — `__req`.
- `getNumericValue:function` — the `jazoest` algorithm body (sum of charcodes prefixed with `SprinkleConfig.version`).
- `fb_api_req_friendly_name` — the `RelayFBNetwork` block where GraphQL-specific fields get added.
- `__d("getAsyncHeaders"` — request-header construction.

### 5.3 Verify `jazoest` computation in Python

```python
fb_dtsg = "NAfu98PfbTb2hH7wSzL3xLlPQokO3C4nHIMf-TO5mjad2mve50mLRCA:37:1777406973"
jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))
assert jazoest == "25339", jazoest
```

(`"2"` is `SprinkleConfig.version`, currently constant. Re-grep `SprinkleConfig` if a future capture's `jazoest` doesn't match this formula.)

### 5.4 Read live values from a Camoufox session

For testing the harvest strategy in §2.2 / §2.3:

```python
async with BrowserSession(account, pool) as s:
    page = s.page
    vals = await page.evaluate("""() => ({
        user:      require('CurrentUserInitialData').USER_ID,
        rev:       require('SiteData').client_revision,
        hsi:       require('SiteData').hsi,
        haste:     require('SiteData').haste_session,
        cometEnv:  require('SiteData').comet_env,
        spinR:     require('SiteData').__spin_r,
        spinB:     require('SiteData').__spin_b,
        spinT:     require('SiteData').__spin_t,
        webSess:   require('WebSession').getId(),
        ccg:       require('WebConnectionClassServerGuess').connectionClass,
        dpr:       require('SiteData').pr,
        dtsg:      require('DTSG').getToken(),
        lsd:       require('LSD').token,
        csr:       require('HasteBitMap').toCompressedString('__csr'),
        hsdp:      require('HasteBitMap').toCompressedString('__hsdp'),
        hblp:      require('HasteBitMap').toCompressedString('__hblp'),
        sjsp:      require('HasteBitMap').toCompressedString('__sjsp'),
        dyn:       require('ServerJSDefine').getLoadedModuleHash(),
    })""")
```

Compare each value to the body of the next captured PCTFRQ POST. Mismatches indicate either (a) FB changed the wiring (re-run the JS grep), or (b) the field is set by a different code path than the one this doc claims (open an investigation entry).

---

## 6) Open questions

- **`__aaid` source.** Always `0` in our captures; not traced in the JS bundles. May rotate for accounts with associated apps. Worth re-checking on a different account profile.
- **`x-asbd-id` source.** The literal `asbd` doesn't appear in any captured JS body. Likely a minified/obfuscated constant or set in a bundle outside the capture window. Static across our session, so fine to hardcode for now.
- **Are `__hsdp`/`__hblp`/`__sjsp` actually needed?** They're typically empty (`""` is dropped by `getAsyncParams`'s `t !== "" &&` guard). Test omitting them entirely.
- **Is `__csr` actually needed for the post-bearing query?** The `delete v.__csr` branch in `RelayFBNetwork` is conditional on a flag `f` we didn't trace. Worth checking via the minimal-body experiment (§3).

These belong in the next round of the Path B investigation. Update [`overview.md`](overview.md) when answered.
