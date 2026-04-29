"""
Response interception and Facebook GraphQL parsing
"""

import json
import os
import traceback
from datetime import datetime, timezone
from playwright.async_api import Page, Response
from fbscrape.utils import parse_json_or_jsonl, flatten_dict, recursively_get_dict_value
from .logger import logger


# TEMP: resource types whose response bodies are textual and worth keeping verbatim
# for Path B investigation. Binary types (image, font, media) are recorded with
# metadata + size only — bytes are not stored. Remove when investigation is done.
_TEXT_RESOURCE_TYPES = frozenset({
    "xhr", "fetch", "document", "script", "stylesheet", "websocket", "preflight",
})


class FacebookGraphQLParser:
    """Parses Facebook GraphQL responses to extract posts"""

    def parse_timeline_response(self, body: bytes, url: str) -> dict | None:
        """
        Parse Facebook GraphQL timeline response

        Args:
            body: Response body bytes
            url: Response URL

        Returns:
            Dict with 'posts' key, or None if parsing fails
        """
        try:
            response_data = parse_json_or_jsonl(body.decode('utf-8'))
            posts = []
            for data in response_data:
                if 'data' in data:
                    if 'node' in data['data']:
                        if self.is_post_node(data['data']['node']):
                            posts.append(data['data'])

            return {'posts': posts}

        except json.JSONDecodeError as e:
            logger.error(f"[PARSER ERROR] Failed to decode JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"[PARSER ERROR] {e}")
            logger.error(traceback.print_exc())
            return None

    def flatten_post(self, post: dict) -> dict | None:
        """
        Extract basic metadata from a single post (one entry of `posts` list).

        Expects shape: {'node': {'timeline_list_feed_units': {'edges': [{'node': <Story>}]}}}

        Returns a flat dict with: post_id, story_id, url, created_at,
        author_id, author_name, author_url, text, reactions, top_reactions,
        shares, attachments. Missing fields become None.
        """
        def g(obj, *keys, default=None):
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

        # Two response shapes:
        #   A) initial page load: node is a User containing timeline_list_feed_units.edges[].node (Story)
        #   B) pagination: node IS the Story directly (has post_id at the top)
        node = g(post, 'node') or {}
        if 'timeline_list_feed_units' in node:
            story = g(node, 'timeline_list_feed_units', 'edges', 0, 'node')
        elif 'post_id' in node:
            story = node
        else:
            story = None
        if not story:
            return None

        metadata = g(story, 'comet_sections', 'context_layout', 'story',
                     'comet_sections', 'metadata', 0, 'story') or {}

        content_story = g(story, 'comet_sections', 'content', 'story') or {}

        text = (g(content_story, 'comet_sections', 'message', 'story', 'message', 'text')
                or g(content_story, 'comet_sections', 'message_container', 'story', 'message', 'text')
                or g(content_story, 'message', 'text'))

        # External URLs linked in the post text
        external_urls = []
        for ranges_path in (
            ('comet_sections', 'message', 'story', 'message', 'ranges'),
            ('comet_sections', 'message_container', 'story', 'message', 'ranges'),
            ('message', 'ranges'),
        ):
            ranges = g(content_story, *ranges_path, default=[]) or []
            for r in ranges:
                url = g(r, 'entity', 'external_url')
                if url and url not in external_urls:
                    external_urls.append(url)
            if external_urls:
                break

        feedback_context = g(story, 'comet_sections', 'feedback', 'story',
                             'story_ufi_container', 'story', 'feedback_context') or {}
        summary_fb = g(feedback_context, 'feedback_target_with_context',
                       'comet_ufi_summary_and_actions_renderer', 'feedback') or {}
        reactions = g(summary_fb, 'reaction_count', 'count')
        shares = g(summary_fb, 'share_count', 'count')
        comments = g(summary_fb, 'comments_count_summary_renderer', 'feedback',
                     'comment_rendering_instance', 'comments', 'total_count')
        video_views = g(summary_fb, 'video_view_count')
        is_live = g(summary_fb, 'video_view_count_renderer', 'feedback',
                    'associated_video', 'is_live_streaming')

        reaction_counts = {k: 0 for k in ('like', 'love', 'haha', 'wow', 'sad', 'angry', 'care')}
        for e in g(summary_fb, 'top_reactions', 'edges', default=[]) or []:
            name = g(e, 'node', 'localized_name')
            if name:
                key = name.lower()
                if key in reaction_counts:
                    reaction_counts[key] = e.get('reaction_count') or 0

        # Top comments that FB surfaces with the post
        top_comments = []
        for tc in g(feedback_context, 'interesting_top_level_comments', default=[]) or []:
            c = tc.get('comment') or {}
            c_fb = c.get('feedback') or {}
            c_reactions = g(c_fb, 'reaction_count', 'count')
            if c_reactions is None:
                summed = sum((e.get('reaction_count') or 0)
                             for e in g(c_fb, 'top_reactions', 'edges', default=[]) or [])
                c_reactions = summed or None
            top_comments.append({
                'text': g(c, 'body', 'text'),
                'author_id': g(c, 'author', 'id'),
                'author_name': g(c, 'author', 'name'),
                'author_url': g(c, 'author', 'url'),
                'created_at': c.get('created_time'),
                'reactions': c_reactions,
            })

        attachments = []
        video_duration_sec = None
        for a in g(story, 'attachments', default=[]) or []:
            media = a.get('media') or {}
            dur = g(a, 'styles', 'attachment', 'media', 'length_in_second')
            if dur and video_duration_sec is None:
                video_duration_sec = dur
            attachments.append({
                'type': media.get('__typename'),
                'id': media.get('id'),
                'url': (g(a, 'styles', 'attachment', 'media', 'url')
                        or g(a, 'url')),
                'accessibility_caption': g(a, 'styles', 'attachment', 'media',
                                           'accessibility_caption'),
            })

        # Shared / reposted content
        att = story.get('attached_story') or {}
        att_meta = (g(att, 'comet_sections', 'context_layout', 'story',
                      'comet_sections', 'metadata', 0, 'story') or {})
        att_content = g(att, 'comet_sections', 'content', 'story') or {}
        att_actor = g(att_content, 'actors', 0) or {}
        att_text = (g(att_content, 'comet_sections', 'message', 'story', 'message', 'text')
                    or g(att_content, 'comet_sections', 'message_container', 'story', 'message', 'text')
                    or g(att_content, 'message', 'text'))
        shared = {
            'shared_post_id': att.get('post_id') or att.get('id'),
            'shared_post_url': att.get('permalink_url') or att_meta.get('url'),
            'shared_post_created_at': att_meta.get('creation_time'),
            'shared_post_author_id': g(att, 'feedback', 'owning_profile', 'id') or att_actor.get('id'),
            'shared_post_author_name': g(att, 'feedback', 'owning_profile', 'name') or att_actor.get('name'),
            'shared_post_text': att_text,
        } if att else {k: None for k in (
            'shared_post_id', 'shared_post_url', 'shared_post_created_at',
            'shared_post_author_id', 'shared_post_author_name', 'shared_post_text',
        )}

        permalink_url = story.get('permalink_url')
        is_reel = bool(permalink_url and '/reel/' in permalink_url)

        privacy = None
        for m in g(story, 'comet_sections', 'context_layout', 'story',
                   'comet_sections', 'metadata', default=[]) or []:
            desc = g(m, 'story', 'privacy_scope', 'description')
            if desc:
                privacy = desc
                break

        created_at = metadata.get('creation_time')
        created_at_utc = (
            datetime.fromtimestamp(created_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            if isinstance(created_at, (int, float)) else None
        )

        return {
            'post_id': story.get('post_id'),
            'story_id': story.get('id'),
            'url': metadata.get('url') or permalink_url,
            'permalink_url': permalink_url,
            'created_at': created_at,
            'created_at_utc': created_at_utc,
            'privacy': privacy,
            'is_reel': is_reel,
            'is_live': is_live,
            'video_duration_sec': video_duration_sec,
            'video_views': video_views,
            'author_id': g(story, 'feedback', 'owning_profile', 'id'),
            'author_name': g(story, 'feedback', 'owning_profile', 'name'),
            'author_url': g(content_story, 'actors', 0, 'url'),
            'text': text,
            'external_urls': external_urls or None,
            'reactions': reactions,
            **reaction_counts,
            'shares': shares,
            'comments': comments,
            'top_comments': top_comments or None,
            **shared,
            'attachments': attachments,
        }

    def is_post_node(self, node: dict) -> bool | None:
        """
        Determine if a node is a Facebook post

        Args:
            node: Post node from GraphQL response

        Returns:
            bool indicating if node is a post, or None if parsing fails
        """
        try:
            # if there's the link to a post 'https://www.facebook.com/reel/893086710220638/'
            post_data = flatten_dict(node)
            post_url = [
                v for k, v in post_data.items()
                if isinstance(v, str) and (("/reel/" in v) or ("/posts/" in v)) and "comment_id" not in v
            ]
            # text in:
            # A: 'timeline_list_feed_units.edges.0.node.comet_sections.content.story.message.text'
            # A: 'timeline_list_feed_units.edges.0.node.comet_sections.content.story.comet_sections.message.story.message.text'
            # B: 'comet_sections.content.story.comet_sections.message.story.message.text'
            # B: 'comet_sections.content.story.comet_sections.message_container.story.message.text'
            # B: 'comet_sections.content.story.message.text'
            has_post_url = len(post_url) > 0

            # if has isFeedUnit, my hunch is it needs to be 'Story' but unsure
            is_feed_unit = 'Story' in set(recursively_get_dict_value(post_data, '__isFeedUnit').values())
            is_post: bool = has_post_url and is_feed_unit
            return is_post

        except Exception as e:
            logger.error(f"Failed to parse post node: {e}")
            return None

class ResponseInterceptor:
    """Intercepts and handles browser network responses"""

    def __init__(self):
        self.posts = []
        self.graphql_request_count = 0
        self.last_response_time: datetime | None = None
        self.parser = FacebookGraphQLParser()
        self.page = None
        # True once any GraphQL response body carries a non-null `viewer` object
        # (i.e., an authenticated-user context query resolved). Used to detect
        # login without relying on scrolling or the home feed rendering.
        self.viewer_seen: bool = False
        # TEMP: capture full request+response of every XHR (GraphQL and otherwise) for
        # Path B investigation (see docs/path_b_investigation.md). Each entry has
        # `is_graphql`, full request (method/headers/post_data), and full response
        # (status/headers/body). Cleared on flush(). Remove when investigation is done.
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
        # timestamp drives the stall watchdog in user_timeline.
        if is_graphql:
            self.graphql_request_count += 1
            self.last_response_time = datetime.now(timezone.utc)

        # TEMP: capture network traffic for Path B investigation.
        # Default scope: XHR only (preserves prior behavior).
        # If FB_NETWORK_CAPTURE_ALL=1, capture every response (CSS/JS/images/etc.)
        # — body is kept verbatim only for textual resource types; binaries get
        # metadata + size. See docs/path_b_investigation.md. Remove when done.
        capture_all = os.environ.get("FB_NETWORK_CAPTURE_ALL") == "1"
        if is_xhr or capture_all:
            try:
                await self._capture_response(response, url, resource_type, is_xhr, is_graphql)
            except Exception as e:
                logger.warning(f"[CAPTURE] Failed to record {resource_type} for {url}: {e}")

        # Only XHR-GraphQL responses go through the parser / viewer detector.
        if not is_graphql:
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
            parsed = self.parser.parse_timeline_response(body, url)
            if parsed:
                self.posts.extend(parsed['posts'])
            else:
                logger.warning(f"[PARSER] Returned None - parser needs implementation")

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
        self.graphql_request_count = 0
        self.last_response_time = None
        self.viewer_seen = False
        # TEMP: clear Path B network capture between scrapes. Remove when done.
        self.network_capture = []

    # TEMP: dump captured XHR (GraphQL + others) to JSONL for Path B investigation.
    # Remove when investigation is done. See docs/path_b_investigation.md.
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
