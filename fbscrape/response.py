"""
Response interception and Facebook GraphQL parsing
"""

import base64
import json
import os
import re
import traceback
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from playwright.async_api import Page, Response, Error as PlaywrightError
from fbscrape.utils import parse_json_or_jsonl
from .logger import logger


# TEMP: resource types whose response bodies are textual and worth keeping verbatim
# for Path B investigation. Binary types (image, font, media) are recorded with
# metadata + size only — bytes are not stored. Remove when investigation is done.
_TEXT_RESOURCE_TYPES = frozenset({
    "xhr", "fetch", "document", "script", "stylesheet", "websocket", "preflight",
})


def _g(obj, *keys, default=None):
    """Safe nested getter: walks dict keys / list indices, returns `default` on any miss."""
    for k in keys:
        if obj is None:
            return default
        if isinstance(k, int):
            if isinstance(obj, list) and -len(obj) <= k < len(obj):
                obj = obj[k]
            else:
                return default
        elif isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return default
    return obj if obj is not None else default


_ABBREVIATED_COUNT_RE = re.compile(
    r"^([\d,]+(?:\.\d+)?)([KMB]?)", re.IGNORECASE
)
_ABBREVIATED_COUNT_MULTIPLIERS = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_abbreviated_count(text) -> int | None:
    """Parse FB's abbreviated count strings (e.g. "121M followers", "22K",
    "1 following") into an approximate integer, stripping the trailing
    word. Returns `None` if `text` doesn't start with a recognizable count.

    FB only ever ships these pre-rounded to 1-3 significant digits — this
    recovers the order of magnitude for sorting/comparison, not the exact
    live count (e.g. "121M" becomes exactly 121_000_000, not whatever the
    real figure is).

    The suffix letter must be glued directly to the digits with no space —
    real abbreviations are always formatted that way ("121M", "43K"). This
    matters because FB ships *exact*, unabbreviated counts for small
    numbers as a plain "<digits> <word>" string (e.g. "827 members"); if
    the regex allowed whitespace before the suffix, it would greedily
    match the leading letter of that trailing word (the "m" in "members")
    as if it meant "million", inflating 827 into 827,000,000."""
    if not isinstance(text, str):
        return None
    m = _ABBREVIATED_COUNT_RE.match(text.strip())
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    multiplier = _ABBREVIATED_COUNT_MULTIPLIERS[m.group(2).upper()]
    return int(round(number * multiplier))


def _resolve_story(post: dict) -> dict | None:
    """Walk a raw post dict to its canonical Story node.

    Shapes:
      A) initial page load — `node` is a container with edges[].node = Story.
         Two container keys are recognized: `timeline_list_feed_units`
         (UserTimeline PCTFRQ) and `group_feed` (GroupTimeline GCFRSPQ).
      B) pagination / fan-out — `node` IS the Story directly.

    `parse_timeline_response` fans Shape A entries into per-Story entries
    before they reach this function, so the Shape A branch below is mostly
    defensive — it lets callers pass a raw response line directly.
    """
    node = post.get("node") or {}
    for container_key in ("timeline_list_feed_units", "group_feed"):
        if container_key in node:
            return _g(node, container_key, "edges", 0, "node")
    if "post_id" in node:
        return node
    return None


# Map FB's StoryAttachment style __typename to a short type label. Used by
# _extract_attachment to dispatch type-specific field extraction. New style
# renderers default to "unknown" — extend the map when seen.
_ATTACHMENT_TYPE_BY_STYLE = {
    "StoryAttachmentPhotoStyleRenderer":                    "photo",
    "StoryAttachmentUnifiedLightweightVideoStyleRenderer":  "video",
    "StoryAttachmentVideoStyleRenderer":                    "video",
    "StoryAttachmentAnimatedImageShareStyleRenderer":       "video",
    "StoryAttachmentAlbumStyleRenderer":                    "album",
    "StoryAttachmentShareStyleRenderer":                    "link",
    "StoryAttachmentShareMediumStyleRenderer":              "link",
    "StoryAttachmentFBReelsStyleRenderer":                  "reel_share",
    "StoryAttachmentUnavailableStyleRenderer":              "unavailable",
    "StoryAttachmentCustomUnavailableStyleRenderer":        "unavailable",
}

# Story.comet_sections.context_layout.story.comet_sections.metadata[] is a
# non-deterministic list of typed strategies; dispatch by __typename rather
# than positional index. Each lookup accepts a TUPLE of candidate typenames
# so FB's sibling strategy renames (e.g. `Longer` vs `Minimized` for the
# timestamp — same `story.creation_time` payload, different __typename) are
# absorbed without code changes. First-match wins, so order = preference.
_METADATA_TIMESTAMP_TYPENAMES = (
    "CometFeedStoryLongerTimestampStrategy",
    "CometFeedStoryMinimizedTimestampStrategy",
)
_METADATA_AUDIENCE_TYPENAMES  = ("CometFeedStoryAudienceStrategy",)
_METADATA_MUSIC_TYPENAMES     = ("CometStoryMusicPostLevelAttributionStrategy",)

# `comet_ufi_summary_and_actions_renderer.feedback` ships in two variants:
#
#   A) "Full" — totals live directly on the feedback dict:
#        feedback.reaction_count.count
#        feedback.share_count.count
#        feedback.comments_count_summary_renderer.feedback.comment_rendering_instance.comments.total_count
#
#   B) "Thinned" — top-level totals are absent; the same numbers are nested
#      inside `adaptive_ufi_action_renderers[i].feedback.<thing>`, dispatched
#      by `__typename` (same pattern as metadata[] strategies). Empirically ~60%
#      of UserTimeline responses in the wild ship this shape.
#
# Same FB rename-tolerance pattern as _METADATA_*_TYPENAMES: tuples, first match wins.
_REACTION_RENDERER_TYPENAMES = ("UFIStoryReactActionRenderer",)
_COMMENT_RENDERER_TYPENAMES  = ("UFICommentActionRenderer",)
_SHARE_RENDERER_TYPENAMES    = ("XFBUFIAdaptiveShareActionRenderer",)


def _pick_progressive_video(media: dict | None) -> str | None:
    """Return the highest-priority progressive mp4 URL from a media dict, if any."""
    vdrr = _g(media or {}, "videoDeliveryResponseFragment", "videoDeliveryResponseResult") or {}
    for p in vdrr.get("progressive_urls") or []:
        url = p.get("progressive_url")
        if url:
            return url
    return None


class FacebookGraphQLParser:
    """Parses Facebook GraphQL responses and flattens posts into output rows.

    The flattening pipeline is layered: per-aspect `_extract_*` methods consume
    a canonical Story dict and return a partial dict. An endpoint-specific
    orchestrator (`_flatten_<endpoint>_post`) composes them into the final row.
    Most FB surfaces share the same Story shape (the "Comet" UI), so the
    extractors are reused across endpoints — adding a new endpoint flattener
    is mostly an orchestration call, plus a registry entry.
    """

    # Endpoint name → orchestrator method name on this class. Public flatten()
    # routes via this. Adding an endpoint = one new orchestrator + one row here.
    ENDPOINT_FLATTENERS: dict[str, str] = {
        "UserTimeline": "_flatten_pctfrq_post",
        "Search": "_flatten_pctfrq_post",
        "GroupTimeline": "_flatten_grouptimeline_post",
        "PageTransparency": "_flatten_pagetransparency_record",
        "ProfileAuthenticity": "_flatten_profile_authenticity_record",
        "CommentsList": "_flatten_commentslist_comment",
        "PostDetail": "_flatten_postdetail_record",
        "ProfileInfo": "_flatten_profile_info_record",
        "ProfileAbout": "_flatten_profile_about_record",
        "GroupInfo": "_flatten_group_info_record",
        "GroupAbout": "_flatten_group_about_record",
    }

    # FB's canonical reaction ids — stable per reaction type, used as edge
    # `node.id` in `top_reactions.edges[]` on Comment.feedback (no
    # `localized_name` is shipped on the CommentsList endpoint, so we map
    # by id). Source: empirical observation across captures + FB's published
    # reaction emoji ids. Unknown ids are passed through under their id key
    # in the breakdown dict so we don't silently drop them.
    _REACTION_ID_TO_NAME: dict[str, str] = {
        "1635855486666999": "like",
        "1678524932434102": "love",
        "115940658764963":  "haha",
        "1885436228231891": "wow",
        "1538317126927996": "sad",
        "478547315650144":  "angry",
        "613557422527858":  "care",
    }

    # Edge-container keys that wrap one or more Stories. `parse_timeline_response`
    # fans out their edges into per-Story `posts` entries so each downstream
    # consumer (flatten, _hybrid_iter_wrapping_creation_times, add_posts dedup)
    # sees exactly one Story per entry regardless of how FB streamed the body.
    _STORY_EDGE_CONTAINERS = ("timeline_list_feed_units", "group_feed")

    # ----- runtime parsing (used by ResponseInterceptor) -----

    def parse_timeline_response(self, body: bytes, url: str) -> dict | None:
        """Parse a Facebook GraphQL timeline response body into `{posts: [...]}`.

        Each returned entry has shape `{node: Story, ...}` — a single canonical
        Story per entry. Shape-A response lines (a container node wrapping
        multiple `edges[].node` Stories) are fanned out into one entry per
        edge; Shape-B response lines (a Story delivered as `data.node`
        directly) are preserved with their original `{node, cursor, ...}`
        keys. Both forms resolve cleanly through `_resolve_story`.

        Returns None on any decode/parse failure.
        """
        try:
            response_data = parse_json_or_jsonl(body.decode('utf-8'))
            posts = []
            for data_line in response_data:
                if not isinstance(data_line, dict) or 'data' not in data_line:
                    continue
                payload = data_line['data']
                if not isinstance(payload, dict):
                    continue
                node = payload.get('node')
                if not isinstance(node, dict):
                    continue
                # Shape B: data.node IS the Story directly.
                if (node.get('__typename') == 'Story'
                        and node.get('__isFeedUnit') == 'Story'
                        and isinstance(node.get('post_id'), str)
                        and node['post_id']):
                    posts.append(payload)
                    continue
                # Shape A: data.node is a container with edges[].node = Story.
                # Recognised containers: timeline_list_feed_units (UserTimeline),
                # group_feed (GroupTimeline). Fan out so each Story becomes its
                # own entry — otherwise the flattener's edges[0] fallback would
                # silently drop edges 1..N.
                for container_key in self._STORY_EDGE_CONTAINERS:
                    container = node.get(container_key)
                    if not isinstance(container, dict):
                        continue
                    for edge in (container.get('edges') or []):
                        inner = edge.get('node') if isinstance(edge, dict) else None
                        if (isinstance(inner, dict)
                                and inner.get('__typename') == 'Story'
                                and isinstance(inner.get('post_id'), str)
                                and inner['post_id']):
                            posts.append({'node': inner})
                    break  # only one container key applies per response line

            return {'posts': posts}

        except json.JSONDecodeError as e:
            logger.error(f"[PARSER ERROR] Failed to decode JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"[PARSER ERROR] {e}")
            logger.error(traceback.print_exc())
            return None

    def parse_comments_response(self, body: bytes, url: str) -> dict | None:
        """Parse a CommentsListComponentsPaginationQuery response into
        `{comments: [...], end_cursor: str | None, has_next_page: bool}`.

        The response is single-chunk JSON (no JSONL / @defer streaming):
        `data.node.comment_rendering_instance_for_feed_location.comments` is
        a Relay connection with `edges[].node` Comment nodes, `edges[].cursor`
        being null (file-level cursor), and `page_info.end_cursor` /
        `has_next_page` driving pagination. The parent post's base64
        `feedback:<id>` is at `data.node.id` (top-level).

        Returns None on decode/parse failure.
        """
        try:
            response_data = parse_json_or_jsonl(body.decode('utf-8'))
            comments: list[dict] = []
            end_cursor: str | None = None
            has_next_page: bool = False
            parent_feedback_id: str | None = None
            for data_line in response_data:
                if not isinstance(data_line, dict) or 'data' not in data_line:
                    continue
                payload = data_line['data']
                if not isinstance(payload, dict):
                    continue
                node = payload.get('node')
                if not isinstance(node, dict):
                    continue
                if node.get('__typename') != 'Feedback':
                    continue
                if parent_feedback_id is None and isinstance(node.get('id'), str):
                    parent_feedback_id = node['id']
                cri = node.get('comment_rendering_instance_for_feed_location') or {}
                connection = cri.get('comments') or {}
                for edge in connection.get('edges') or []:
                    inner = edge.get('node') if isinstance(edge, dict) else None
                    if isinstance(inner, dict) and inner.get('id'):
                        # Synthesize a top-level `post_id` from the numeric
                        # comment id (`legacy_fbid`) so the interceptor's
                        # generic post-id dedup (`add_posts`) and the resume
                        # streamer (`_stream_resume_state`) both work without
                        # endpoint-specific branches. Falls back to the b64
                        # `id` if `legacy_fbid` is missing (defensive).
                        synthetic_pid = (
                            inner.get('legacy_fbid') or inner.get('id')
                        )
                        # Stash the parent feedback id on each comment so
                        # the flattener can recover it without the caller
                        # threading it through.
                        comments.append({
                            'node': inner,
                            'post_id': synthetic_pid,
                            '_parent_feedback_id_b64': parent_feedback_id,
                        })
                page_info = connection.get('page_info') or {}
                if page_info.get('end_cursor'):
                    end_cursor = page_info['end_cursor']
                if page_info.get('has_next_page'):
                    has_next_page = True
            return {
                'comments': comments,
                'end_cursor': end_cursor,
                'has_next_page': has_next_page,
                'parent_feedback_id_b64': parent_feedback_id,
            }

        except json.JSONDecodeError as e:
            logger.error(f"[PARSER ERROR] Failed to decode JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"[PARSER ERROR] {e}")
            logger.error(traceback.print_exc())
            return None

    def parse_search_response(self, body: bytes, url: str) -> dict | None:
        """Parse a SearchCometResultsPaginatedResultsQuery response body into
        `{posts: [...]}`.

        Each entry has shape `{node: Story, post_id: str}`. Stories live at
        `data.serpResponse.results.edges[].rendering_strategy.view_model
        .click_model.story` in the main (non-deferred) chunk only — deferred
        lines carry partial media/feedback patches for the same edges and are
        skipped. Non-story edges (SearchSimpleModuleViewModel, etc.) are
        silently dropped (no story or post_id present).

        Returns None on any decode/parse failure.
        """
        try:
            response_data = parse_json_or_jsonl(body.decode('utf-8'))
            posts = []
            for data_line in response_data:
                if not isinstance(data_line, dict) or 'data' not in data_line:
                    continue
                if data_line.get('path') is not None:
                    continue  # skip deferred patches — full Story objects not present
                serp = data_line['data'].get('serpResponse')
                if not isinstance(serp, dict):
                    continue
                results = serp.get('results')
                if not isinstance(results, dict):
                    continue
                for edge in (results.get('edges') or []):
                    if not isinstance(edge, dict):
                        continue
                    rm = edge.get('rendering_strategy')
                    if not isinstance(rm, dict):
                        continue
                    vm = rm.get('view_model')
                    if not isinstance(vm, dict):
                        continue
                    cm = vm.get('click_model')
                    if not isinstance(cm, dict):
                        continue
                    story = cm.get('story')
                    if (isinstance(story, dict)
                            and story.get('__typename') == 'Story'
                            and isinstance(story.get('post_id'), str)
                            and story['post_id']):
                        posts.append({'node': story, 'post_id': story['post_id']})
            return {'posts': posts}

        except json.JSONDecodeError as e:
            logger.error(f"[PARSER ERROR] Failed to decode JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"[PARSER ERROR] {e}")
            logger.error(traceback.print_exc())
            return None

    def is_post_node(self, node: dict) -> bool | None:
        """
        Determine if a node is a Facebook post.

        Recognised response shapes:
          A) initial page load — `data.node` is a container (User/Group)
             with `edges[].node` Stories. Container keys: `timeline_list_feed_units`
             (UserTimeline PCTFRQ), `group_feed` (GroupTimeline GCFRSPQ).
          B) pagination — `data.node` IS the Story directly, carrying a
             top-level `post_id` and `__isFeedUnit: "Story"`.

        Kept as a public helper; `parse_timeline_response` no longer routes
        through it (it inlines the shape checks during fan-out).

        Returns:
            bool indicating if node is a post, or None if parsing fails.
        """
        try:
            if not isinstance(node, dict):
                return False
            # Shape B: paginated Story node delivered as data.node directly.
            if (node.get('__typename') == 'Story'
                    and node.get('__isFeedUnit') == 'Story'
                    and isinstance(node.get('post_id'), str)
                    and node['post_id']):
                return True
            # Shape A: any recognised edge-container with at least one Story.
            for container_key in self._STORY_EDGE_CONTAINERS:
                container = node.get(container_key)
                if not isinstance(container, dict):
                    continue
                for edge in (container.get('edges') or []):
                    inner = edge.get('node') if isinstance(edge, dict) else None
                    if isinstance(inner, dict) and inner.get('__typename') == 'Story':
                        return True
            return False

        except Exception as e:
            logger.error(f"Failed to parse post node: {e}")
            return None

    # `<script type="application/json" data-sjs>…</script>` blobs carry FB's
    # server-rendered Relay payloads (RelayPrefetchedStreamCache). A post
    # permalink server-renders its Story into one of these rather than firing a
    # GraphQL XHR, so PostDetail reads the document instead of a response body.
    _SJS_SCRIPT_RE = re.compile(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S
    )

    # Relay keys under which a permalink query delivers its *subject* Story.
    # A permalink page also embeds neighbour posts (other stories by the same
    # author / "related" units), so the subject must be distinguished from
    # those — it's the Story sitting directly under one of these root keys.
    _PERMALINK_ROOT_KEYS = ("node_v2", "node")

    def _iter_story_nodes(self, obj, parent_key=None):
        """Yield `(story, parent_key)` for every Comet `Story` node in `obj`.

        `parent_key` is the dict key the Story hangs off — used to spot the
        permalink's *subject* Story (parent in `_PERMALINK_ROOT_KEYS`) vs.
        neighbour posts also embedded in the document.
        """
        if isinstance(obj, dict):
            if obj.get("__typename") == "Story":
                yield obj, parent_key
            for k, v in obj.items():
                yield from self._iter_story_nodes(v, k)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._iter_story_nodes(v, parent_key)

    def extract_permalink_story(self, html: str, post_id: str) -> dict | None:
        """Extract a post's Story from a permalink page's server-rendered HTML.

        FB embeds the post as a Comet `Story` node inside a
        `<script type="application/json">` Relay payload (the subject sits at
        `data.node_v2` / `data.node`). Returns it wrapped as `{"node": story}`
        — the same Shape-B entry `parse_timeline_response` produces — so the
        existing flatteners consume it unchanged.

        Selection handles two wrinkles:
          - A permalink document embeds *several* Story nodes: the subject plus
            neighbour posts and nested `comet_sections.content.story` fragments.
          - `post_id` may be the pfbid form, while the rendered Story always
            carries the *numeric* id — so an exact id match can't be required.

        Strategy: prefer a Story whose numeric `post_id` matches exactly; else
        fall back to the Story sitting under a permalink root key (`node_v2` /
        `node`) — that's the page's subject regardless of id form. Ties broken
        by richness (most top-level keys = the fully-hydrated node).

        Returns `{"node": story}`, or `None` if no subject Story is found (post
        deleted, not visible to the account, or shape drift).
        """
        target = str(post_id)
        candidates: list[tuple[dict, bool, int]] = []  # (story, is_root, nkeys)
        seen_ids: set[int] = set()
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if '"__typename":"Story"' not in blob.replace(" ", ""):
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for story, parent_key in self._iter_story_nodes(payload):
                if "comet_sections" not in story or not story.get("post_id"):
                    continue
                if id(story) in seen_ids:
                    continue
                seen_ids.add(id(story))
                candidates.append(
                    (story, parent_key in self._PERMALINK_ROOT_KEYS, len(story))
                )

        def _richest(cands: list[tuple[dict, bool, int]]) -> dict:
            return max(cands, key=lambda c: c[2])[0]

        exact = [c for c in candidates if str(c[0]["post_id"]) == target]
        if exact:
            return {"node": _richest(exact)}

        roots = [c for c in candidates if c[1]]
        if roots:
            logger.debug(
                f"[PARSER] permalink post_id={post_id}: no exact id match "
                f"(pfbid or redirect); using root Story "
                f"post_id={_richest(roots).get('post_id')}"
            )
            return {"node": _richest(roots)}

        logger.warning(
            f"[PARSER] No subject Story for post_id={post_id} in permalink "
            f"document ({len(html)} bytes)"
        )
        return None

    def _iter_profile_nodes(self, obj):
        """Yield every dict in `obj` that looks like a rendered profile-header
        node (User or Page) — identified by carrying `profile_social_context`
        alongside an `id`, the shape FB emits for both surfaces under
        `profile_header_renderer.user` in the bootstrap payload."""
        if isinstance(obj, dict):
            if obj.get("id") is not None and "profile_social_context" in obj:
                yield obj
            for v in obj.values():
                yield from self._iter_profile_nodes(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._iter_profile_nodes(v)

    def extract_profile_info(self, html: str, handle: str | None = None) -> dict | None:
        """Extract a profile's header info from a profile page's server-rendered HTML.

        Like PostDetail, FB renders this directly into a `<script
        type="application/json">` BigPipe bootstrap payload
        (`profile_header_renderer.user`) rather than firing a dedicated
        GraphQL XHR — no replay needed, just parse the document.

        A profile page can embed more than one profile-shaped node (e.g.
        "People you may know" sidebar suggestions also carry
        `profile_social_context`), so the subject is selected by preferring a
        node whose `url` contains the navigated `handle`, falling back to the
        most fully-hydrated (most keys) candidate — same tie-break principle
        as `extract_permalink_story`.

        Returns the selected node, or `None` if none found (private profile,
        logged out, or shape drift).
        """
        candidates: list[dict] = []
        seen_ids: set[int] = set()
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if "profile_social_context" not in blob:
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in self._iter_profile_nodes(payload):
                if id(node) in seen_ids:
                    continue
                seen_ids.add(id(node))
                candidates.append(node)

        if not candidates:
            logger.warning(
                f"[PARSER] No profile node found for handle={handle!r} "
                f"in document ({len(html)} bytes)"
            )
            return None

        if handle:
            needle = f"/{handle.strip('/').lower()}"
            url_matches = [
                c for c in candidates
                if isinstance(c.get("url"), str) and needle in c["url"].lower()
            ]
            if url_matches:
                return max(url_matches, key=len)

        return max(candidates, key=len)

    def _iter_about_app_sections(self, obj):
        """Yield each About app-section entry
        (`{name, section_type, all_collections, activeCollections}`) found
        anywhere in `obj` — identified by carrying both `all_collections`
        (the sub-tab directory) and `activeCollections` (whichever sub-tab
        is populated in the current document) side by side."""
        if isinstance(obj, dict):
            if "all_collections" in obj and "activeCollections" in obj:
                yield obj
            for v in obj.values():
                yield from self._iter_about_app_sections(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._iter_about_app_sections(v)

    @staticmethod
    def _tab_key_from_url(url) -> str | None:
        """Normalize an About sub-tab URL to its `tab_key`, regardless of
        whether FB rendered it query-style (`?...&sk=directory_contact_info`
        — observed for numeric-id profiles) or path-style
        (`/<handle>/directory_contact_info` — observed for vanity handles);
        both forms occur for the same tab_key depending on account type."""
        if not isinstance(url, str):
            return None
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "sk" in qs:
            return qs["sk"][0]
        segments = [s for s in parsed.path.split("/") if s]
        return segments[-1] if segments else None

    def extract_profile_about_collections(self, html: str) -> dict:
        """Extract the About sub-tab directory (`{tab_key: absolute_url}`)
        from an About-family page — the landing page or any sub-tab; the
        directory is embedded on all of them via `all_collections`.

        Returns `{}` if not found (account has no About tabs, or shape drift).
        """
        directory: dict = {}
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if "all_collections" not in blob or "activeCollections" not in blob:
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for section in self._iter_about_app_sections(payload):
                for node in (_g(section, "all_collections", "nodes", default=[]) or []):
                    if not isinstance(node, dict):
                        continue
                    tab_key = self._tab_key_from_url(node.get("url"))
                    if tab_key:
                        directory[tab_key] = node["url"]
            if directory:
                break
        return directory

    def extract_profile_about_sections(self, html: str) -> list:
        """Extract the populated `profile_field_sections` for whichever
        About sub-tab is active in this document (`activeCollections`) — the
        fields FB actually rendered for that tab (e.g. phone/email for
        Contact info, address/hours for Details). A sub-tab's fields only
        populate when that sub-tab itself was navigated to directly — FB
        doesn't server-render every section together.

        Returns `[]` if none found.
        """
        sections: list = []
        seen_ids: set = set()
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if "profile_field_sections" not in blob:
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for section_group in self._iter_about_app_sections(payload):
                for coll in (_g(section_group, "activeCollections", "nodes", default=[]) or []):
                    for sec in (_g(coll, "style_renderer", "profile_field_sections", default=[]) or []):
                        if not isinstance(sec, dict) or id(sec) in seen_ids:
                            continue
                        seen_ids.add(id(sec))
                        sections.append(sec)
        return sections

    def _iter_group_nodes(self, obj):
        """Yield every dict in `obj` that looks like a rendered group-header
        node — identified by `__typename == "Group"` alongside
        `viewer_join_state`, a field only present on the fully-hydrated
        header (stub `Group` references elsewhere in the document carry
        just `id`/`__typename`)."""
        if isinstance(obj, dict):
            if obj.get("__typename") == "Group" and "viewer_join_state" in obj:
                yield obj
            for v in obj.values():
                yield from self._iter_group_nodes(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._iter_group_nodes(v)

    def extract_group_info(self, html: str, handle: str | None = None) -> dict | None:
        """Extract a group's header info from a group page's server-rendered HTML.

        Like `extract_profile_info`, FB renders this directly into a
        `<script type="application/json">` BigPipe bootstrap payload rather
        than firing a dedicated GraphQL XHR — no replay needed, just parse
        the document. Present identically on both the group landing page
        (`/groups/<handle>/`) and the About page (`/groups/<handle>/about/`).

        The subject is selected by preferring a node whose `group_address`
        matches the navigated `handle` exactly, falling back to the most
        fully-hydrated (most keys) candidate — same tie-break principle as
        `extract_profile_info`.

        Returns the selected node, or `None` if none found (private/
        restricted group, logged out, or shape drift).
        """
        candidates: list[dict] = []
        seen_ids: set[int] = set()
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if "viewer_join_state" not in blob:
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for node in self._iter_group_nodes(payload):
                if id(node) in seen_ids:
                    continue
                seen_ids.add(id(node))
                candidates.append(node)

        if not candidates:
            logger.warning(
                f"[PARSER] No group node found for handle={handle!r} "
                f"in document ({len(html)} bytes)"
            )
            return None

        if handle:
            needle = handle.strip("/").lower()
            addr_matches = [
                c for c in candidates
                if isinstance(c.get("group_address"), str)
                and c["group_address"].lower() == needle
            ]
            if addr_matches:
                return max(addr_matches, key=len)

        return max(candidates, key=len)

    # __typename values for the group About page's right-rail "cards" —
    # each a distinct GraphQL fragment carrying one aspect of the group
    # (description + info items, activity stats, rules, admin facepile).
    _GROUP_ABOUT_CARD_TYPENAMES = frozenset({
        "GroupsAboutFeedAboutCardUnit",
        "GroupsAboutFeedActivityCardUnit",
        "GroupsAboutFeedRulesCardUnit",
        "GroupsAboutFeedMembersCardUnit",
    })

    def _iter_group_about_cards(self, obj):
        """Yield every dict in `obj` whose `__typename` is one of the group
        About page's card units (see `_GROUP_ABOUT_CARD_TYPENAMES`)."""
        if isinstance(obj, dict):
            if obj.get("__typename") in self._GROUP_ABOUT_CARD_TYPENAMES:
                yield obj
            for v in obj.values():
                yield from self._iter_group_about_cards(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from self._iter_group_about_cards(v)

    def extract_group_about_cards(self, html: str) -> list:
        """Extract the group About page's card units (description, activity
        stats, rules, admin facepile) from its server-rendered HTML.

        Unlike ProfileAbout's sub-tabs, FB renders all of these together on
        one navigation (`/groups/<handle>/about/`) — no per-section
        navigation needed. Returns `[]` if none found.
        """
        cards: list = []
        seen_ids: set = set()
        for blob in self._SJS_SCRIPT_RE.findall(html):
            if not any(t in blob for t in self._GROUP_ABOUT_CARD_TYPENAMES):
                continue
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                continue
            for card in self._iter_group_about_cards(payload):
                if id(card) in seen_ids:
                    continue
                seen_ids.add(id(card))
                cards.append(card)
        return cards

    # ----- public flatten API -----

    def flatten(
        self,
        record: dict | list[dict],
        endpoint: str,
    ) -> dict | list[dict | None] | None:
        """Flatten one record — or a list of records — into row dict(s).

        Args:
            record: Either one element of `ScrapingResult.data`, or the full
                list. For post-stream endpoints (UserTimeline, Search,
                GroupTimeline) each element is a `data.<root>` dict wrapping a
                Story; for single-record endpoints (PageTransparency,
                ProfileAuthenticity) it's the record dict directly.
            endpoint: Endpoint name from the originating Query (e.g. "UserTimeline").

        Returns:
            - `dict | None` when `record` is a dict — the row dict, or None if
              the record can't be resolved.
            - `list[dict | None]` when `record` is a list — one entry per input,
              with None preserved for records that can't be resolved (callers
              that want only successes should filter Nones themselves).

        Raises `ValueError` if `endpoint` is unregistered or `record` is
        neither a dict nor a list. Routes to `ENDPOINT_FLATTENERS[endpoint]`.
        """
        method = self.ENDPOINT_FLATTENERS.get(endpoint)
        if not method:
            raise ValueError(
                f"No flattener registered for endpoint: {endpoint!r}. "
                f"Registered: {list(self.ENDPOINT_FLATTENERS)}"
            )

        if isinstance(record, dict):
            return getattr(self, method)(record)
        elif isinstance(record, list):
            return [
                getattr(self, method)(r) for r in record
            ]
        else:
            raise ValueError(
                f"Unrecognized record type: {type(record)} please pass either a dict or a list[dict]."
            )

    # ----- endpoint orchestrators -----

    def _flatten_pctfrq_post(self, post: dict) -> dict | None:
        """Orchestrator for ProfileCometTimelineFeedRefetchQuery (UserTimeline)."""
        story = _resolve_story(post)
        if not story:
            return None
        out: dict = {}
        out.update(self._extract_ids_and_urls(story))
        out.update(self._extract_times(story))
        out.update(self._extract_audience(story))
        out.update(self._extract_author(story))
        out.update(self._extract_message(story))
        out.update(self._extract_music(story))
        out.update(self._extract_flags(story))
        out.update(self._extract_engagement(story))
        out["top_comments"] = self._extract_top_comments(story)
        out["attachments"]  = self._extract_attachments(story)
        out["shared_post"]  = self._extract_shared_post(story)
        return out

    def _flatten_grouptimeline_post(self, post: dict) -> dict | None:
        """Orchestrator for GroupsCometFeedRegularStoriesPaginationQuery (GroupTimeline).

        Group-feed Stories share the same Comet shape as UserTimeline posts
        (metadata, author, attachments, engagement — all identical), so this
        is a thin alias over `_flatten_pctfrq_post`. Kept as a distinct method
        so future GroupTimeline-only fields (poster's role in the group,
        group_id resolution, etc.) can be added here without polluting the
        UserTimeline flattener.
        """
        return self._flatten_pctfrq_post(post)

    def _flatten_postdetail_record(self, post: dict) -> dict | None:
        """Orchestrator for PostDetail (a single permalink Story).

        `extract_permalink_story` returns the Story wrapped as `{"node": story}`
        — the same shape `parse_timeline_response` emits for a Shape-B feed
        entry — so a permalink post flattens identically to a feed post. Kept
        distinct so PostDetail-only fields can diverge later without touching
        the timeline flatteners.
        """
        return self._flatten_pctfrq_post(post)

    def _flatten_pagetransparency_record(self, record: dict) -> dict | None:
        """Orchestrator for ProfileTransparencyDialogQuery (PageTransparency).

        `record` is the `data.page` dict from the GraphQL response. Returns a
        single-row dict — PageTransparency is a single-record endpoint, not
        a post stream. None on shape mismatch (no `id` field).
        """
        if not isinstance(record, dict) or not record.get("id"):
            return None

        info = record.get("pages_transparency_info") or {}
        admin_locations = info.get("admin_locations") or {}
        history_items = info.get("history_items") or []

        # Page creation date — first item with item_type == "CREATION".
        creation_event_time: int | None = None
        for item in history_items:
            if isinstance(item, dict) and item.get("item_type") == "CREATION":
                creation_event_time = item.get("event_time")
                break

        # Name change history — every NAME_CHANGE item, oldest-first preserved
        # from the response order.
        name_changes = [
            {
                "event_time": item.get("event_time"),
                "target_name": item.get("target_name"),
            }
            for item in history_items
            if isinstance(item, dict) and item.get("item_type") == "NAME_CHANGE"
        ]

        admin_country_counts = [
            {
                "country": _g(c, "country", "iso_name"),
                "country_id": _g(c, "country", "id"),
                "count": c.get("count"),
            }
            for c in (admin_locations.get("admin_country_counts") or [])
            if isinstance(c, dict)
        ]

        delegate = record.get("profile_plus_for_delegate_page") or {}

        return {
            "page_id": record.get("id"),
            "name": record.get("name"),
            "page_type_name_for_content": record.get("page_type_name_for_content"),
            "is_viewer_admin": record.get("is_viewer_admin"),
            "verification_status": record.get("verification_status"),
            "profile_picture_url": _g(record, "profile_picture", "uri"),
            "page_transparency_settings_uri": record.get("page_transparency_settings_uri"),
            "should_show_responsible_for_org_content": record.get(
                "should_show_responsible_for_org_content"
            ),
            "category_text": delegate.get("category_text"),
            "delegate_id": delegate.get("id"),
            # pages_transparency_info fields
            "transparency_id": info.get("id"),
            "transparency_title": info.get("transparency_title"),
            "is_person_profile": info.get("is_person_profile"),
            "is_profile_action_report": info.get("is_profile_action_report"),
            "linked_profile_id": info.get("linked_profile_id"),
            "should_use_page_rename": info.get("should_use_page_rename"),
            "genai_chatbot_disclosure": info.get("genai_chatbot_disclosure"),
            "enabled_features": info.get("enabled_features") or [],
            "has_active_ads": info.get("has_active_ads"),
            "has_run_political_ads": info.get("has_run_political_ads"),
            "page_id_for_admin": info.get("page_id_for_admin"),
            "state_media_country_label": info.get("state_media_country_label"),
            "confirmed_page_owner_consumer": record.get("confirmed_page_owner_consumer"),
            "confirmed_page_partner_names": record.get("confirmed_page_partner_names") or [],
            "creation_event_time": creation_event_time,
            "name_changes": name_changes,
            "admin_country_counts": admin_country_counts,
            "admin_num_opt_out": admin_locations.get("num_opt_out"),
            "admin_num_unknown": admin_locations.get("num_unknown"),
        }

    def _flatten_profile_authenticity_record(self, record: dict) -> dict | None:
        """Orchestrator for ProfileCometDirectoryAuthenticityModalQuery (ProfileAuthenticity).

        `record` is the `data.user` dict from the GraphQL response. Returns
        a single-row dict — ProfileAuthenticity is a single-record endpoint.
        None on shape mismatch (no `id` field).

        Header-field dispatch by `profile_field_type`:
          - PROFILE_JOIN_DATE     → `profile_join_date` (e.g. "May 7, 2013")
          - PROFILE_UPDATED_SINCE → `profile_updated_since` (e.g. "6 hours ago")
          - CATEGORY              → `category` (e.g. "Personal blog")
          - TRANSPARENCY          → `transparency_present` (bool — the entry
                                    exists but `value` is null; it's a link
                                    placeholder in the FB UI)
        """
        if not isinstance(record, dict) or not record.get("id"):
            return None

        modal = record.get("profile_directory_authenticity_modal") or {}
        header_fields = modal.get("header_fields") or []

        by_type: dict[str, dict] = {}
        for hf in header_fields:
            if not isinstance(hf, dict):
                continue
            tn = hf.get("profile_field_type")
            if isinstance(tn, str):
                by_type[tn] = hf

        join = by_type.get("PROFILE_JOIN_DATE") or {}
        updated = by_type.get("PROFILE_UPDATED_SINCE") or {}
        category = by_type.get("CATEGORY") or {}

        meta_verified = modal.get("meta_verified_section") or {}

        about_fields = [
            {
                "label": f.get("label"),
                "profile_field_type": f.get("profile_field_type"),
                "value": f.get("value"),
                "subtitle": f.get("subtitle"),
            }
            for f in (modal.get("about_fields") or [])
            if isinstance(f, dict)
        ]

        header_fields_clean = [
            {
                "label": hf.get("label"),
                "profile_field_type": hf.get("profile_field_type"),
                "value": hf.get("value"),
                "subtitle": hf.get("subtitle"),
            }
            for hf in header_fields
            if isinstance(hf, dict)
        ]

        return {
            "user_id": record.get("id"),
            "name": record.get("name"),
            "delegate_page_id": record.get("delegate_page_id"),
            "profile_join_date": join.get("value"),
            "profile_updated_since": updated.get("value"),
            "category": category.get("value"),
            "transparency_present": "TRANSPARENCY" in by_type,
            "is_meta_verified": bool(meta_verified.get("show_section")),
            "meta_verified_headline": meta_verified.get("headline"),
            "meta_verified_body": meta_verified.get("body"),
            "header_description": modal.get("header_description"),
            "about_title": modal.get("about_title"),
            "about_fields": about_fields,
            "header_fields": header_fields_clean,
            "section_token": modal.get("section_token"),
            "collection_token": modal.get("collection_token"),
        }

    def _flatten_profile_info_record(self, record: dict) -> dict | None:
        """Orchestrator for the server-rendered profile header block (ProfileInfo).

        `record` is the profile node returned by `extract_profile_info` — FB's
        Comet profile-header shape, shared by User and Page surfaces. Returns
        a single-row dict; None on shape mismatch (no `id` field).

        Follower/following counts (`profile_social_context`) only ship as
        FB-formatted abbreviated strings (e.g. "121M followers") — FB
        doesn't expose an exact integer on this surface, so `follower_count`
        / `following_count` are parsed via `_parse_abbreviated_count` into
        an approximate integer (e.g. "121M" -> 121_000_000) for sorting/
        comparison; treat as order-of-magnitude, not exact.

        Intro-card fields (`profile_intro_card.context_items`) are dispatched
        by `profile_field_type`, mirroring
        `_flatten_profile_authenticity_record`'s `header_fields` dispatch —
        `category` (e.g. "Public figure") is the one consistently present
        across profiles; everything else observed there is also preserved
        generically in `intro_card_fields` since coverage varies by account
        (work / education / location / relationship status, ...).

        `profile_social_context.content` is a list — one entry for followers,
        one for following when FB renders both (personal profiles only show
        followers). Entries are matched by their `uri` (`.../followers...` /
        `.../following...`) rather than position, since the URI format itself
        varies (path-style `/<handle>/followers/` vs query-style
        `?...&sk=followers`) and ordering isn't guaranteed either way.
        """
        if not isinstance(record, dict) or not record.get("id"):
            return None

        social_context = record.get("profile_social_context") or {}
        context_content = social_context.get("content") or []

        follower_text = follower_uri = following_text = None
        for item in context_content:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or ""
            text = _g(item, "text", "text")
            if "following" in uri:
                following_text = text
            elif "followers" in uri:
                follower_text, follower_uri = text, uri

        bio = _g(
            record, "header_top_row", "profile_user", "profile_status",
            "profile_status_text", "text",
        )

        intro_card = _g(
            record, "header_top_row", "profile_user", "profile_intro_card",
            default={},
        ) or {}
        edges = _g(intro_card, "context_items", "edges", default=[]) or []

        by_type: dict[str, dict] = {}
        intro_card_fields = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            field_type = node.get("profile_field_type")
            short_title = _g(node, "short_title", "text")
            if isinstance(field_type, str):
                by_type[field_type] = node
            intro_card_fields.append({
                "profile_field_type": field_type,
                "text": short_title,
            })

        category = by_type.get("category") or {}

        return {
            "profile_id": record.get("id"),
            "name": record.get("name"),
            "url": record.get("url"),
            "gender": record.get("gender"),
            "username_for_profile": record.get("username_for_profile"),
            "is_verified": bool(record.get("show_verified_badge_on_profile")),
            "is_viewer_friend": record.get("is_viewer_friend"),
            "is_memorialized": bool(record.get("is_visibly_memorialized")),
            "follower_count": _parse_abbreviated_count(follower_text),
            "followers_url": follower_uri,
            "following_count": _parse_abbreviated_count(following_text),
            "bio": bio,
            "category": _g(category, "short_title", "text"),
            "intro_card_fields": intro_card_fields,
            "cover_photo_url": _g(record, "cover_photo", "photo", "image", "uri"),
            "profile_picture_url": _g(record, "profilePicLarge", "uri"),
        }

    def _flatten_profile_about_record(self, record: dict) -> dict | None:
        """Orchestrator for the profile About page (ProfileAbout).

        `record` is `{"profile": <profile_header_node>, "sections":
        [<profile_field_section>, ...]}` assembled by `profile_about_hybrid`
        from one About-landing navigation (header + sub-tab directory) plus
        one navigation per requested section (that section's populated
        fields). Reuses `_flatten_profile_info_record` for the header
        fields (name, follower count, bio, category, ...) — the About
        landing page already renders the header for free, so a
        ProfileAbout row is a superset of what ProfileInfo returns rather
        than requiring a separate call.

        About fields are dispatched into named convenience keys for the
        highest-value, most consistently-present field types observed
        (phone, email, messenger, address, hours, rating, website), with
        every field — dispatched or not — also preserved in the generic
        `about_fields` list, since section coverage varies enormously by
        account (Pages typically expose contact/basic-info/links; personal
        profiles more often expose work/education/personal-details instead).
        """
        if not isinstance(record, dict):
            return None
        profile = record.get("profile")
        flat = self._flatten_profile_info_record(profile) if isinstance(profile, dict) else None
        if flat is None:
            return None

        by_field_type: dict = {}
        about_fields = []
        for section in record.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_type = section.get("field_section_type")
            for f in (_g(section, "profile_fields", "nodes", default=[]) or []):
                if not isinstance(f, dict):
                    continue
                field_type = f.get("field_type")
                text = _g(f, "title", "text")
                link = f.get("link_url")
                if isinstance(field_type, str):
                    by_field_type[field_type] = {"text": text, "link_url": link}
                about_fields.append({
                    "field_section_type": section_type,
                    "field_type": field_type,
                    "text": text,
                    "link_url": link,
                })

        def _val(field_type):
            return (by_field_type.get(field_type) or {}).get("text")

        def _link(field_type):
            return (by_field_type.get(field_type) or {}).get("link_url")

        flat.update({
            "phone": _val("profile_phone"),
            "email": _val("profile_email"),
            "messenger_url": _link("business_messenger"),
            "address": _val("address"),
            "address_map_url": _link("address"),
            "hours": _val("business_hours"),
            "rating_text": _val("ratings"),
            "website": _val("website"),
            "website_url": _link("website"),
            "about_fields": about_fields,
        })
        return flat

    def _flatten_group_info_record(self, record: dict) -> dict | None:
        """Orchestrator for the server-rendered group header block (GroupInfo).

        `record` is the group node returned by `extract_group_info`. Returns
        a single-row dict; None on shape mismatch (no `id` field).

        Member count only ships as FB's abbreviated display string (e.g.
        "120.4K members") on this surface — parsed via
        `_parse_abbreviated_count` into an approximate integer, same
        order-of-magnitude caveat as ProfileInfo's follower_count.

        `content_views` is the group's tab directory (About/Discussion/
        Featured/People/Events/Media/...) as `{content_view_type: uri}` —
        FB hands these back as absolute URIs directly, unlike ProfileAbout's
        sub-tab directory which needed format-sniffing.

        `privacy_label` is a short display string (e.g. "Public group") —
        the header's `privacy_info` only carries a `title`, not a split
        label/description. `GroupAbout`'s About page carries a richer
        `XFBPrivacyGroupsAboutInfoItem` (separate "Public" label +
        "Anyone can see..." description) that `_flatten_group_about_record`
        promotes over this one when available.
        """
        if not isinstance(record, dict) or not record.get("id"):
            return None

        content_views = {}
        for edge in _g(record, "group_content_views", "edges", default=[]) or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict) and node.get("content_view_type"):
                content_views[node["content_view_type"]] = node.get("content_view_uri")

        return {
            "group_id": record.get("id"),
            "name": record.get("name"),
            "url": record.get("url"),
            "handle": record.get("group_address"),
            "privacy_label": _g(record, "privacy_info", "title", "text"),
            "privacy_description": None,
            "member_count": _parse_abbreviated_count(
                _g(record, "group_member_profiles", "formatted_count_text")
            ),
            "viewer_join_state": record.get("viewer_join_state"),
            "cover_photo_url": _g(
                record, "cover_renderer", "cover_photo_content", "photo", "image", "uri"
            ),
            "content_views": content_views,
        }

    @staticmethod
    def _group_admin_profile(node: dict) -> dict:
        return {
            "id": node.get("id"),
            "name": node.get("name"),
            "url": node.get("url"),
            "profile_picture_url": _g(node, "profile_picture", "uri"),
        }

    def _flatten_group_about_record(self, record: dict) -> dict | None:
        """Orchestrator for the group About page (GroupAbout).

        `record` is `{"group": <group_header_node>, "cards":
        [<about_card_unit>, ...]}` assembled by `group_about_hybrid` from a
        single navigation to `/groups/<handle>/about/` — unlike
        ProfileAbout, FB renders every About card together on that one
        page, so no per-section navigation is needed. Reuses
        `_flatten_group_info_record` for the header fields, same
        composition principle as `_flatten_profile_about_record`.

        Cards are dispatched by `__typename`:
          - `GroupsAboutFeedAboutCardUnit` → `description` +
            `about_info_items` (raw list — item shapes vary too much by
            type for a uniform field_type dispatch like ProfileAbout's;
            recognized types are also promoted to named keys:
            `privacy_label`/`privacy_description` (overriding the header's
            coarser `privacy_info.title` with this item's richer split
            label + description), `discoverability_label/description`,
            `history_summary`, `created_time`, `locations`).
          - `GroupsAboutFeedActivityCardUnit` → `posts_last_day`,
            `posts_last_month`, `total_members_text`, `new_members_text`.
          - `GroupsAboutFeedRulesCardUnit` → `admin_and_moderator_count`
            (exact — matches FB's "Admins & moderators" tab count) +
            `rules`.
          - `GroupsAboutFeedMembersCardUnit` → `friend_member_count` +
            `admin_profiles`.

        `admin_profiles` is best-effort: it comes from a UI "facepile" FB
        may truncate for groups with many admins/moderators, so it can be
        shorter than `admin_and_moderator_count` — that count is the
        reliable one; the roster may not be exhaustive.
        """
        if not isinstance(record, dict):
            return None
        group = record.get("group")
        flat = self._flatten_group_info_record(group) if isinstance(group, dict) else None
        if flat is None:
            return None

        about_info_items: list = []
        discoverability_label = discoverability_description = None
        history_summary = created_time = None
        locations: list = []
        posts_last_day = posts_last_month = None
        total_members_text = new_members_text = None
        admin_and_moderator_count = None
        rules: list = []
        friend_member_count = None
        admin_profiles: list = []

        for card in record.get("cards") or []:
            if not isinstance(card, dict):
                continue
            card_type = card.get("__typename")
            card_group = card.get("group") or {}

            if card_type == "GroupsAboutFeedAboutCardUnit":
                flat["description"] = _g(card_group, "description_with_entities", "text")
                for item in card_group.get("about_info_items") or []:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("__typename")
                    item_group = item.get("group") or {}
                    about_info_items.append({"type": item_type, "group": item_group})
                    if item_type == "XFBPrivacyGroupsAboutInfoItem":
                        # Richer than the header's `privacy_info.title` —
                        # a split label ("Public") + description
                        # ("Anyone can see..."). Promote over the header
                        # value, which only carries a single "Public group"
                        # string.
                        flat["privacy_label"] = _g(
                            item_group, "privacy_info", "label", "text"
                        ) or flat.get("privacy_label")
                        flat["privacy_description"] = _g(
                            item_group, "privacy_info", "description", "text"
                        )
                    elif item_type == "XFBDiscoverabilityGroupsAboutInfoItem":
                        discoverability_label = _g(
                            item_group, "discoverability_info", "label", "text"
                        )
                        discoverability_description = _g(
                            item_group, "discoverability_info", "description", "text"
                        )
                    elif item_type == "XFBHistoryGroupsAboutInfoItem":
                        created_time = _g(item_group, "group_history", "create_time")
                        history_summary = _g(
                            item_group, "group_history", "group_history_summary", "text"
                        )
                    elif item_type == "XFBLocationGroupsAboutInfoItem":
                        locations = [
                            loc.get("name")
                            for loc in (item_group.get("group_locations") or [])
                            if isinstance(loc, dict) and loc.get("name")
                        ]

            elif card_type == "GroupsAboutFeedActivityCardUnit":
                posts_last_day = card_group.get("number_of_posts_in_last_day")
                posts_last_month = card_group.get("number_of_posts_in_last_month")
                total_members_text = card_group.get("group_total_members_info_text")
                new_members_text = card_group.get("group_new_members_info_text")

            elif card_type == "GroupsAboutFeedRulesCardUnit":
                admin_and_moderator_count = _g(card_group, "group_admin_profiles", "count")
                rules = [
                    {
                        "id": r.get("id"),
                        "title": r.get("rule_title"),
                        "description": r.get("description"),
                    }
                    for r in _g(card_group, "group_rules", "nodes", default=[]) or []
                    if isinstance(r, dict)
                ]

            elif card_type == "GroupsAboutFeedMembersCardUnit":
                friend_member_count = _g(card_group, "group_friend_members", "count")
                admin_profiles = [
                    self._group_admin_profile(node)
                    for edge in _g(card_group, "facepile_admin_profiles", "edges", default=[]) or []
                    for node in [edge.get("node") if isinstance(edge, dict) else None]
                    if isinstance(node, dict)
                ]

        flat.setdefault("description", None)
        flat.update({
            "discoverability_label": discoverability_label,
            "discoverability_description": discoverability_description,
            "created_time": created_time,
            "history_summary": history_summary,
            "locations": locations,
            "about_info_items": about_info_items,
            "posts_last_day": posts_last_day,
            "posts_last_month": posts_last_month,
            "total_members_text": total_members_text,
            "new_members_text": new_members_text,
            "admin_and_moderator_count": admin_and_moderator_count,
            "rules": rules,
            "friend_member_count": friend_member_count,
            "admin_profiles": admin_profiles,
        })
        return flat

    def _flatten_commentslist_comment(self, record: dict) -> dict | None:
        """Orchestrator for CommentsListComponentsPaginationQuery (CommentsList).

        `record` is one entry from `parse_comments_response`'s `comments`
        list: `{"node": <Comment>, "_parent_feedback_id_b64": <str>}`. The
        Comment shape is FB's Comet "Comment" node (distinct from Story —
        different `feedback.id` namespace, no `metadata[]` strategy list, no
        `comet_sections` wrapping). Returns one row dict per comment.
        """
        if not isinstance(record, dict):
            return None
        comment = record.get("node") if "node" in record else record
        if not isinstance(comment, dict) or not comment.get("id"):
            return None
        parent_feedback_id_b64 = (
            record.get("_parent_feedback_id_b64")
            if isinstance(record, dict) else None
        )
        out: dict = {}
        out.update(self._extract_comment_ids(comment, parent_feedback_id_b64))
        out.update(self._extract_comment_times(comment))
        out.update(self._extract_comment_author(comment))
        out.update(self._extract_comment_body(comment))
        out.update(self._extract_comment_reactions(comment))
        out.update(self._extract_comment_replies(comment))
        out["attachments"] = self._extract_attachments(comment)
        return out

    # ----- per-aspect extractors -----

    def _metadata_by_typenames(
        self, story: dict, typenames: tuple[str, ...]
    ) -> dict | None:
        """First entry in `metadata[]` whose `__typename` matches any candidate
        in `typenames`, checked in order — earlier candidates win. Returns None
        if no match.

        FB's metadata[] ordering is non-deterministic across posts and the
        strategy typename varies by rendering context (e.g. `Longer` vs
        `Minimized` for the timestamp; same inner shape, different label).
        Passing a tuple lets us absorb sibling renames without code changes.
        """
        md = _g(story, "comet_sections", "context_layout", "story",
                "comet_sections", "metadata", default=[]) or []
        for tn in typenames:
            for m in md:
                if isinstance(m, dict) and m.get("__typename") == tn:
                    return m
        return None

    def _extract_ids_and_urls(self, story: dict) -> dict:
        ts_meta = self._metadata_by_typenames(story, _METADATA_TIMESTAMP_TYPENAMES) or {}
        canonical_url = _g(ts_meta, "story", "url")
        permalink_url = story.get("permalink_url")
        return {
            "post_id":       story.get("post_id"),
            "story_id":      story.get("id"),
            "url":           canonical_url or permalink_url,
            "permalink_url": permalink_url,
        }

    def _extract_times(self, story: dict) -> dict:
        ts_meta = self._metadata_by_typenames(story, _METADATA_TIMESTAMP_TYPENAMES) or {}
        created_at = _g(ts_meta, "story", "creation_time")
        created_at_utc = (
            datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if isinstance(created_at, (int, float)) else None
        )
        return {"created_at": created_at, "created_at_utc": created_at_utc}

    def _extract_audience(self, story: dict) -> dict:
        aud = self._metadata_by_typenames(story, _METADATA_AUDIENCE_TYPENAMES) or {}
        return {"privacy": _g(aud, "story", "privacy_scope", "description")}

    def _extract_author(self, story: dict) -> dict:
        op = _g(story, "feedback", "owning_profile") or {}
        a0 = _g(story, "actors", 0) or {}
        return {
            "author_id":            op.get("id") or a0.get("id"),
            "author_name":          op.get("name") or a0.get("name"),
            "author_url":           a0.get("url"),
            "author_type":          a0.get("__typename"),
            "author_promode_badge": a0.get("show_promode_badge"),
        }

    def _extract_message(self, story: dict) -> dict:
        """Text + entity-annotated ranges (hashtags, mentions, external URLs).

        FB stores all three as typed entities in the same `message.ranges[]`,
        each carrying `entity.__typename` ∈ {Hashtag, User, ExternalUrl}.
        """
        cs = _g(story, "comet_sections", "content", "story") or {}
        text = (
            _g(cs, "comet_sections", "message", "story", "message", "text")
            or _g(cs, "comet_sections", "message_container", "story", "message", "text")
            or _g(cs, "message", "text")
        )
        ranges = []
        for path in (
            ("comet_sections", "message", "story", "message", "ranges"),
            ("comet_sections", "message_container", "story", "message", "ranges"),
            ("message", "ranges"),
        ):
            ranges = _g(cs, *path, default=[]) or []
            if ranges:
                break

        hashtags: list[str] = []
        mentions: list[dict] = []
        external_urls: list[str] = []
        seen_h: set[str] = set()
        seen_m: set[str] = set()
        seen_u: set[str] = set()
        for r in ranges:
            entity = r.get("entity") or {}
            tn = entity.get("__typename")
            if tn == "Hashtag":
                # Hashtag has no name field — derive from URL last segment.
                url = entity.get("url") or ""
                name = url.rsplit("/", 1)[-1] if url else None
                if name and name not in seen_h:
                    seen_h.add(name)
                    hashtags.append(name)
            elif tn == "User":
                uid = entity.get("id")
                if uid and uid not in seen_m:
                    seen_m.add(uid)
                    mentions.append({
                        "id":   uid,
                        "name": entity.get("name"),
                        "url":  entity.get("url"),
                    })
            elif tn == "ExternalUrl":
                url = entity.get("external_url")
                if url and url not in seen_u:
                    seen_u.add(url)
                    external_urls.append(url)

        return {
            "text":          text,
            "hashtags":      hashtags or None,
            "mentions":      mentions or None,
            "external_urls": external_urls or None,
        }

    def _extract_music(self, story: dict) -> dict:
        m = self._metadata_by_typenames(story, _METADATA_MUSIC_TYPENAMES) or {}
        meta = _g(m, "story", "story_media_metadata") or {}
        return {
            "music_artist": meta.get("artist_name"),
            "music_title":  meta.get("song_title"),
        }

    def _extract_flags(self, story: dict) -> dict:
        permalink_url = story.get("permalink_url") or ""
        is_reel = "/reel/" in permalink_url
        is_live = _g(self._summary_feedback(story),
                     "video_view_count_renderer", "feedback",
                     "associated_video", "is_live_streaming")
        return {
            "is_reel":   is_reel,
            "is_live":   is_live,
            "is_repost": bool(story.get("attached_story")),
        }

    def _summary_feedback(self, story: dict) -> dict:
        """Engagement-bearing feedback (reactions, shares, comments_count, video_views)."""
        return (_g(story, "comet_sections", "feedback", "story",
                   "story_ufi_container", "story", "feedback_context",
                   "feedback_target_with_context",
                   "comet_ufi_summary_and_actions_renderer", "feedback") or {})

    def _action_renderer_feedback(self, sf: dict, typenames: tuple[str, ...]) -> dict:
        """Per-renderer `feedback` subdict from `adaptive_ufi_action_renderers[]`,
        dispatched by `__typename`. First match wins (sibling renames tolerated
        via the candidate tuple, same pattern as `_metadata_by_typenames`).

        Variant B of the summary feedback shape (~60% of UserTimeline responses)
        omits the top-level totals and only ships them through these renderers.
        Returns `{}` if no renderer matches — caller decides what that means.
        """
        for tn in typenames:
            for r in sf.get("adaptive_ufi_action_renderers") or []:
                if isinstance(r, dict) and r.get("__typename") == tn:
                    return r.get("feedback") or {}
        return {}

    def _extract_engagement(self, story: dict) -> dict:
        sf = self._summary_feedback(story)

        # Per-reaction breakdown — both variants ship `top_reactions.edges[]`
        # at the top of `sf`, so this path works regardless of variant.
        rxn_counts = {k: 0 for k in ("like", "love", "haha", "wow", "sad", "angry", "care")}
        for e in _g(sf, "top_reactions", "edges", default=[]) or []:
            name = _g(e, "node", "localized_name")
            if name:
                key = name.lower()
                if key in rxn_counts:
                    rxn_counts[key] = e.get("reaction_count") or 0

        # Totals: try top-level (Variant A), fall back to the matching adaptive
        # renderer's feedback (Variant B). Last-resort for reactions: sum the
        # per-type edges — FB has exactly 7 reaction types so the sum is exhaustive.
        react_fb   = sf if "reaction_count"                  in sf else self._action_renderer_feedback(sf, _REACTION_RENDERER_TYPENAMES)
        share_fb   = sf if "share_count"                     in sf else self._action_renderer_feedback(sf, _SHARE_RENDERER_TYPENAMES)
        # Comments: in Variant A the total lives under either
        # `comments_count_summary_renderer.feedback.comment_rendering_instance`
        # or directly at `comment_rendering_instance` (FB ships both). In Variant
        # B only the renderer-nested version exists. We try the most specific
        # path first, then both shallower fallbacks.
        comment_fb = sf if ("comments_count_summary_renderer" in sf or "comment_rendering_instance" in sf) \
                        else self._action_renderer_feedback(sf, _COMMENT_RENDERER_TYPENAMES)

        total_reactions = _g(react_fb, "reaction_count", "count")
        if total_reactions is None and any(rxn_counts.values()):
            total_reactions = sum(rxn_counts.values())

        total_comments = (
            _g(comment_fb, "comments_count_summary_renderer", "feedback",
               "comment_rendering_instance", "comments", "total_count")
            or _g(comment_fb, "comment_rendering_instance", "comments", "total_count")
        )

        # Video duration: first attachment carrying a non-zero length wins.
        duration = None
        for a in story.get("attachments") or []:
            d = _g(a, "styles", "attachment", "media", "length_in_second")
            if d:
                duration = d
                break

        return {
            "reactions":          total_reactions,
            **rxn_counts,
            "shares":             _g(share_fb, "share_count", "count"),
            "comments":           total_comments,
            "video_views":        _g(sf, "video_view_count"),
            "video_duration_sec": duration,
        }

    def _extract_top_comments(self, story: dict) -> list[dict] | None:
        """Top-level comments FB surfaces with the post (interesting_top_level_comments)."""
        fbc = _g(story, "comet_sections", "feedback", "story",
                 "story_ufi_container", "story", "feedback_context") or {}
        out = []
        for tc in fbc.get("interesting_top_level_comments") or []:
            c = tc.get("comment") or {}
            c_fb = c.get("feedback") or {}
            r = _g(c_fb, "reaction_count", "count")
            if r is None:
                # Some shapes only carry per-reaction edges; sum as fallback.
                summed = sum((e.get("reaction_count") or 0)
                             for e in _g(c_fb, "top_reactions", "edges", default=[]) or [])
                r = summed or None
            out.append({
                "text":        _g(c, "body", "text"),
                "author_id":   _g(c, "author", "id"),
                "author_name": _g(c, "author", "name"),
                "author_url":  _g(c, "author", "url"),
                "created_at":  c.get("created_time"),
                "reactions":   r,
            })
        return out or None

    def _extract_attachments(self, story: dict) -> list[dict] | None:
        out = [self._extract_attachment(a) for a in (story.get("attachments") or [])]
        return out or None

    # Uniform attachment shape — every type fills the same keys, type-specific
    # extras get None when not applicable. Keeps polars schema stable.
    _ATTACHMENT_KEYS = (
        "type", "id", "url",
        "image_url", "image_lowres_url", "thumbnail_url",
        "width", "height", "accessibility_caption",
        "video_url", "video_duration_sec", "video_is_live", "video_permalink_url", "video_captions_url",
        "link_title", "link_description", "link_source", "link_destination_url",
        "subattachments",
    )

    def _empty_attachment(self) -> dict:
        return {k: None for k in self._ATTACHMENT_KEYS}

    def _extract_attachment(self, att: dict) -> dict:
        """Extract one attachment, recursing into album subattachments and
        reel-share inner attachments.
        """
        styles = att.get("styles") or {}
        sa = styles.get("attachment") or {}
        style_typename = styles.get("__typename")
        atype = _ATTACHMENT_TYPE_BY_STYLE.get(style_typename, "unknown")

        # Bare-media fallback: album sub-nodes & reel inner attachments don't
        # carry the outer styles wrapper.
        media = sa.get("media") or att.get("media") or {}

        out = self._empty_attachment()
        out.update({
            "type":                  atype,
            "id":                    media.get("id") or att.get("id"),
            "url":                   sa.get("url") or att.get("url")
                                     or media.get("permalink_url") or media.get("url"),
            "width":                 media.get("width"),
            "height":                media.get("height"),
            "accessibility_caption": media.get("accessibility_caption"),
            "video_duration_sec":    media.get("length_in_second"),
            "video_is_live":         media.get("is_live_streaming"),
            "video_permalink_url":   media.get("permalink_url"),
            "video_captions_url":    media.get("captions_url"),
            "link_title":            _g(sa, "title_with_entities", "text"),
            "link_description":      _g(sa, "description", "text"),
            "link_source":           _g(sa, "source", "text"),
            "link_destination_url":  _g(sa, "story_attachment_link_renderer",
                                        "attachment", "web_link", "url"),
        })

        # Photos / album cover: single photos serve the URL on photo_image
        # (viewer_image carries dimensions only); album subnodes invert this
        # — viewer_image has the URL, photo_image isn't there. Try both.
        if atype in ("photo", "album"):
            out["image_url"] = (_g(media, "photo_image", "uri")
                                or _g(media, "viewer_image", "uri"))
            out["image_lowres_url"] = _g(media, "image", "uri")
            # width/height may live on viewer_image / photo_image rather than top-level
            if not out["width"]:
                out["width"]  = _g(media, "viewer_image", "width") or _g(media, "photo_image", "width")
                out["height"] = _g(media, "viewer_image", "height") or _g(media, "photo_image", "height")

        # Videos & animated GIFs: thumbnail is a string URL on first_frame_thumbnail
        # when present, else nested {image: {uri}} on preferred_thumbnail.
        if atype == "video":
            ff = media.get("first_frame_thumbnail")
            out["thumbnail_url"] = (ff if isinstance(ff, str)
                                    else _g(media, "preferred_thumbnail", "image", "uri"))
            out["video_url"] = _pick_progressive_video(media)

        # Link previews: thumbnail lives on media.large_share_image (full) or
        # media.image (favicon-sized). Destination URL falls back to the
        # l.facebook.com redirect when the resolved web_link.url is missing.
        if atype == "link":
            out["image_url"]     = _g(media, "large_share_image", "uri") or _g(media, "image", "uri")
            out["thumbnail_url"] = out["image_url"]
            out["link_destination_url"] = (
                out["link_destination_url"]
                or _g(sa, "story_attachment_link_renderer", "attachment", "url")
            )

        # Albums: recurse over all_subattachments[].nodes (stripped media-only shape).
        subs = _g(sa, "all_subattachments", "nodes", default=[]) or []
        if subs:
            out["subattachments"] = [self._extract_album_subnode(s) for s in subs]

        # Reel shares: inner attachments live under style_infos[].fb_shorts_story.attachments[].
        if atype == "reel_share":
            for si in (sa.get("style_infos") or []):
                inner_atts = _g(si, "fb_shorts_story", "attachments", default=[]) or []
                if inner_atts:
                    out["subattachments"] = [self._extract_attachment(i) for i in inner_atts]
                    # Hoist the first inner reel's permalink/thumbnail/duration/video_url to the
                    # outer attachment so a single-row consumer doesn't have to recurse to find them.
                    inner_media = inner_atts[0].get("media") or {}
                    out["url"]                = out["url"] or inner_media.get("permalink_url")
                    out["thumbnail_url"]      = out["thumbnail_url"] or _g(inner_media, "thumbnailImage", "uri")
                    out["video_duration_sec"] = out["video_duration_sec"] or inner_media.get("length_in_second")
                    out["video_url"]          = out["video_url"] or _pick_progressive_video(inner_media)
                    break

        return out

    def _extract_album_subnode(self, sub: dict) -> dict:
        """Album subattachments use a stripped shape (no outer styles wrapper)."""
        media = sub.get("media") or {}
        typename = media.get("__typename")
        out = self._empty_attachment()
        out.update({
            "type":                  ("photo" if typename == "Photo"
                                      else "video" if typename == "Video"
                                      else "unknown"),
            "id":                    media.get("id"),
            "url":                   sub.get("url"),
            "image_url":             _g(media, "viewer_image", "uri"),
            "image_lowres_url":      _g(media, "image", "uri"),
            "video_url":             _pick_progressive_video(media),
            "width":                 _g(media, "viewer_image", "width") or _g(media, "image", "width"),
            "height":                _g(media, "viewer_image", "height") or _g(media, "image", "height"),
            "accessibility_caption": media.get("accessibility_caption"),
        })
        return out

    # ----- comment-specific extractors (CommentsList) -----

    @staticmethod
    def _decode_b64_legacy(b64_id: str | None) -> str | None:
        """Decode `<prefix>:<numeric_id>` from a base64 GraphQL id; tolerate junk."""
        if not b64_id or not isinstance(b64_id, str):
            return None
        try:
            decoded = base64.b64decode(b64_id).decode("utf-8", errors="replace")
        except Exception:
            return None
        if ":" not in decoded:
            return None
        return decoded.split(":", 1)[1] or None

    def _extract_comment_ids(
        self, comment: dict, parent_feedback_id_b64: str | None
    ) -> dict:
        """Comment id, feedback id, parent ids — both numeric + b64 forms.

        Comment.id is `comment:<post_id>_<comment_id>` (base64); legacy_fbid
        is the numeric comment id directly. The parent post's feedback id is
        captured at the response top-level (`data.node.id`) and threaded in
        as `parent_feedback_id_b64`; we decode it to recover the numeric
        post-feedback id.
        """
        comment_id_b64 = comment.get("id")
        comment_feedback = comment.get("feedback") or {}
        # `comment.feedback.id` decodes to `feedback:<post_id>_<comment_id>`;
        # the numeric comment_id is `legacy_fbid` (preferred — no decoding).
        comment_id = comment.get("legacy_fbid") or self._decode_b64_legacy(comment_id_b64)
        # The parent's feedback (the post's feedback) id — we already have
        # both b64 and numeric forms from the response top-level.
        post_feedback_id = self._decode_b64_legacy(parent_feedback_id_b64)
        # Reply parent (only populated when depth > 0; null in v1 captures).
        parent = comment.get("comment_direct_parent") or {}
        parent_comment_id_b64 = parent.get("id") if isinstance(parent, dict) else None
        return {
            "comment_id": comment_id,
            "comment_id_b64": comment_id_b64,
            "comment_feedback_id_b64": comment_feedback.get("id"),
            "post_feedback_id": post_feedback_id,
            "post_feedback_id_b64": parent_feedback_id_b64,
            "depth": comment.get("depth"),
            "parent_comment_id_b64": parent_comment_id_b64,
            "url": _g(comment_feedback, "url"),
        }

    def _extract_comment_times(self, comment: dict) -> dict:
        created_at = comment.get("created_time")
        created_at_utc = (
            datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if isinstance(created_at, (int, float)) else None
        )
        return {"created_at": created_at, "created_at_utc": created_at_utc}

    def _extract_comment_author(self, comment: dict) -> dict:
        a = comment.get("author") or {}
        return {
            "author_id":   a.get("id"),
            "author_name": a.get("name"),
            "author_url":  a.get("url"),
            "author_type": a.get("__typename"),
        }

    def _extract_comment_body(self, comment: dict) -> dict:
        """Comment text + typed entities (hashtags, mentions, external URLs).

        Comments carry text/ranges at `body.{text,ranges}`. Translation
        variants live under `preferred_body.text` with `translation_type`
        (e.g. "ORIGINAL"). The ranges schema matches Story posts —
        `entity.__typename ∈ {Hashtag, User, ExternalUrl}` — so the entity-
        extraction logic mirrors `_extract_message`.
        """
        body = comment.get("body") or {}
        text = body.get("text")
        ranges = body.get("ranges") or []

        hashtags: list[str] = []
        mentions: list[dict] = []
        external_urls: list[str] = []
        seen_h: set[str] = set()
        seen_m: set[str] = set()
        seen_u: set[str] = set()
        for r in ranges:
            entity = r.get("entity") or {}
            tn = entity.get("__typename")
            if tn == "Hashtag":
                url = entity.get("url") or ""
                name = url.rsplit("/", 1)[-1] if url else None
                if name and name not in seen_h:
                    seen_h.add(name)
                    hashtags.append(name)
            elif tn == "User":
                uid = entity.get("id")
                if uid and uid not in seen_m:
                    seen_m.add(uid)
                    mentions.append({
                        "id":   uid,
                        "name": entity.get("name"),
                        "url":  entity.get("url"),
                    })
            elif tn == "ExternalUrl":
                url = entity.get("external_url")
                if url and url not in seen_u:
                    seen_u.add(url)
                    external_urls.append(url)

        pref = comment.get("preferred_body") or {}
        pref_text = pref.get("text")
        translation_type = pref.get("translation_type")
        return {
            "text":             text,
            "hashtags":         hashtags or None,
            "mentions":         mentions or None,
            "external_urls":    external_urls or None,
            # Surface the translation only when it actually differs from the
            # original (FB ships `preferred_body.text == body.text` when
            # `translation_type == "ORIGINAL"`).
            "translated_text":  pref_text if (pref_text and pref_text != text) else None,
            "translation_type": translation_type,
            "is_disabled":      comment.get("is_disabled"),
        }

    def _extract_comment_reactions(self, comment: dict) -> dict:
        """Reactions breakdown by canonical name + total.

        On CommentsListComponentsPaginationQuery, `feedback.top_reactions.edges`
        carries `{node:{id}, reaction_count}` only — no `localized_name`. We
        map known reaction ids to names via `_REACTION_ID_TO_NAME`; unknown
        ids land in `reactions_other` keyed by id so nothing is silently
        dropped if FB introduces a new reaction.
        """
        fb = comment.get("feedback") or {}
        rxn_counts = {k: 0 for k in ("like", "love", "haha", "wow", "sad", "angry", "care")}
        rxn_other: dict[str, int] = {}
        for e in _g(fb, "top_reactions", "edges", default=[]) or []:
            rid = _g(e, "node", "id")
            count = e.get("reaction_count") or 0
            name = self._REACTION_ID_TO_NAME.get(rid) if isinstance(rid, str) else None
            if name:
                rxn_counts[name] = count
            elif rid:
                rxn_other[rid] = count
        # Comment.feedback.reaction_count.count is often null on this
        # endpoint; sum as the canonical total.
        total = _g(fb, "reaction_count", "count")
        if total is None:
            total = sum(rxn_counts.values()) + sum(rxn_other.values()) or None
        return {
            "reactions":       total,
            **rxn_counts,
            "reactions_other": rxn_other or None,
        }

    def _extract_comment_replies(self, comment: dict) -> dict:
        """Reply counts (the inline reply edges are usually empty on this endpoint).

        `replies_fields.total_count` tells you how many replies the comment
        has — useful for deciding whether to drill into a follow-up
        endpoint that fetches them. `count` is the same number minus
        low-quality / hidden replies; both surface.
        """
        rf = _g(comment, "feedback", "replies_fields") or {}
        return {
            "replies_total_count": rf.get("total_count"),
            "replies_count":       rf.get("count"),
        }

    def _extract_shared_post(self, story: dict) -> dict | None:
        """Run extractors on `attached_story` (FB's repost/share slot).

        FB serves an abbreviated Story under attached_story — only
        comet_sections.context_layout (metadata) and footer plus
        feedback.owning_profile and permalink_url. The full content section
        (text, attachments, top_reactions) is NOT inlined; FB expects the UI
        to fetch the original post on click. So most extractors return None
        for shared posts; that's fine — the row still carries the shared-
        post id, url, creation time, privacy, and author.

        attached_story IS already a Story-shaped dict; pass it straight to the
        extractors (no _resolve_story call — that requires a top-level post_id
        which attached_story doesn't carry).
        """
        att = story.get("attached_story")
        if not att:
            return None
        out: dict = {}
        out.update(self._extract_ids_and_urls(att))
        out.update(self._extract_times(att))
        out.update(self._extract_audience(att))
        out.update(self._extract_author(att))
        out.update(self._extract_message(att))
        out.update(self._extract_music(att))
        out.update(self._extract_flags(att))
        out.update(self._extract_engagement(att))
        out["top_comments"] = self._extract_top_comments(att)
        out["attachments"]  = self._extract_attachments(att)
        return out


class ResponseInterceptor:
    """Intercepts and handles browser network responses"""

    def __init__(self):
        self.posts = []
        # Write-on-parse sink (JSONL migration). When set (by paginated hybrid
        # scrapes), `add_posts` routes each deduped post here — to a
        # JsonlPostWriter — instead of appending to `self.posts`, so the leg is
        # never accumulated in RAM. None => append to `self.posts` (manual mode,
        # single-shot endpoints). `post_count` tracks total added either way.
        self.post_sink = None
        self.post_count = 0
        # post_ids of every post in `self.posts`. Maintained by `add_posts`
        # so the auto-extract path and the hybrid replay path share dedup.
        # Without this, FB cursor-degraded responses (which can re-serve the
        # same posts) inflate `len(self.posts)` and defeat no-progress
        # backstops in the pagination loop. Cleared in `flush()`.
        self.seen_post_ids: set[str] = set()
        self.graphql_request_count = 0
        self.last_response_time: datetime | None = None
        self.parser = FacebookGraphQLParser()
        self.page = None
        # True once any GraphQL response body carries a non-null `viewer` object
        # (i.e., an authenticated-user context query resolved). Used to detect
        # login without relying on scrolling or the home feed rendering.
        self.viewer_seen: bool = False
        # Latest fresh per-session GraphQL tokens, parsed from natural
        # browser-issued GraphQL POST bodies. Used by hybrid mode to splice
        # fresh tokens into spliced-replay bodies (so replays don't drift
        # against FB's rotating __csr / __dyn). None until first natural
        # GraphQL POST with these fields lands.
        # NOTE: page.request.post() requests do NOT trigger this listener,
        # so manual replays will not pollute these values.
        self.latest_csr: str | None = None
        self.latest_dyn: str | None = None
        # When False, posts parsed from auto-intercepted GraphQL responses are
        # NOT appended to self.posts. Hybrid mode flips this off so its
        # natural bootstrap-scroll + organic-burst responses (which do not
        # carry beforeTime/afterTime filters) cannot pollute the result with
        # off-date-range posts. Manual mode keeps it True — auto-extraction is
        # how it collects posts. Token tracking, viewer detection, and the
        # network_capture all keep working regardless of this flag.
        self.extract_posts: bool = True
        # Opt-in for ProfileInfo/ProfileAbout only (set by their hybrid
        # methods, mirroring extract_posts above): skip response.body() once
        # nothing downstream needs it (viewer already confirmed, posts not
        # being extracted), rather than reading a body just to discard it.
        # Defaults False so every other endpoint's behavior is unchanged.
        self.skip_unneeded_body_reads: bool = False
        # Latest captured ProfileCometTimelineFeedRefetchQuery request, if any.
        # Used by hybrid mode to grab a replay template (form body + headers)
        # without holding the full network_capture in memory. Updated whenever
        # a natural PCTFRQ request is observed; reset on flush().
        # Shape: {"post_data": str | None, "headers": dict[str, str]}
        self.latest_pctfrq_request: dict | None = None
        # Latest captured SearchCometResultsPaginatedResultsQuery request, if any.
        # Same role as latest_pctfrq_request, but for the Search endpoint's
        # hybrid mode. Reset on flush().
        self.latest_scrq_request: dict | None = None
        # Latest captured GroupsCometFeedRegularStoriesPaginationQuery request,
        # if any. Same role as latest_pctfrq_request, but for the GroupTimeline
        # endpoint's hybrid mode. Reset on flush().
        self.latest_gcfrspq_request: dict | None = None
        # Latest captured CommentsListComponentsPaginationQuery request, if any.
        # Same role as latest_pctfrq_request, but for the CommentsList
        # endpoint's hybrid mode. Reset on flush().
        self.latest_clcpq_request: dict | None = None
        # Latest captured natural GraphQL POST (any friendly-name). Populated
        # on every browser-issued GraphQL POST observed. Used by single-shot
        # endpoints (e.g., PageTransparency) that synthesize the request body
        # rather than waiting for a specific friendly-name to fire naturally
        # — they only need the auth-bearing fields (fb_dtsg, lsd, __user,
        # __csr, __dyn, cookies, etc.), which are cross-cutting across all
        # GraphQL POSTs from the same session. Reset on flush().
        self.latest_natural_graphql_request: dict | None = None
        # Capture full request+response of every response, opt-in only.
        # Off by default (production hybrid does not need it). Enable with
        # FB_NETWORK_CAPTURE_ALL=1 for offline forensic analysis. When off,
        # nothing is appended; when on, every response (XHR + others) is
        # recorded — body kept verbatim for textual types, metadata+size for
        # binaries. See docs/hybrid/overview.md.
        self.network_capture: list[dict] = []

    def setup_interception(self, page: Page):
        """
        Setup response interception on page

        Args:
            page: Playwright page object
        """
        self.page = page
        self.page.on("response", self.intercept_response)
        logger.info("Response interception enabled")

    def stop_interception(self):
        if self.page:
            self.page.remove_listener("response", self.intercept_response)
            self.page = None

    async def intercept_response(self, response: Response):
        """
        Callback for intercepted responses

        Args:
            response: Playwright response object
        """
        url = response.url
        resource_type = response.request.resource_type
        is_xhr = resource_type == 'xhr'

        # GraphQL detection only meaningful for XHR.
        graphql_endpoints = (
            "https://www.facebook.com/api/graphql/",
            "https://www.facebook.com/graphql/"
        )
        is_graphql = is_xhr and any(url.startswith(ep) for ep in graphql_endpoints)

        # GraphQL-specific bookkeeping: counter for diagnostics, last-response
        # timestamp drives the stall watchdog in user_timeline; the PCTFRQ
        # template hook feeds hybrid mode's replay-template capture.
        if is_graphql:
            self.graphql_request_count += 1
            self.last_response_time = datetime.now(timezone.utc)
            self._track_request_tokens(response.request)
            await self._track_pctfrq_template(response.request)
            await self._track_scrq_template(response.request)
            await self._track_gcfrspq_template(response.request)
            await self._track_clcpq_template(response.request)
            await self._track_any_graphql_request(response.request)

        # Full network capture is opt-in via FB_NETWORK_CAPTURE_ALL=1. Off by
        # default to keep production memory tight. When on, every response is
        # recorded (textual bodies verbatim, binaries metadata-only). See
        # docs/hybrid/overview.md.
        if os.environ.get("FB_NETWORK_CAPTURE_ALL") == "1":
            try:
                await self._capture_response(response, url, resource_type, is_xhr, is_graphql)
            except Exception as e:
                logger.warning(f"[CAPTURE] Failed to record {resource_type} for {url}: {e}")

        # Only XHR-GraphQL responses go through the parser / viewer detector.
        if not is_graphql:
            return

        # ProfileInfo/ProfileAbout only (see skip_unneeded_body_reads): once
        # login is confirmed and posts aren't being extracted, nothing below
        # uses the body — skip the read entirely rather than attempt one
        # that's guaranteed to be discarded.
        if self.skip_unneeded_body_reads and self.viewer_seen and not self.extract_posts:
            return

        try:
            body = await response.body()
            body_text = body.decode("utf-8", errors="replace")
            # Detect non-null `data.viewer` — canonical logged-in marker.
            if not self.viewer_seen:
                try:
                    for doc in parse_json_or_jsonl(body_text):
                        if (
                            isinstance(doc, dict)
                            and isinstance(doc.get("data"), dict)
                            and isinstance(doc["data"].get("viewer"), dict)
                        ):
                            self.viewer_seen = True
                            break
                except Exception:
                    pass
            if self.extract_posts:
                parsed = self.parser.parse_timeline_response(body, url)
                if parsed:
                    self.add_posts(parsed['posts'])
                else:
                    logger.warning(f"[PARSER] Returned None - parser needs implementation")

        except PlaywrightError as e:
            # Benign shutdown race: browser closed while a response.body() callback
            # was still queued on the event loop. Demote to debug — cookies/scrape
            # state already persisted by the time close() runs.
            if "Target page, context or browser has been closed" in str(e):
                logger.debug(f"Response intercept skipped (browser closing): {url}")
            else:
                logger.error(f"[ERROR] Error intercepting response: {e}")
                traceback.print_exc()
        except Exception as e:
            logger.error(f"[ERROR] Error intercepting response: {e}")
            traceback.print_exc()

    async def _capture_response(
        self,
        response: Response,
        url: str,
        resource_type: str,
        is_xhr: bool,
        is_graphql: bool,
    ):
        """TEMP: record one response (request + response) into self.network_capture.

        Used by the Path B investigation to determine which queries fire,
        what request shape they need, and what surrounding telemetry exists
        that direct GraphQL replay would have to mimic.

        For textual resource types (xhr, fetch, document, script, stylesheet,
        websocket, preflight) the response body is decoded as UTF-8 and stored
        verbatim. For binary types (image, font, media) the body bytes are
        NOT stored — only `body_size` is recorded — to keep capture files
        from ballooning into gigabytes.

        Remove when investigation is done.
        """
        req = response.request
        try:
            req_headers = await req.all_headers()
        except Exception:
            req_headers = dict(req.headers) if req.headers else {}
        try:
            resp_headers = await response.all_headers()
        except Exception:
            resp_headers = dict(response.headers) if response.headers else {}

        # Request body: `req.post_data` decodes strict UTF-8 internally, which
        # throws `UnicodeDecodeError` on FB beacon endpoints (e.g. /ajax/bnzai)
        # whose POST bodies are compressed/binary. Fall back to the async
        # buffer accessor with `errors="replace"`, and record skip if even
        # that fails. We don't actually need the body content for these
        # endpoints — knowing the URL + method is enough — so a lossy decode
        # is fine.
        post_data_text: str | None = None
        post_data_size: int | None = None
        post_data_skipped: bool = False
        try:
            post_data_text = req.post_data
            if post_data_text is not None:
                post_data_size = len(post_data_text.encode("utf-8", errors="replace"))
        except UnicodeDecodeError:
            try:
                buf = await req.post_data_buffer()
                if buf is not None:
                    post_data_size = len(buf)
                    post_data_text = buf.decode("utf-8", errors="replace")
                    post_data_skipped = True  # lossy — original was non-UTF-8
            except Exception:
                post_data_skipped = True
        except Exception:
            post_data_skipped = True

        resp_body_text: str | None = None
        resp_body_size: int | None = None
        body_skipped: bool = False
        if resource_type in _TEXT_RESOURCE_TYPES:
            try:
                resp_body = await response.body()
                resp_body_size = len(resp_body)
                resp_body_text = resp_body.decode("utf-8", errors="replace")
            except Exception:
                pass
        else:
            # Binary / non-text resource: record size only, skip body bytes.
            try:
                resp_body = await response.body()
                resp_body_size = len(resp_body)
            except Exception:
                pass
            body_skipped = True

        self.network_capture.append({
            "url": url,
            "timestamp": datetime.now(timezone.utc),
            "is_xhr": is_xhr,
            "is_graphql": is_graphql,
            "request": {
                "method": req.method,
                "resource_type": resource_type,
                "headers": req_headers,
                "post_data": post_data_text,
                "post_data_size": post_data_size,
                "post_data_skipped": post_data_skipped,
            },
            "response": {
                "status": response.status,
                "headers": resp_headers,
                "body": resp_body_text,
                "body_size": resp_body_size,
                "body_skipped": body_skipped,
            },
        })

    def get_posts(self) -> list[dict]:
        """Get collected posts"""
        return self.posts

    def add_posts(self, posts: list[dict]) -> int:
        """Append posts parsed elsewhere (e.g. by a hybrid replay path)
        to the same accumulator that auto-intercepted posts populate.
        Preferred over directly mutating `self.posts`.

        Dedups on `post_id` against `self.seen_post_ids`. The parser's
        `parse_timeline_response` emits one Story per entry with shape
        `{node: Story, ...}` — the `post_id` lives on `node`, not at the
        top level, so we check both. Posts without a `post_id` anywhere are
        appended as-is (defensive — the parser should always set one).
        Returns the count of posts actually added.
        """
        added = 0
        for post in posts:
            pid = post.get("post_id") or _g(post, "node", "post_id")
            if pid:
                if pid in self.seen_post_ids:
                    continue
                self.seen_post_ids.add(pid)
            if self.post_sink is not None:
                self.post_sink(post)        # write-on-parse: stream to disk
            else:
                self.posts.append(post)     # accumulate in RAM (manual/single-shot)
            self.post_count += 1
            added += 1
        return added

    def _track_request_tokens(self, request):
        """Parse `__csr` and `__dyn` from a natural GraphQL POST body and
        update `latest_csr` / `latest_dyn`. Called from intercept_response
        only on browser-issued GraphQL XHRs — page.request.post replays
        bypass the page event stream, so they cannot self-pollute these.

        Best-effort: any parse / decode failure silently leaves the cached
        values alone.
        """
        try:
            post_data = request.post_data
        except Exception:
            return
        if not post_data:
            return
        try:
            form = parse_qs(post_data, keep_blank_values=True)
        except Exception:
            return
        # parse_qs returns lists; take last value
        csr = form.get("__csr")
        if csr and csr[-1]:
            self.latest_csr = csr[-1]
        dyn = form.get("__dyn")
        if dyn and dyn[-1]:
            self.latest_dyn = dyn[-1]

    async def _track_pctfrq_template(self, request):
        """If this request is a `ProfileCometTimelineFeedRefetchQuery`, save
        a small replay-template snapshot (post_data + headers) to
        `self.latest_pctfrq_request`. Hybrid mode polls this attr to grab
        the form template without holding a full network capture in memory.

        Friendly name lives in two places — the `x-fb-friendly-name` request
        header, or `fb_api_req_friendly_name` inside the urlencoded body.
        Check both so we don't miss either fronted.
        """
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers) if request.headers else {}

        is_pctfrq = headers.get("x-fb-friendly-name") == "ProfileCometTimelineFeedRefetchQuery"
        post_data = None
        if not is_pctfrq:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
            if post_data:
                try:
                    form = parse_qs(post_data, keep_blank_values=True)
                    name = form.get("fb_api_req_friendly_name") or []
                    if name and name[-1] == "ProfileCometTimelineFeedRefetchQuery":
                        is_pctfrq = True
                except Exception:
                    pass
        if not is_pctfrq:
            return

        # Only fetch post_data if we haven't already (header-fast-path skips it).
        if post_data is None:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
        self.latest_pctfrq_request = {
            "post_data": post_data,
            "headers": headers,
        }

    async def _track_scrq_template(self, request):
        """If this request is a `SearchCometResultsPaginatedResultsQuery`, save
        a small replay-template snapshot (post_data + headers) to
        `self.latest_scrq_request`. Mirrors `_track_pctfrq_template` for the
        Search endpoint's hybrid mode.
        """
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers) if request.headers else {}

        is_scrq = headers.get("x-fb-friendly-name") == "SearchCometResultsPaginatedResultsQuery"
        post_data = None
        if not is_scrq:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
            if post_data:
                try:
                    form = parse_qs(post_data, keep_blank_values=True)
                    name = form.get("fb_api_req_friendly_name") or []
                    if name and name[-1] == "SearchCometResultsPaginatedResultsQuery":
                        is_scrq = True
                except Exception:
                    pass
        if not is_scrq:
            return

        if post_data is None:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
        self.latest_scrq_request = {
            "post_data": post_data,
            "headers": headers,
        }

    async def _track_gcfrspq_template(self, request):
        """If this request is a `GroupsCometFeedRegularStoriesPaginationQuery`,
        save a small replay-template snapshot (post_data + headers) to
        `self.latest_gcfrspq_request`. Mirrors `_track_pctfrq_template` for
        the GroupTimeline endpoint's hybrid mode.
        """
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers) if request.headers else {}

        is_gcfrspq = headers.get("x-fb-friendly-name") == "GroupsCometFeedRegularStoriesPaginationQuery"
        post_data = None
        if not is_gcfrspq:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
            if post_data:
                try:
                    form = parse_qs(post_data, keep_blank_values=True)
                    name = form.get("fb_api_req_friendly_name") or []
                    if name and name[-1] == "GroupsCometFeedRegularStoriesPaginationQuery":
                        is_gcfrspq = True
                except Exception:
                    pass
        if not is_gcfrspq:
            return

        if post_data is None:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
        self.latest_gcfrspq_request = {
            "post_data": post_data,
            "headers": headers,
        }

    async def _track_clcpq_template(self, request):
        """If this request is a `CommentsListComponentsPaginationQuery`, save
        a small replay-template snapshot (post_data + headers) to
        `self.latest_clcpq_request`. Mirrors `_track_pctfrq_template` for
        the CommentsList endpoint's hybrid mode.
        """
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers) if request.headers else {}

        is_clcpq = headers.get("x-fb-friendly-name") == "CommentsListComponentsPaginationQuery"
        post_data = None
        if not is_clcpq:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
            if post_data:
                try:
                    form = parse_qs(post_data, keep_blank_values=True)
                    name = form.get("fb_api_req_friendly_name") or []
                    if name and name[-1] == "CommentsListComponentsPaginationQuery":
                        is_clcpq = True
                except Exception:
                    pass
        if not is_clcpq:
            return

        if post_data is None:
            try:
                post_data = request.post_data
            except Exception:
                post_data = None
        self.latest_clcpq_request = {
            "post_data": post_data,
            "headers": headers,
        }

    async def _track_any_graphql_request(self, request):
        """Save (post_data, headers) for any natural GraphQL POST to
        `self.latest_natural_graphql_request`. Called from intercept_response
        on every GraphQL request — last-write-wins.

        Used by single-shot endpoints (e.g., PageTransparency) to harvest
        auth-bearing fields from organic traffic without waiting for a
        specific friendly-name to fire. The endpoint then synthesizes its
        own body by overriding `fb_api_req_friendly_name`, `variables`, and
        `doc_id` while inheriting cross-cutting fields (fb_dtsg, lsd,
        __user, __csr, __dyn, etc.) from the captured template.

        Skips non-POST requests (preflights, GETs).
        """
        if request.method != "POST":
            return
        try:
            headers = await request.all_headers()
        except Exception:
            headers = dict(request.headers) if request.headers else {}
        try:
            post_data = request.post_data
        except Exception:
            post_data = None
        if not post_data:
            return
        self.latest_natural_graphql_request = {
            "post_data": post_data,
            "headers": headers,
        }

    def has_graphql_activity(self) -> bool:
        """Check if any GraphQL requests have been intercepted"""
        return self.graphql_request_count > 0

    def has_viewer_response(self) -> bool:
        """True if any intercepted response body contained a non-null `data.viewer` object."""
        return self.viewer_seen

    def get_graphql_request_count(self) -> int:
        """Get the number of GraphQL requests intercepted"""
        return self.graphql_request_count

    def flush(self):
        """Clear collected data and reset counters"""
        self.posts = []
        self.post_sink = None
        self.post_count = 0
        self.seen_post_ids = set()
        self.graphql_request_count = 0
        self.last_response_time = None
        self.viewer_seen = False
        self.latest_csr = None
        self.latest_dyn = None
        self.latest_pctfrq_request = None
        self.latest_scrq_request = None
        self.latest_gcfrspq_request = None
        self.latest_clcpq_request = None
        self.latest_natural_graphql_request = None
        # network_capture is opt-in (FB_NETWORK_CAPTURE_ALL=1); reset anyway
        # so a fresh scrape starts with a clean slate when capture is enabled.
        self.network_capture = []

    def save_network_capture_to_jsonl(self, path: str) -> int:
        """Write captured XHR (request + response) to a JSONL file.

        Each line is one full request/response pair, suitable for offline
        analysis to determine what we'd need to replay to drive scraping
        without the renderer (Path B).

        Args:
            path: Destination file path. Parent dirs must exist.

        Returns:
            Number of records written.
        """
        count = 0
        with open(path, "w", encoding="utf-8") as f:
            for r in self.network_capture:
                ts = r["timestamp"]
                record = {
                    "url": r["url"],
                    "timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
                    "is_xhr": r.get("is_xhr"),
                    "is_graphql": r["is_graphql"],
                    "request": r["request"],
                    "response": r["response"],
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        logger.info(f"Wrote {count} network records to {path}")
        return count
