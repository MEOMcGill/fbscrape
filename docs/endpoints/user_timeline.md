# UserTimeline

Scrape a user's (or Page's) timeline of posts between two dates.

## Overview

- **Kind:** paginated post stream (`hybrid` mode).
- **High-level API:** `scraper.user_timeline(handle, start_date=None, end_date=None, max_posts=-1, resume_from=None, **params)`
- **CLI:** `fbscrape scrape user-timeline <handle> [--start-date …] [--end-date …]`
- **Output:** `data = list[dict]` — one element per post.
- **Target query:** `ProfileCometTimelineFeedRefetchQuery` (PCTFRQ).

For a single post's full content by permalink, see [`PostDetail`](post_detail.md);
for a profile's header/about, see [`ProfileInfo`](profile_info.md) /
[`ProfileAbout`](profile_about.md).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity handle or numeric id; drives navigation to `/<handle>/`. |
| `start_date` | no | `YYYY-MM-DD`. Client-side lower bound. When omitted, no lower bound — termination relies on end-of-feed / no-new-posts / max-posts. |
| `end_date` | no | `YYYY-MM-DD`. Enforced **server-side** via FB's `beforeTime`. The CLI auto-fills today (UTC) when omitted, mirroring FB's UI (which always sends `beforeTime`). |

## Strategy

1. Navigate to the profile, then **re-scroll while waiting** for a natural
   PCTFRQ to capture as a replay template. Scrolls deeper every few seconds
   until the query fires or `template_capture_timeout` (45 s) elapses.
2. `extract_posts=False` during bootstrap — prevents the initial feed from
   leaking into results.
3. Replay loop: `cursor=null` (first iteration) or `end_cursor` (subsequent),
   `beforeTime = min(end_of_day(end_date), now_utc)`, `count=pagination_count`.
   `afterTime` stays `null`.
4. `__csr`/`__dyn` are spliced from organic scroll bursts every N paginations
   so replay bodies don't drift against FB's rotating tokens.
5. Default stop conditions: `EndOfFeed`, `OldestInBatchBelowStartDate` (skipped
   on iteration 1), `NoNewPostsStreak`, `MaxPostsReached`, `CursorReset`,
   `ResponseShapeError`, `MaxPaginations`, `GraphQLError`.
6. `CursorReset` triggers **multi-leg resume**: `end_date` is advanced backward
   to the oldest collected post's day and the scrape re-submits (on a fresh
   account), capped at `MAX_CURSOR_RESET_RESUMES` (5).

## Options

Common knobs (full set + pacing defaults in
`Query.ENDPOINT_REGISTRY["UserTimeline"]["modes"]["hybrid"]`):

| Param / flag | Default | Meaning |
|---|---|---|
| `pagination_count` | `3` | Posts requested per replay (matches FB UI). |
| `max_posts` / `--max-posts` | `-1` | Cap on accumulated posts (`-1` = no cap). Batch-boundary enforced. |
| `max_paginations` / `--max-paginations` | `-1` | Safety cap on replays per session. |
| `max_no_progress_streak` | `5` | Bail after N consecutive replays with no new posts. |
| `resume_from` / `--continue` | — | Resume from a prior file's `last_cursor` + seen post_ids. |

Pacing knobs (`scroll_burst_*`, `pagination_sleep_*`, `*_timeout*`) mirror FB's
fingerprint and are rarely worth overriding — see the registry.

Media can be collected during the scrape (`--download-media` /
`--media-manifest`); see [`../media_streaming.md`](../media_streaming.md).

## Usage

```python
from fbscrape import FacebookScraper

async with FacebookScraper(db="accounts.db", max_browser_sessions=2) as scraper:
    r = await scraper.user_timeline("zuck", "2024-01-01", "2025-01-01")
    print(r.result, len(r.data))
    r.save("data/posts/zuck_UserTimeline_hybrid.jsonl.gz")
```

```bash
# Open-ended — most recent N posts, no date filter
fbscrape scrape user-timeline zuck --max-posts 500

# Resume a prior scrape (merges new posts into the existing file)
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --continue
```

## Gotchas

- **Legacy short ids.** Pre-2010 accounts (Zuck = `"4"`) expose a legacy short
  `author_id` on their own posts, distinct from their modern 15-digit
  `ProfileAuthenticity.user_id`. Same person, not cross-referenceable from a
  post alone. Filter to modern ids
  (`len(author_id) >= 10 and author_id.startswith(("100", "61"))`) if you need
  to be strict.
- **`end_date` is a server-side frontier**, not a hard filter on returned posts
  — a rare recent share of an old post can appear; the flattened `created_at`
  is the authority.

## Output shape

`data = list[dict]`, one per post. Flattened via
`FacebookGraphQLParser.flatten(record, "UserTimeline")` → `_flatten_pctfrq_post`
(post_id, created_at, author_id, author_name, url, message, reactions, shares,
comments, attachments, …). See `tests/unit/test_flatten_user_timeline.py`.
