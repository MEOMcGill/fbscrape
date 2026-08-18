# CommentsList

Scrape the top-level comments on a single post.

## Overview

- **Kind:** paginated comment stream (`hybrid` mode). Top-level comments only
  (`depth=0`); replies are not collected.
- **High-level API:** `scraper.comments_list(handle, post_id, max_results=-1, **params)`
- **CLI:** `fbscrape scrape comments-list <handle>:<post_id> [--max-results N]`
- **Output:** `data = list[dict]` — one element per top-level comment.
- **Target query:** `CommentsListComponentsPaginationQuery` (CLCPQ).

## Inputs

| Field | Required | Purpose |
|---|---|---|
| `handle` | yes | Vanity handle of the post's author/page — drives the navigation URL. |
| `post_id` | yes | Numeric **or** `pfbid` form; both resolve via `/<handle>/posts/<post_id>/`. |

The base64 `feedback:<numeric_post_id>` that GraphQL needs as `variables.id` is
captured automatically from the natural CLCPQ request — callers don't supply it.

## Strategy

Same family as [GroupTimeline](group_timeline.md) hybrid, with these
differences:

1. **Single-chunk JSON, not JSONL.** CLCPQ doesn't use `@stream`/`@defer`.
   `end_cursor` is read straight from `page_info.end_cursor`; termination is
   driven by `page_info.has_next_page: false`.
2. **No date filter, no Story shape.** FB orders comments "Most Relevant"
   (algorithmic). Stop set: `EndOfFeed`, `NoNewPostsStreak`, `MaxPostsReached`
   (driven by `max_results`), `MaxPaginations`, `GraphQLError`.
3. **Dedicated pagination loop** (`_hybrid_comments_pagination_loop`) using
   `commentsAfterCursor` / `commentsAfterCount`, with `parse_comments_response`
   as the per-iteration parser. `feedLocation` (default
   `POST_PERMALINK_DIALOG`) is injected on every replay.
4. **No multi-leg cursor_reset resume.**

## Options

| Param / flag | Default | Meaning |
|---|---|---|
| `max_results` / `--max-results` | `-1` | Cap on accumulated comments (`-1` = exhaust). Batch-boundary enforced. |
| `comments_after_count` / `--comments-after-count` | `-1` | Page size (`-1` = server picks, ~10). Rarely worth overriding. |
| `feed_location` | `POST_PERMALINK_DIALOG` | `variables.feedLocation` on every replay. |
| `max_paginations` / `--max-paginations` | `-1` | Safety cap on replays. |
| `resume_from` / `--continue` | — | Resume from a prior file's cursor + seen comment ids. |

Full set in `Query.ENDPOINT_REGISTRY["CommentsList"]["modes"]["hybrid"]`.

## Usage

```bash
fbscrape scrape comments-list MarkJCarney2025:pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl
fbscrape scrape comments-list zuck:10115311901107991 --max-results 200
fbscrape scrape comments-list --input-file posts.csv
```

```python
async with FacebookScraper(db="accounts.db") as scraper:
    r = await scraper.comments_list(
        handle="MarkJCarney2025",
        post_id="pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl",
        max_results=200,   # -1 (default) = exhaust
    )
    print(f"{len(r.data)} comments, result={r.result!r}")
```

## Gotchas

- **Top-level only (v1).** Replies (`depth>0`) are not fetched. Each comment
  carries `replies_total_count` so callers can decide which threads warrant a
  future reply-fetching pass.
- **Reactions lack localized names.** `feedback.top_reactions.edges[]` ship
  `{node:{id}, reaction_count}`; the flattener maps known ids via
  `_REACTION_ID_TO_NAME`, and unknowns land in `reactions_other`.

## Output shape

`data = list[dict]`, one per comment. Flattened via
`FacebookGraphQLParser.flatten(record, "CommentsList")` →
`_flatten_commentslist_comment` (comment_id, author_id, author_name,
author_url, created_at, depth, per-reaction counts, attachments, mentions,
hashtags, external_urls, replies_total_count, …). Filename stem:
`<handle>_<post_id_truncated>_CommentsList_hybrid`. See
`tests/unit/test_flatten_commentslist.py`.
