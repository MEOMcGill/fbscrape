# Endpoint strategy deep dives → moved

Per-endpoint strategy, options, and usage now live in one guide per endpoint
under [`../endpoints/`](../endpoints/README.md):

- Paginated: [UserTimeline](../endpoints/user_timeline.md),
  [Search](../endpoints/search.md),
  [GroupTimeline](../endpoints/group_timeline.md),
  [CommentsList](../endpoints/comments_list.md)
- Single-shot: [PostDetail](../endpoints/post_detail.md),
  [PageTransparency](../endpoints/page_transparency.md),
  [ProfileAuthenticity](../endpoints/profile_authenticity.md),
  [ProfileInfo](../endpoints/profile_info.md),
  [ProfileAbout](../endpoints/profile_about.md),
  [GroupInfo](../endpoints/group_info.md),
  [GroupAbout](../endpoints/group_about.md)

Each guide covers Overview → Inputs → Strategy → Options → Usage → Gotchas →
Output shape. For the cross-cutting architecture (how a scrape flows through
`FacebookScraper` → `WorkerPool` → `Worker` → `BrowserSession`) see
[`overview.md`](overview.md).
