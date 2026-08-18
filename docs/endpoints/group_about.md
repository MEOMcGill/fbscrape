# GroupAbout

Single-shot fetch of a group's **About** page — header, description, activity stats, rules, and the admin/moderator facepile — in one navigation, no pagination.

## Overview

- **Kind:** single-shot (`hybrid` mode) — one navigation, no GraphQL replay, no scroll, no date filter.
- **High-level API:** `scraper.group_about(handle)`
- **CLI:** `fbscrape scrape group-about <handle>...`
- **Output:** `data = [{"group": group_dict, "cards": [...]}]` — single-element list.

Related: [GroupInfo](group_info.md) (header only), [GroupTimeline](group_timeline.md) (the group's posts).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity group handle **or** numeric group id. Builds the navigation URL `https://www.facebook.com/groups/<handle>/about/`. |

## Strategy

`BrowserSession.group_about_hybrid`:

1. Navigate to `/groups/<handle>/about/` (`_hybrid_navigate`); pause `post_nav_sleep_seconds`, dismiss any dialog, run the error check.
2. Wait `document_wait_seconds` for the renderer to settle, then read the full DOM via `page.content()` (guarded by `operation_timeout_seconds`).
3. Parse the header with `parser.extract_group_info(html, handle)` — returns `parse_error` result if the group node is absent.
4. Parse the About cards with `parser.extract_group_about_cards(html)` — description, privacy/discoverability/history/location info items, activity stats, rules, admin facepile.
5. Return `ScrapeOutcome(result='success', data=[{"group": group, "cards": cards}])`.

**Single-navigation, unlike `profile-about`.** Facebook renders every About section on the one page, so there is no per-sub-tab navigation — one `goto` captures the whole About page.

## Options

Defaults from `Query.ENDPOINT_REGISTRY["GroupAbout"]["modes"]["hybrid"]["params"]`.

| Param | Default | Meaning |
|---|---|---|
| `post_nav_sleep_seconds` | `3.0` | Pause after navigating before the error check. |
| `document_wait_seconds` | `4.0` | Extra settle time before reading the rendered document. |
| `operation_timeout_seconds` | `120` | Per-await safety timeout for renderer hangs. |

Pass `None` (or omit) to use the registry default.

## Usage

```bash
fbscrape scrape group-about 392585550772135 --headless
# multiple groups / from a file (CSV, Parquet, YAML, JSON/JSONL with a `handle` column)
fbscrape scrape group-about --input-file groups.csv
```

```python
async with FacebookScraper(db="db/accounts.db") as scraper:
    result = await scraper.group_about("392585550772135")
    row = FacebookGraphQLParser().flatten(result.data[0], "GroupAbout")
```

## Gotchas

- **Admin roster is best-effort.** `admin_profiles` comes from a UI facepile that FB may truncate for groups with many admins/moderators; `admin_and_moderator_count` (from the Rules card) is the exact combined count, so the roster can undercount relative to it.
- The About page's privacy info item (split `privacy_label` + `privacy_description`) is richer than the header's single "Public group" string and is promoted over the header value.
- Cards are extracted from the settled DOM, not from GraphQL — a header-only failure yields `parse_error`; missing cards still flatten cleanly (About-specific keys empty/None) as long as the header parsed.

## Output shape

`data = [{"group": group_dict, "cards": [...]}]`. Flattened via `FacebookGraphQLParser.flatten(record, "GroupAbout")` -> `_flatten_group_about_record`, a superset of `_flatten_group_info_record`. Identifying column: `group_id`.

Columns include: `group_id`, `name`, `url`, `handle`, `privacy_label`, `privacy_description`, `member_count`, `viewer_join_state`, `cover_photo_url`, `content_views`, `description`, `discoverability_label`, `discoverability_description`, `created_time`, `history_summary`, `locations`, `about_info_items`, `posts_last_day`, `posts_last_month`, `total_members_text`, `new_members_text`, `admin_and_moderator_count`, `rules`, `friend_member_count`, `admin_profiles`.

See `tests/unit/test_flatten_group_about.py`.
