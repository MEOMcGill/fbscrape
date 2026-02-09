"""
Response interception and Facebook GraphQL parsing
"""

import json
from playwright.sync_api import Page, Response
from fbscrape.utils import parse_json_or_jsonl, flatten_dict, recursively_get_dict_value


class FacebookGraphQLParser:
    """Parses Facebook GraphQL responses to extract posts and users"""

    def parse_timeline_response(self, body: bytes, url: str) -> dict | None:
        """
        Parse Facebook GraphQL timeline response

        Args:
            body: Response body bytes
            url: Response URL

        Returns:
            Dict with 'posts' and 'users' keys, or None if parsing fails
        """
        try:
            response_data = parse_json_or_jsonl(body.decode('utf-8'))
            posts = []
            users = []
            for data in response_data:
                # TODO: IMPLEMENT ACTUAL PARSING LOGIC
                # Once you inspect the saved JSON files (debug_graphql_*.json),
                # you'll see the structure and can implement extraction here

                if 'data' in data:
                    # post
                    if 'node' in data['data']:
                        if self.is_post_node(data['data']['node']):
                            posts.append(data['data'])
                    pass

            return {'posts': posts, 'users': users}

        except json.JSONDecodeError as e:
            print(f"[PARSER ERROR] Failed to decode JSON: {e}")
            return None
        except Exception as e:
            print(f"[PARSER ERROR] {e}")
            import traceback
            traceback.print_exc()
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
            # TODO: Implement based on actual Facebook GraphQL structure
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
            print(f"Failed to parse post node: {e}")
            return None

    def is_user_node(self, node: dict) -> dict | None:
        """
        Extract user metadata from GraphQL node

        Args:
            node: User node from GraphQL response

        Returns:
            User metadata dict or None
        """
        try:
            # TODO: Implement based on actual Facebook GraphQL structure
            # Example fields to extract:
            # - username
            # - profile_pic_url
            # - id
            pass
        except Exception as e:
            print(f"Failed to parse user node: {e}")
            return None


class ResponseInterceptor:
    """Intercepts and handles browser network responses"""

    def __init__(self):
        self.posts = []
        self.users = []
        self.parser = FacebookGraphQLParser()

    def setup_interception(self, page: Page):
        """
        Setup response interception on page

        Args:
            page: Playwright page object
        """
        page.on("response", self.intercept_response)
        print("Response interception enabled")

    def intercept_response(self, response: Response):
        """
        Callback for intercepted responses

        Args:
            response: Playwright response object
        """
        # DEBUG: Uncomment to see ALL responses
        # print(f"[DEBUG] Response: {response.request.resource_type} | {response.url[:100]}")

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

        try:
            body = response.body()

            parsed = self.parser.parse_timeline_response(body, url)

            if parsed:
                self.posts.extend(parsed['posts'])
                self.users.extend(parsed['users'])
            else:
                print(f"[PARSER] Returned None - parser needs implementation")

        except Exception as e:
            print(f"[ERROR] Error intercepting response: {e}")
            import traceback
            traceback.print_exc()

    def get_posts(self) -> list[dict]:
        """Get collected posts"""
        return self.posts

    def get_users(self) -> list[dict]:
        """Get collected users"""
        return self.users

    def flush(self):
        """Clear collected data"""
        self.posts = []
        self.users = []
