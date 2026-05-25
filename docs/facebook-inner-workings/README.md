# How Facebook serves posts

Field notes from many hours of inspecting GraphQL traffic for `fbscrape`. The
goal of this document is the conceptual model — not the wire protocol details
(those live in the per-endpoint code) and not a tutorial on GraphQL (assumed).
Five sections, in order of how deeply they affect what the scraper has to do.

---

## 1. Every query has a name

When Facebook's UI wants more posts, it sends a `POST /api/graphql/` request.
Hundreds of different requests share that URL — the actual operation is named
in the form body as `fb_api_req_friendly_name`, and (separately) in the HTTP
header `x-fb-friendly-name`. They always agree.

Naming convention: `<Surface>Comet<What>Query`. The ones we care about:

| Surface | Friendly name | Triggered by |
|---|---|---|
| User timeline (paginated) | `ProfileCometTimelineFeedRefetchQuery` | Scrolling on a profile |
| User timeline (initial) | `ProfileCometTimelineFeedQuery` | First profile load (SSR-equivalent) |
| User profile header | `ProfileCometTimelineHeaderQuery` | Profile load — followers, name, bio |
| Group feed (paginated) | `GroupsCometFeedRegularStoriesPaginationQuery` | Scrolling in a group |
| Search results | `SearchCometResultsPaginatedResultsQuery` | Scrolling on a search results page |
| Page transparency modal | `ProfileTransparencyDialogQuery` | Clicking "About this page" |
| Profile authenticity modal | `ProfileCometDirectoryAuthenticityModalQuery` | Clicking "About this profile" |

Two consequences:

1. **Interception is name-routed, not URL-routed.** All these requests hit
   `/api/graphql/`; we discriminate by the friendly name on the form body.
2. **The same name fires from multiple actions.** PCTFRQ fires on every scroll
   *and* on date-filter changes *and* on tab switches within a profile. The
   `variables` blob (cursor, beforeTime, sortingSetting) is what changes.

You will sometimes see `Comet` replaced with `Mobile` (mobile UA flow) or
similar prefixes. Same shape, different surface.

---

## 2. Responses are streams of patches, not one JSON object

This is the most surprising part of FB's GraphQL.

A normal GraphQL response is one JSON object: `{ "data": {...} }`. Facebook
responses are usually **multiple JSON objects, one per line** — a JSONL stream
where each line is a "patch" that plugs into a specific location in the
response tree.

Why: FB's schema uses `@defer` and `@stream` directives heavily. `@defer` says
"this nested field can come later"; `@stream` says "this array can come
piecemeal." The server flushes whatever it has computed so far and continues
producing more chunks until the response is complete. This lets the page paint
the main feed while the Reels mini-feed, ad slots, and engagement counts are
still resolving in the background.

A real response (one healthy GroupTimeline pagination, 11 lines) looks like:

```
line 0:  data.node               ← skeleton: { __typename: "Group", group_feed: { edges: [] }, id: "..." }
line 1:  data.node.cursor        ← edge cursor for post #1
line 2:  data.node.cursor        ← edge cursor for post #2
line 3:  data.node.cursor        ← edge cursor for post #3
line 4:  data.comet_bf_affiliate_link_data
line 5:  data.comet_bf_affiliate_link_data
line 6:  data.page_info          ← end_cursor + has_next_page  ←── THE pagination cursor
line 7:  data.<reel>.video / attachments / ...
line 8:  data.<reel>.video / attachments / ...
line 9:  data.<instream_ad>
line 10: data.<instream_ad>
```

Each chunk declares its location via a top-level `path` field (and a `label`
field naming the deferred fragment, useful for debugging).

Three real consequences this caused us to fix in code:

- **Cursor extraction must be path-aware.** Lines 7-8 (the Reels sub-feed)
  carry their own `end_cursor` for the *Reels connection* — meaningless for
  paginating the *group feed*. A naïve "first non-empty `end_cursor`" extractor
  picks the wrong one. The fix is to prefer the shortest `path` (= top-level
  connection). See Key Design Decision 21 in `CLAUDE.md`.
- **Body parsing isn't `json.loads(body)`.** Must split on newlines and parse
  each line.
- **A "post" can be spread across multiple chunks.** The skeleton ships one
  edge; later chunks fill in its `video`, `feedback`, etc. The flattener has
  to assume any post may be partial.

The `extensions.is_final` boolean on each chunk tells you whether more chunks
are coming for that specific path — useful for knowing when to stop waiting.

---

## 3. Where a post actually lives in the response

A "post" is internally called a **Story**. Most surfaces share the same Story
shape (the "Comet" UI components), which is why one flattener generally
handles many endpoints with minor variations.

You will see two wrapper shapes depending on the chunk:

- **Shape A — connection edge** (paginated lists):
  `data.node.<connection_name>.edges[i].node = Story`
  - GroupTimeline bootstrap: `data.node.group_feed.edges[].node`
  - UserTimeline: `data.node.timeline_list_feed_units.edges[].node`
- **Shape B — direct story** (per-edge patches):
  `data.node = Story`
  - GroupTimeline stream lines once pagination is in flight

Inside a Story, the important fields:

```
story.id                                 # numeric story id
story.post_id                            # the post_id we dedup on
story.url                                # canonical post URL
story.creation_time                      # WRAPPING timestamp (this share's timestamp)
story.attached_story.creation_time       # INNER timestamp (e.g. the original post being shared)
story.actors[]                           # author(s)
story.attachments[]                      # photos, videos, links, reels, albums

# UI sections (these are nested deep — paths abbreviated for readability):
story.comet_sections
   .context_layout.story.comet_sections.metadata[]   # timestamp / audience / music
   .message.story.message.text                       # post text
   .message.story.message.ranges[]                   # typed entities: hashtags, mentions, urls

story.feedback.comet_ufi_summary_and_actions_renderer.feedback
   .reaction_count / comment_count / share_count / ...
```

Two things worth knowing:

**Wrapping vs inner timestamps.** A post that *shares* another post carries
its own `creation_time` (when the share happened) and a nested
`attached_story.creation_time` (when the original was posted). We always use
the wrapping one for date filtering — otherwise a recent share of a 2018 post
could trip a "we reached the start date" stop condition years too early.

**Metadata dispatch by `__typename`.** The metadata list is non-deterministic:

```
story.comet_sections.context_layout.story.comet_sections.metadata = [
  { __typename: "CometFeedStoryLongerTimestampStrategy", story: { creation_time: ... } },
  { __typename: "CometFeedStoryAudienceStrategy", story: { audience: ... } },
  { __typename: "CometFeedStoryAttachedMusicStrategy", ... },
]
```

The same logical field (timestamp) can appear under different `__typename`s
depending on how FB chose to render the story — e.g.
`CometFeedStoryLongerTimestampStrategy` vs `CometFeedStoryMinimizedTimestampStrategy`,
with an identical inner payload. FB renames and reshuffles these strategies
periodically, so the flattener walks the list and matches by typename rather
than positional index. Each "kind" we extract has a tuple of candidate
typenames, checked in order, first match wins.

---

## 4. Cursors are positions, not IDs

A cursor looks like an opaque blob:

```
Cg8TZXhpc3RpbmdfdW5pdF9jb3VudAIHDwtyZWFsX2N1cnNvcg+fQVFI...
```

It's base64-encoded length-prefixed binary. Decode it and you get a struct:

```
existing_unit_count   = 7682              # how many posts you've already been served
real_cursor           = <opaque, signed>  # the actually-validated server state
header_global_count   = 1
main_feed_position    = 7                 # where you are in the feed
feed_ordering         = ranked_interest_communities
is_evergreen_cursor   = false
group_feed_version    = V2
demoted_post_ids      = [...]
```

Three things follow:

**`real_cursor` is HMAC-signed.** It encodes a server-side ranker checkpoint
(seed, ranker state, demotion list). Tampering with the surrounding fields
without rebuilding `real_cursor` fails validation — FB returns an empty batch
or an error. You can read the cursor, you cannot edit it.

**Cursors are sort-specific.** A TOP_POSTS cursor doesn't work as a
CHRONOLOGICAL cursor — different rankers, different state. Switching
`sortingSetting` mid-scrape effectively starts a new feed.

**Per-edge vs page cursors.** Each post in the response carries its own
`cursor` field (the per-edge cursor); the `page_info.end_cursor` is the cursor
to send for the *next batch*. They look identical in format but serve
different roles. We almost always want `page_info.end_cursor`. The per-edge
cursors are useful for "anchor at post N" semantics — e.g. our auto-unstick
logic picks the rank-3 oldest post's per-edge cursor when a `--continue` resume
gets stuck on dedup-saturated data.

This is also why "skip the broken post" doesn't work when we hit a
`field_exception`. There's no cursor that means "skip the post FB couldn't
resolve" — the cursor we have is the one that *leads to* it, and the response
that fails carries no usable forward cursor (just `page_info: null`).

---

## 5. Sort modes change what's served, not just the order

Group feeds accept a `sortingSetting` GraphQL variable. Three known-valid values:

| Sort | What it means | Order | Cursor stability |
|---|---|---|---|
| `TOP_POSTS` (FB UI default) | Algorithmic ranker — engagement × recency × personalization | Non-monotonic in time. Popular old posts can sit at high rank. | Low: re-ranks shift over time as engagement signals change |
| `CHRONOLOGICAL` | Descending by creation_time (the stream tail at least) | Strict reverse-chronological *for the tail*. Bootstrap edge can carry an out-of-order "highlight" injected by FB | High: time order is immutable |
| `RECENT_ACTIVITY` | Sorted by recency of last comment/reaction | An old post with new activity floats to top | Medium |

The choice has three knock-on effects:

**It changes what posts you see.** TOP_POSTS skews toward viral / personalized
posts and may *never* surface low-engagement posts even with infinite
pagination. CHRONOLOGICAL surfaces everything in order. RECENT_ACTIVITY shows
"what's alive" rather than "what was posted."

**It changes which stop conditions are sensible.** "Bail when oldest post in
batch < start_date" only works when posts arrive in time order — i.e. under
CHRONOLOGICAL. Under TOP_POSTS, post #5 might be from 2018 and post #6 from
yesterday, so the date-monotonic stop is useless; instead we count
"consecutive posts outside the window" before bailing.

**It changes fingerprint and risk.** TOP_POSTS matches what FB's UI sends by
default, so a scraper using it looks like a real user. CHRONOLOGICAL is rarely
chosen in the UI (it's behind a menu) — empirically, sustained scraping with
`sortingSetting=CHRONOLOGICAL` on group feeds correlates with account
suspensions. This is why our GroupTimeline default flipped from CHRONOLOGICAL
to TOP_POSTS, even though TOP_POSTS gives us best-effort coverage instead of
exhaustive.

There's a parallel here to search engines: `?sort=relevance` vs `?sort=date`
on a search page changes both *what* matches you see and *how* you'd judge
"have I seen everything yet." Same logic applies to FB feeds — the sort is
fundamentally a different question being asked of FB's index, not just a
reordering of the same answer set.

---

## 6. User-vs-Page is not a post-level distinction

Two of `fbscrape`'s endpoints look at "the same kind of thing" from very
different angles:

- `ProfileAuthenticity(user_id)` — info on a User profile (join date, category,
  meta-verified, transparency-link presence).
- `PageTransparency(page_id)` — info on the linked Page (creation date, name
  history, admin countries, paid-ad activity).

For any account that's "a person with a public-facing role" — politicians,
local businesses, news outlets, creators, low-quality content farms — both
exist. FB models this as **a User entity with a separate Page entity attached**.
The Page is where the ads/transparency live; the User is what posts on the
timeline and gets followed. They have **different numeric ids**.

The non-obvious bit: **FB doesn't surface this distinction at the post layer**.
A `UserTimeline` post from a Page-backed account looks structurally identical
to one from a pure personal profile:

```
actors[0].__typename       = "User"      # always
actors[0].__isActor        = "User"
actors[0].__isEntity       = "User"
feedback.owning_profile.__typename = "User"
actors[0].id               = <user_id>   # never the page_id
```

Empirically, 142 of 161 distinct authors in the canadian-fb-slop dataset
have a populated `delegate_page_id` (so they're Page-backed); zero of their
posts report `author_type == "Page"`. The post payload is from FB's
*timeline/feed* graph, which is owner-keyed on User entities — the linked
Page is reachable only by a separate query.

Three practical consequences:

**Don't filter `author_type == "Page"` to find Page-backed accounts.** It
will return zero rows even on a dataset that's almost entirely Page-backed.
The signal isn't there.

**`PageTransparency(page_id=author_id)` won't work for these accounts.**
FB resolves `page(id: <user_id>)` to no node and returns `data.page = null`.
You need the actual Page id, which lives on the User's ProfileAuthenticity
record as `delegate_page_id`.

**The right pipeline is two-stage and unconditional.** Even though 88% of
the canadian dataset is Page-backed, you can't tell which 88% from posts
alone — you have to call `ProfileAuthenticity` on every distinct `author_id`,
then check `delegate_page_id`:

```
post.author_id   ──►   ProfileAuthenticity   ──►   delegate_page_id?
                              │                         │
                              │                         ├── populated
                              │                         │       │
                              │                         │       ▼
                              │                         │   PageTransparency
                              │                         │   (page_id=delegate_page_id)
                              │                         │
                              │                         └── null → stop
                              ▼
                       (always written:
                        join_date, category,
                        meta_verified, …)
```

`author_id` is in the same namespace as `ProfileAuthenticity.user_id` for
modern accounts (15-digit ids starting with `100…` or `61…`). The one edge
case is pre-2010 accounts (Zuck = `"4"`), whose post-level `author_id` and
modern `ProfileAuthenticity.user_id` live in different namespaces — those
won't cross-reference from a post alone.
