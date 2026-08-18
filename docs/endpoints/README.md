# Endpoint guides

One guide per endpoint — the single home for everything about it: what it
returns, the identifiers it needs, how the scrape works (strategy), the tunable
options, usage recipes, and gotchas. For the compact registry table and wiring
checklist see [`../../CLAUDE.md`](../../CLAUDE.md); for the cross-cutting
architecture see [`../architecture/overview.md`](../architecture/overview.md).

Every endpoint runs in `hybrid` mode: capture a real request template from the
live page, then replay it (a GraphQL POST for the feed/comment endpoints, or a
server-rendered document read for the header/about endpoints).

| Endpoint | Kind | Required | Guide |
|---|---|---|---|
| UserTimeline | paginated posts | `handle` | [user_timeline.md](user_timeline.md) |
| Search | paginated posts | `query_text` | [search.md](search.md) |
| GroupTimeline | paginated posts | `handle` | [group_timeline.md](group_timeline.md) |
| CommentsList | paginated comments | `handle`, `post_id` | [comments_list.md](comments_list.md) |
| PostDetail | single post | `handle`, `post_id` | [post_detail.md](post_detail.md) |
| PageTransparency | single-shot | `page_id` | [page_transparency.md](page_transparency.md) |
| ProfileAuthenticity | single-shot | `user_id` | [profile_authenticity.md](profile_authenticity.md) |
| ProfileInfo | single-shot | `handle` | [profile_info.md](profile_info.md) |
| ProfileAbout | single-shot | `handle` | [profile_about.md](profile_about.md) |
| GroupInfo | single-shot | `handle` | [group_info.md](group_info.md) |
| GroupAbout | single-shot | `handle` | [group_about.md](group_about.md) |

Every guide follows the same skeleton: **Overview → Inputs → Strategy →
Options → Usage → Gotchas → Output shape.**

Collecting media during a scrape (feed endpoints) is covered separately in
[`../media_streaming.md`](../media_streaming.md).
