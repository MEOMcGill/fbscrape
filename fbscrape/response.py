"""
Response interception and Facebook GraphQL parsing
"""

import json
import traceback
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
            is_post = len(post_url) > 0
            return is_post

        except Exception as e:
            logger.error(f"Failed to parse post node: {e}")
            return None

class ResponseInterceptor:
    """Intercepts and handles browser network responses"""

    def __init__(self):
        self.posts = []
        self.graphql_request_count = 0
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
