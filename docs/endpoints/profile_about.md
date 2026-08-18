# ProfileAbout

Multi-navigation fetch of a profile's **About** page — the profile header plus the fields of each requested About sub-section (contact info, basic info, links: phone, email, address, hours, website, etc.). No GraphQL replay: everything is read out of FB's server-rendered document.

## Overview

- **Kind:** multi-navigation (`hybrid` mode only). One landing navigation to `/<handle>/about/` (which also renders the header for free), then **one navigation per requested section** — see [Strategy](#strategy).
- **High-level API:** `scraper.profile_about(handle, ...)`
- **CLI:** `fbscrape scrape profile-about <handle>...`
- **Output:** `data = [{"profile": profile_dict, "sections": [section_dict, ...]}]` — single-element list.
- **Relation to siblings:** the flattened row is a **superset** of [ProfileInfo](profile_info.md) (the header is reused via `_flatten_profile_info_record`). See also [ProfileAuthenticity](profile_authenticity.md).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity handle or numeric id of the profile; drives `/<handle>/about/`. |

## Strategy

1. Navigate to `/<handle>/about/`. This server-renders the profile **header** (name, follower count, bio, category — the same fields ProfileInfo returns) plus a **directory** of About sub-tab URLs.
2. Parse the header (`extract_profile_info`) and the sub-tab directory (`extract_profile_about_collections`); collect any sections already rendered on the landing page (`extract_profile_about_sections`).
3. For **each** key in `sections`: look it up in the discovered directory. If absent, skip it (not an error). Otherwise navigate to FB's own rendered sub-tab URL and append that page's parsed sections.
   - Why per-section: FB only server-renders a sub-tab's fields when *that* sub-tab is navigated to directly; they are never all rendered together. FB's rendered URLs are used verbatim because the URL shape differs between numeric-id profiles (query-style `?...&sk=directory_contact_info`) and vanity handles (path-style `/<handle>/directory_contact_info`).
4. Assemble `{"profile": ..., "sections": [...]}` as the single output record.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["ProfileAbout"]["modes"]["hybrid"]["params"]`.

| Param | Default | Meaning |
|---|---|---|
| `sections` | `("directory_contact_info", "directory_basic_info", "directory_links")` | Sub-tab keys to fetch, matched against the discovered directory. Defaults are the highest-value **Page** sections; override for personal-profile-shaped accounts. |
| `post_nav_sleep_seconds` | `3.0` | Pause after each navigation (landing + per-section). |
| `document_wait_seconds` | `4.0` | Extra settle time before reading each rendered document. |
| `operation_timeout_seconds` | `120` | Per-await safety timeout for renderer hangs. |

CLI flags: `--section <key>` (repeatable, maps to `sections`), `--post-nav-sleep-seconds`, `--document-wait-seconds`, `--operation-timeout-seconds`, plus the shared `--input-file`, `--output-dir`, `--max-sessions`, `--headless`, `--mobile`, etc.

## Usage

```bash
# Page-shaped account (default sections)
fbscrape scrape profile-about 61582991935083 --headless

# Personal-profile-shaped account: request different sub-tabs
fbscrape scrape profile-about zuck --section directory_work --section directory_education
```

Other observed section keys: `directory_intro`, `directory_category`, `directory_personal_details`, `directory_work`, `directory_education`, `directory_privacy_and_legal_info`.

```python
result = await scraper.profile_about("zuck")
row = FacebookGraphQLParser().flatten(result.data[0], endpoint="ProfileAbout")
```

## Gotchas

- **Not single-navigation.** Unlike [ProfileInfo](profile_info.md) (one navigation), each requested section costs an additional page navigation. Requesting many sections is proportionally slower.
- **Section coverage varies a lot by account.** Pages typically expose contact/basic-info/links; personal profiles more often expose work/education/personal-details instead — and rarely expose the same section keys Pages do. A requested key absent from the account's directory is silently skipped.
- A per-section navigation failure is logged and skipped; only a failed landing navigation or a missing header (`parse_error`) fails the whole scrape.

## Output shape

`data = [{"profile": profile_dict, "sections": [section_dict, ...]}]`. Flattened via `FacebookGraphQLParser.flatten(record, "ProfileAbout")` → `_flatten_profile_about_record`, which reuses the ProfileInfo header flattener and dispatches About fields into named keys (falling back to a generic `about_fields` list for everything).

Identifying column: **`profile_id`**. Selected columns:

| Column | Source |
|---|---|
| `profile_id`, `name`, `url`, `category`, `bio` | header (via ProfileInfo flattener) |
| `cover_photo_url`, `profile_picture_url`, `follower_count`, `following_count` | header |
| `phone`, `email` | dispatched from `directory_contact_info` |
| `messenger_url` | `business_messenger` link |
| `address`, `address_map_url`, `hours`, `rating_text` | dispatched from `directory_basic_info` |
| `website`, `website_url` | dispatched from `directory_links` |
| `about_fields` | full list of `{field_section_type, field_type, text, link_url}` — every field, dispatched or not |

Named About keys are `None` when their section was not fetched or not present. See `tests/unit/test_flatten_profile_about.py`.
