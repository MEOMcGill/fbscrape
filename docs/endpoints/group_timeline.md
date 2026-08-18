# GroupTimeline

Scrape a Facebook group's feed.

## Overview

- **Kind:** paginated post stream (`hybrid` mode).
- **High-level API:** `scraper.group_timeline(handle, start_date=None, end_date=None, **params)`
- **CLI:** `fbscrape scrape group-timeline <handle> [--start-date …] [--end-date …]`
- **Output:** `data = list[dict]` — one element per group post.
- **Target query:** `GroupsCometFeedRegularStoriesPaginationQuery` (GCFRSPQ).

For a group's header/about metadata, see [`GroupInfo`](group_info.md) /
[`GroupAbout`](group_about.md).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity group handle **or** numeric group id; both resolve via `/groups/<handle>/`. |
| `start_date` | no | `YYYY-MM-DD`. Client-side lower bound only. |
| `end_date` | no | `YYYY-MM-DD`. Advisory — see below. |

**Dates are client-side only.** GCFRSPQ has no `beforeTime`/`afterTime`
variable, so FB applies no server-side date filter. Both dates are optional;
without them termination relies on `MaxPostsReached` / `NoNewPostsStreak` /
`EndOfFeed` / `MaxPaginations`.

## Strategy

Same shape as [UserTimeline](user_timeline.md) hybrid, with these differences:

1. **No server-side date filter** (above).
2. **Sort override** via `sorting_setting` (injected into every replay's
   `variables.sortingSetting`):
   - `TOP_POSTS` (default) — FB UI default, algorithmic. Lowest-fingerprint and
     empirically safer for sustained scraping. The date-tail stop is
     `ConsecutiveOutOfRange`, not "oldest < start".
   - `CHRONOLOGICAL` — descending by creation_time; closest to true order **but
     empirically correlated with account suspensions** — opt-in only.
   - `RECENT_ACTIVITY` — by most recent comment/reaction; non-chronological.
3. **First-batch date-stop guard (CHRONOLOGICAL).** `OldestInBatchBelowStartDate`
   skips on iteration 1 because the bootstrap edge can carry an out-of-order
   "highlight" post.
4. **Cursor-reset uses 2nd-oldest as anchor (CHRONOLOGICAL)** to avoid false
   positives from that bootstrap highlight.
5. **Auto-unstick** on a resumed `no_new_posts_streak` — the CLI swaps
   `last_cursor` to the rank-3 oldest cursored post (`fbscrape unstick-cursor`).
6. **No multi-leg cursor_reset resume** — `cursor_reset` is terminal; partial
   data is preserved.

## Options

| Param / flag | Default | Meaning |
|---|---|---|
| `sorting_setting` / `--sorting-setting` | `TOP_POSTS` | Feed sort (Facebook default; advisable not to change — see above). |
| `pagination_count` | `3` | Posts per replay (Facebook default; advisable not to change). |
| `max_posts` / `--max-posts` | `-1` | Cap on accumulated posts. |
| `max_consecutive_out_of_range` / `--max-consecutive-out-of-range` | `20` | Bail after N posts in a row outside the date window (primary date-tail stop on non-chronological sorts; no-op without dates). |
| `max_no_progress_streak` | `30` | Bail after N replays with no new posts. |
| `max_paginations` / `--max-paginations` | `-1` | Safety cap on replays. |

Full set + pacing in `Query.ENDPOINT_REGISTRY["GroupTimeline"]["modes"]["hybrid"]`.

## Usage

```bash
fbscrape scrape group-timeline 392585550772135 --start-date 2024-01-01 --end-date 2025-01-01
fbscrape scrape group-timeline 787909081545196 --start-date 2024-01-01   # numeric id
fbscrape scrape group-timeline 392585550772135 --max-posts 500          # open-ended

# Chronological + a wider out-of-range tolerance
fbscrape scrape group-timeline 392585550772135 --start-date 2024-01-01 \
    --sorting-setting CHRONOLOGICAL --max-consecutive-out-of-range 30
```

## Gotchas

- **`CHRONOLOGICAL` is ban-correlated** — prefer `TOP_POSTS` for sustained runs.
- **No multi-leg resume.** A `cursor_reset` ends the scrape (partial data kept);
  use `--continue` to pick back up, and `unstick-cursor` if a resume stalls.

## Output shape

`data = list[dict]`, one per post. Flattened via
`FacebookGraphQLParser.flatten(record, "GroupTimeline")` →
`_flatten_grouptimeline_post`. Response shape: bootstrap line uses
`data.node.group_feed.edges[].node = Story` (Shape A); stream lines use
`data.node = Story` (Shape B); the parser fans Shape A into per-Story entries.
See `tests/unit/test_flatten_comet_contract.py`.
