"""
Response interception and Facebook GraphQL parsing
"""

import json
import traceback
from datetime import datetime, timezone
from playwright.async_api import Page, Response
from fbscrape.utils import parse_json_or_jsonl, flatten_dict, recursively_get_dict_value
from .logger import logger


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
        # Only process XHR requests
        if response.request.resource_type != 'xhr':
            return

        url = response.url

        # Check if this is a Facebook GraphQL endpoint
        graphql_endpoints = (
            "https://www.facebook.com/api/graphql/",
            "https://www.facebook.com/graphql/"
        )

        is_graphql = any(url.startswith(endpoint) for endpoint in graphql_endpoints)
        if not is_graphql:
            return

        self.graphql_request_count += 1
        self.last_response_time = datetime.now(timezone.utc)

        try:
            body = await response.body()
            parsed = self.parser.parse_timeline_response(body, url)
            if parsed:
                self.posts.extend(parsed['posts'])
            else:
                logger.warning(f"[PARSER] Returned None - parser needs implementation")

        except Exception as e:
            logger.error(f"[ERROR] Error intercepting response: {e}")
            traceback.print_exc()

    def get_posts(self) -> list[dict]:
        """Get collected posts"""
        return self.posts

    def has_graphql_activity(self) -> bool:
        """Check if any GraphQL requests have been intercepted"""
        return self.graphql_request_count > 0

    def get_graphql_request_count(self) -> int:
        """Get the number of GraphQL requests intercepted"""
        return self.graphql_request_count

    def flush(self):
        """Clear collected data and reset counters"""
        self.posts = []
        self.graphql_request_count = 0
        self.last_response_time = None
