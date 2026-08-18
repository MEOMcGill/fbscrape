# ProfileAuthenticity

Single-shot fetch of a Facebook **profile's** authenticity record — join date,
last-updated window, whether it's Meta-verified, the "about this account"
fields, and the `delegate_page_id` bridge to any linked Page.

## Overview

- **Kind:** single-shot (`hybrid` mode) — one GraphQL POST, no pagination, no
  scroll, no date filter.
- **High-level API:** `scraper.profile_authenticity(user_id)`
- **CLI:** `fbscrape scrape profile-authenticity <user_id>`
- **Output:** `data = [authenticity_dict]` — a single-element list.

Related profile endpoints: [`ProfileInfo`](profile_info.md) (header) and
[`ProfileAbout`](profile_about.md) (about sections).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `user_id` | yes | The profile id (e.g. `100044331674441`, `61577505662345`). Sent as `variables.userID`. |

### `user_id` vs `page_id`

This endpoint takes a **`user_id`** (profile side), not a `page_id`. Modern
accounts start `61…`, classic accounts `100…`. Post `author_id`s **are**
`user_id`s and can be fed in directly. For the Page-side equivalent and the
gotcha of mixing the two, see [`page_transparency.md`](page_transparency.md).

## Strategy

Same single-shot shape as [PageTransparency](page_transparency.md):

1. Navigate to `/<user_id>/`. **Skips the bootstrap scroll** — the natural
   `ProfileCometDirectoryAuthenticityModalQuery` only fires from a UI click.
2. Reuse `ResponseInterceptor.latest_natural_graphql_request` as the auth
   template (harvested from any natural GraphQL POST).
3. Synthesize the body: `fb_api_req_friendly_name =
   ProfileCometDirectoryAuthenticityModalQuery`, `variables = {"scale": <scale>,
   "userID": <user_id>}`, `doc_id = PROFILE_AUTHENTICITY_DOC_ID`.
4. One `page.request.post()`; HTTP-status classification.
5. Return `ScrapeOutcome(result="success", data=[authenticity_dict])`.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["ProfileAuthenticity"]["modes"]["hybrid"]`.
No pagination or date options.

| Param | Default | Meaning |
|---|---|---|
| `scale` | `3` | Image scale requested in `variables.scale`. |
| `post_nav_sleep_seconds` | `3.0` | Pause after navigation before the replay. |
| `template_capture_timeout` | `45.0` | Max wait for a natural GraphQL POST to harvest auth fields. |
| `request_timeout_ms` | `30000` | Per-request timeout. |
| `operation_timeout_seconds` | `120` | Overall safety timeout. |

## Usage

```python
from fbscrape import FacebookScraper, gather
from fbscrape.response import FacebookGraphQLParser

parser = FacebookGraphQLParser()
async with FacebookScraper(db="accounts.db", max_browser_sessions=3) as scraper:
    async for r in gather(scraper.profile_authenticity(uid) for uid in user_ids):
        if not r.data:
            continue
        row = parser.flatten(r.data[0], endpoint="ProfileAuthenticity")
        print(row["user_id"], row["name"], row.get("delegate_page_id"))
```

The most common use is as **stage 1 of the transparency pipeline**: flatten,
read `delegate_page_id`, and feed the non-null ones into
[`PageTransparency`](page_transparency.md#usage).

## Gotchas

- **`delegate_page_id` is `null` for pure personal profiles** — no transparency
  record exists for those, so there's nothing to chain into `PageTransparency`.
- **No post-level "has a Page" signal.** `actors[0].__typename` is always
  `User` on posts, so you must call this endpoint on every distinct `author_id`
  and branch on `delegate_page_id`.

## Output shape

`data = [authenticity_dict]`. Flattened via
`FacebookGraphQLParser.flatten(record, "ProfileAuthenticity")` →
`_flatten_profile_authenticity_record` (user_id, name, delegate_page_id,
profile_join_date, category, is_meta_verified, about_fields, header_fields, …).
See `tests/unit/test_flatten_profile_authenticity.py`.
