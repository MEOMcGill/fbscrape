"""
Core Facebook homepage scraping logic
"""

from datetime import datetime, timezone, timedelta
from time import sleep
from playwright.sync_api import TimeoutError
from pika.adapters.blocking_connection import BlockingChannel

from .browser import PageController
from .response import ResponseInterceptor
from .models import ScrapingResult
from .utils import parse_facebook_date, internet_good, flatten_dict, unix_to_datetime, recursively_get_dict_value


class FacebookScraper:
    """Scrapes Facebook user homepage to collect posts"""

    def __init__(self, page_controller: PageController, response_interceptor: ResponseInterceptor):
        """
        Initialize homepage scraper

        Args:
            page_controller: Controller for page navigation
            response_interceptor: Interceptor for collecting data from responses
        """
        self.page_controller = page_controller
        self.response_interceptor = response_interceptor

    def scrape_user_homepage(
        self,
        handle: str,
        start_date: str,
        end_date: str,
        channel: BlockingChannel | None = None
    ) -> ScrapingResult:
        """
        Alternative scrape method using GraphQL response interception instead of DOM parsing.

        Args:
            handle: Facebook username/handle
            start_date: Start date for scraping (YYYY-MM-DD)
            end_date: End date for scraping (YYYY-MM-DD)
            channel: Optional RabbitMQ channel for polling

        Returns:
            ScrapingResult with outcome and collected data
        """
        base_url = "https://www.facebook.com/"
        target_url = f"{base_url}{handle}/"

        scrape_start_time = datetime.now(timezone.utc)
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d")

        total_scrolls = 0
        no_new_posts_count = 0
        previous_post_count = 0

        print(f"[V2] Scraping @{handle}'s homepage from {start_date} to {end_date}")
        print("Using GraphQL response interception method")

        while True:
            try:
                # Check for RabbitMQ events
                if channel is not None and total_scrolls % 20 == 0:
                    if channel.is_open:
                        channel.connection.process_data_events(time_limit=0)
                    else:
                        print("WARNING: channel is closed")

                # Check for error conditions
                error = self.page_controller.check_error_conditions()
                if error:
                    return ScrapingResult(
                        result=error,
                        posts=self.response_interceptor.get_posts(),
                        users=self.response_interceptor.get_users(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time
                    )

                # Navigate to target page if needed
                if not self.page_controller.is_on_page(target_url):
                    print(f"Navigating to {target_url}")
                    self.page_controller.goto(target_url)
                    sleep(5)

                    # Check if we got logged out
                    if not self.page_controller.is_on_page(target_url):
                        return ScrapingResult(
                            result='logged out while scraping',
                            posts=self.response_interceptor.get_posts(),
                            users=self.response_interceptor.get_users(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

                # ===== Use intercepted GraphQL responses =====

                # Get currently intercepted posts
                posts = self.response_interceptor.get_posts()
                current_post_count = len(posts)

                print(f"Scrolled {total_scrolls} times")
                print(f"Intercepted {current_post_count} posts so far")

                # Check if we're making progress
                if current_post_count == previous_post_count:
                    no_new_posts_count += 1
                    print(f"No new posts intercepted ({no_new_posts_count}/20)")

                    if no_new_posts_count > 20:
                        if current_post_count == 0:
                            return ScrapingResult(
                                result='no posts',
                                posts=[],
                                users=self.response_interceptor.get_users(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        else:
                            return ScrapingResult(
                                result='scraped until first ever post was reached',
                                posts=posts,
                                users=self.response_interceptor.get_users(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                else:
                    no_new_posts_count = 0
                    previous_post_count = current_post_count

                # Check timestamps if we have posts
                if current_post_count > 0:
                    # Find the oldest post (TODO: adjust 'timestamp' key based on actual GraphQL structure)
                    oldest_post = None
                    oldest_timestamp = None

                    for post in posts:
                        # Try multiple possible timestamp fields
                        ts = recursively_get_dict_value(post, 'timestamp.story.creation_time') or recursively_get_dict_value(post, 'created_time')

                        if ts:
                            try:
                                # Extract from dict
                                if len(set(ts.values())) == 1:
                                    ts = list(ts.values()).pop()
                                else:
                                    raise Exception("Multiple timestamps found")
                                # Handle both Unix timestamp and datetime string
                                if isinstance(ts, (int, float)):
                                    post_datetime = datetime.fromtimestamp(ts, tz=timezone.utc)
                                elif isinstance(ts, str):
                                    post_datetime = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                else:
                                    continue

                                if oldest_timestamp is None or post_datetime < oldest_timestamp:
                                    oldest_timestamp = post_datetime
                                    oldest_post = post
                            except Exception as e:
                                print(f"Error parsing timestamp: {e}")
                                continue

                    if oldest_timestamp:
                        print(f"Oldest post date: {oldest_timestamp}")
                        print(f"Target start date: {start_datetime}")

                        # Check if we've reached the target date
                        if oldest_timestamp.replace(tzinfo=None) < start_datetime:
                            print("Reached target start date!")
                            return ScrapingResult(
                                result='scraped until user-specified starting date was reached',
                                posts=self.response_interceptor.get_posts(),
                                users=self.response_interceptor.get_users(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                    else:
                        print("WARNING: Posts intercepted but no valid timestamps found")
                        print("This likely means FacebookGraphQLParser needs implementation")

                # Scroll to trigger loading more posts
                # Just scroll the page down by a viewport height
                self.page_controller.page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                total_scrolls += 1
                if total_scrolls % 20 == 0:
                    print(f"Scraped {total_scrolls} posts so far - sleeping 20 seconds")
                    sleep(30)

                print(f"Runtime: {datetime.now(timezone.utc) - scrape_start_time}")
                sleep(2)  # Give time for GraphQL responses to arrive

            except Exception as e:
                print(f"Unexpected error: {e}")
                raise
