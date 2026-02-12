"""
Browser management and page control for Facebook scraping
"""
from .accounts_pool import AccountsPool
from .response import ResponseInterceptor
from .account import Account
from .logger import logger
from .models import ScrapingResult, Query
from .utils import recursively_get_dict_value
from .exceptions import FailedLoginError

import asyncio
import random
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright, Browser, Locator
from camoufox.async_api import AsyncNewBrowser
from typing import Optional


class BrowserSession:
    """Manages browser session and page navigation"""

    # ==================== Initialization & Lifecycle ====================

    def __init__(
            self,
            account: Account,
            pool: AccountsPool,
            headless: bool = False,
            mobile: bool = False
    ):
        self.account = account
        self.pool = pool
        self.headless = headless
        self.mobile = mobile

        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.response_interceptor: Optional[ResponseInterceptor] = None

    @classmethod
    async def create(cls, account: Account, pool: AccountsPool, headless=False, mobile: bool = False):
        instance = cls(account=account, pool=pool, headless=headless, mobile=mobile)
        await instance.initialize()
        return instance

    async def __aenter__(self):
        """Async context manager entry - initialize browser session"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - close browser session"""
        await self.close()
        return False  # Don't suppress exceptions

    async def initialize(self):
        """Initialize browser session with playwright and camoufox"""
        self._pw = await async_playwright().start()

        # Get proxy settings from account
        proxy_settings = self._get_proxy_dict()

        # Create browser context using camoufox
        self._browser: Browser = await AsyncNewBrowser(
            playwright=self._pw,
            humanize=True,
            headless=self.headless,
            proxy=proxy_settings,
            geoip=True if proxy_settings else False,
            os=self.account.os,
        )

        self._context: BrowserContext = await self._browser.new_context()

        # Create page
        self.page = await self._context.new_page()

        # To-do: Workaround for camoufox issue #473: br/zstd decompression broken
        await self.page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

        # Set up a response interceptor
        self.response_interceptor = ResponseInterceptor()
        self.response_interceptor.setup_interception(self.page)

        # Inject cookies from account if available (already in Playwright format)
        if self.account.cookies:
            try:
                await self._context.add_cookies(self.account.cookies)
                logger.info(f"Injected {len(self.account.cookies)} cookies for {self.account.identifier}")
            except Exception as e:
                logger.warning(f"Failed to inject cookies for {self.account.identifier}: {e}")
        else:
            successful_login = await self.login()
            if not successful_login:
                raise FailedLoginError(f"Failed to login for {self.account.identifier}")

        await self.page.goto("https://www.facebook.com", wait_until="domcontentloaded")

        logger.info(f"Browser session initialized for {self.account.identifier}")

    async def close(self):
        """Close browser session and cleanup resources"""
        # Save cookies BEFORE closing browser (requires active context)
        try:
            await self.save_cookies()
        except Exception as e:
            logger.warning(f"Failed to save cookies on close: {e}")

        if self.response_interceptor:
            self.response_interceptor.stop_interception()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

        logger.info(f"Browser session closed for {self.account.identifier}")

    # ==================== Authentication ====================

    async def login(self) -> bool:
        """
        Execute Facebook login flow if needed.

        Returns:
            True if login successful or already logged in, False otherwise
        """
        # Check if already logged in
        if await self.check_logged_in(timeout=5.0):
            return True

        # Decline cookies popup
        await self._clear_pre_login_popups()

        # Check if login form is visible
        if not await self._is_login_form_visible():
            logger.warning(f"Cannot login {self.account.identifier}: no login form and not logged in")
            return False

        logger.info(f"Logging in to Facebook as {self.account.identifier}")

        try:
            # Fill username with human-like typing
            await self._human_type(
                self.page.get_by_label('Email or phone number'),
                self.account.identifier
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Fill password with human-like typing
            await self._human_type(
                self.page.get_by_label('Password'),
                self.account.password
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Click login button
            if self.mobile:
                await self.page.get_by_role('button', name='Log in').click()
            else:
                await self.page.get_by_role('button', name='Log in').nth(0).click()

            logger.info("Login form submitted")
            await asyncio.sleep(5)

            # Check if login was successful
            if await self.check_logged_in(timeout=10.0):
                # Save cookies after successful login
                await self.save_cookies()
                # Mark account as active
                await self.pool.set_active(self.account.identifier, True)
                # Clear any post-login popups
                await self._clear_post_login_popups()
                logger.info(f"Login successful for {self.account.identifier}")
                return True
            else:
                logger.warning(f"Login failed for {self.account.identifier}")
                return False

        except Exception as e:
            logger.error(f"Login error for {self.account.identifier}: {e}")
            return False

    async def check_logged_in(self, timeout: float = 10.0) -> bool:
        """
        Check if logged in by navigating to facebook.com and checking for GraphQL activity.
        Updates last_used on successful login.

        Args:
            timeout: Max seconds to wait for GraphQL responses

        Returns:
            True if GraphQL activity detected (logged in), False otherwise
        """
        # Create a temporary interceptor for this check
        temp_interceptor = ResponseInterceptor()
        temp_interceptor.setup_interception(self.page)

        try:
            await self.page.goto("https://www.facebook.com", wait_until="domcontentloaded")

            # Wait for GraphQL activity in intercepted responses
            elapsed = 0.0
            interval = 0.5
            while elapsed < timeout:
                if temp_interceptor.has_graphql_activity():
                    logger.info(f"Logged in: intercepted {temp_interceptor.get_graphql_request_count()} GraphQL requests")
                    # Update last_used on successful login check
                    await self.pool.update_last_used(self.account.identifier)
                    return True
                await asyncio.sleep(interval)
                elapsed += interval

            logger.warning(f"Not logged in: no GraphQL activity after {timeout}s")
            return False
        finally:
            temp_interceptor.stop_interception()

    async def get_cookies(self) -> list[dict]:
        """Get cookies from current browser context"""
        storage_state = await self._context.storage_state()
        return storage_state['cookies']

    async def save_cookies(self):
        """Save current cookies to the database"""
        cookies = await self.get_cookies()
        await self.pool.update_cookies(self.account.identifier, cookies)
        logger.info(f"Saved cookies for {self.account.identifier}")

    # ==================== Scraping ====================

    async def scrape_user_homepage(
        self,
        handle: str,
        start_date: str,
        end_date: str,
    ) -> ScrapingResult:
        """
        Scrape a Facebook user's homepage using GraphQL response interception.

        Args:
            handle: Facebook username/handle
            start_date: Start date for scraping (YYYY-MM-DD)
            end_date: End date for scraping (YYYY-MM-DD)

        Returns:
            ScrapingResult with outcome and collected data
        """
        base_url = "https://www.facebook.com/"
        target_url = f"{base_url}{handle}/"

        scrape_start_time = datetime.now(timezone.utc)
        start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
        end_datetime = datetime.strptime(end_date, "%Y-%m-%d")

        # Create Query object for this scrape
        query = Query(
            endpoint="user_page",
            query={
                "handle": handle,
                "start_date": start_date,
                "end_date": end_date,
            },
            params={},
            start_date=start_datetime,
            end_date=end_datetime
        )

        total_scrolls = 0
        no_new_posts_count = 0
        previous_post_count = 0

        logger.info(f"Scraping @{handle}'s homepage from {start_date} to {end_date}")

        # Clear any existing intercepted data
        self.response_interceptor.flush()

        while True:
            try:
                # Check for error conditions
                error = await self.check_error_conditions()
                if error:
                    return ScrapingResult(
                        query=query,
                        result=error,
                        posts=self.response_interceptor.get_posts(),
                        time_started=scrape_start_time,
                        time_taken=datetime.now(timezone.utc) - scrape_start_time
                    )

                # Navigate to target page if needed
                if not self.is_on_page(target_url):
                    logger.info(f"Navigating to {target_url}")
                    await self.goto(target_url)
                    await asyncio.sleep(5)

                    # Check if we got logged out
                    if not self.is_on_page(target_url):
                        return ScrapingResult(
                            query=query,
                            result='logged out while scraping',
                            posts=self.response_interceptor.get_posts(),
                            time_started=scrape_start_time,
                            time_taken=datetime.now(timezone.utc) - scrape_start_time
                        )

                # Get currently intercepted posts
                posts = self.response_interceptor.get_posts()
                current_post_count = len(posts)

                logger.debug(f"Scrolled {total_scrolls} times, intercepted {current_post_count} posts")

                # Check if we're making progress
                if current_post_count == previous_post_count:
                    no_new_posts_count += 1

                    if no_new_posts_count > 20:
                        if current_post_count == 0:
                            return ScrapingResult(
                                query=query,
                                result='no posts',
                                posts=[],
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                        else:
                            return ScrapingResult(
                                query=query,
                                result='scraped until first ever post was reached',
                                posts=posts,
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )
                else:
                    no_new_posts_count = 0
                    previous_post_count = current_post_count

                # Check timestamps if we have posts
                if current_post_count > 0:
                    oldest_timestamp = self._find_oldest_post_timestamp(posts)

                    if oldest_timestamp:
                        logger.debug(f"Oldest post: {oldest_timestamp}, target: {start_datetime}")

                        # Check if we've reached the target date
                        if oldest_timestamp.replace(tzinfo=None) < start_datetime:
                            logger.info(f"Reached target start date for @{handle}")
                            return ScrapingResult(
                                query=query,
                                result='scraped until user-specified starting date was reached',
                                posts=self.response_interceptor.get_posts(),
                                time_started=scrape_start_time,
                                time_taken=datetime.now(timezone.utc) - scrape_start_time
                            )

                # Scroll to trigger loading more posts
                await self.page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                total_scrolls += 1

                # Record scroll in database
                await self.record_scroll("user_page")

                # Rate limiting
                if total_scrolls % 20 == 0:
                    logger.info(f"@{handle}: {current_post_count} posts after {total_scrolls} scrolls - pausing 30s")
                    await asyncio.sleep(30)

                await asyncio.sleep(2)  # Give time for GraphQL responses to arrive

            except Exception as e:
                logger.error(f"Error scraping @{handle}: {e}")
                return ScrapingResult(
                    query=query,
                    result=f'error: {str(e)}',
                    posts=self.response_interceptor.get_posts(),
                    time_started=scrape_start_time,
                    time_taken=datetime.now(timezone.utc) - scrape_start_time
                )

    async def check_error_conditions(self) -> str | None:
        """
        Check for Facebook error conditions on current page.

        Returns:
            Error code string if error detected, None otherwise
        """
        # Check for "Retry" button (multiple error cases)
        retry_button = self.page.get_by_role("button", name="Retry")
        if await retry_button.count() > 0:
            # Case 1: Account is private
            account_is_private = self.page.get_by_text("account is private")
            if await account_is_private.count() > 0:
                return 'account is private'

            # Case 2: Failed to load
            failed_to_load = self.page.get_by_text("Failed to Load")
            if await failed_to_load.count() > 0:
                return 'failed to load'

        # Check for "Reload page" button
        reload_button = self.page.get_by_role('button', name='Reload page')
        if await reload_button.count() > 0:
            something_went_wrong = self.page.get_by_text("Something went wrong")
            if await something_went_wrong.count() > 0:
                return 'something went wrong - reload'

        # Check if profile is not available
        profile_not_available = self.page.get_by_text("Profile isn't available")
        if await profile_not_available.count() > 0:
            return 'profile is not available'

        # Check for "Sorry, this page isn't available"
        page_not_available = self.page.get_by_text("Sorry, this page isn't available")
        if await page_not_available.count() > 0:
            return 'page not available'

        # Check for "No Posts Yet"
        no_posts = self.page.get_by_text("No Posts Yet")
        if await no_posts.count() > 0:
            return 'no posts'

        # Check for "This account is private"
        private_account = self.page.get_by_text("This account is private")
        if await private_account.count() > 0:
            return 'account is private'

        return None

    async def record_scroll(self, endpoint: str, count: int = 1):
        """
        Record scroll(s) for the current account and update the database.

        Args:
            endpoint: The endpoint being scrolled (e.g., 'user_page', 'search')
            count: Number of scrolls to record (default 1)
        """
        await self.pool.update_scroll_count(self.account.identifier, endpoint, count)

    async def get_scroll_count(self, endpoint: str | None = None) -> int:
        """
        Get scroll count for the current account.

        Args:
            endpoint: If provided, get count for specific endpoint; otherwise get overall 24h count

        Returns:
            Scroll count
        """
        return await self.pool.get_scroll_count(self.account.identifier, endpoint)

    # ==================== Navigation ====================

    async def goto(self, url: str, timeout: int = 30000, wait_until: str = "domcontentloaded"):
        """Navigate to URL"""
        await self.page.goto(url, timeout=timeout, wait_until=wait_until)

    def is_on_page(self, url: str) -> bool:
        """Check if currently on a specific URL"""
        return self.page.url == url

    async def scroll_to_element(self, element):
        """Scroll element into view"""
        await element.scroll_into_view_if_needed()

    def find_elements(self, selector: str):
        """Query elements by selector"""
        return self.page.locator(selector)

    # ==================== Private Helpers ====================

    def _get_proxy_dict(self) -> dict | None:
        """Build proxy configuration dict from account settings"""
        if self.account.proxy_server:
            if self.account.proxy_username and self.account.proxy_password:
                return {
                    "server": self.account.proxy_server,
                    "username": self.account.proxy_username,
                    "password": self.account.proxy_password,
                }
            else:
                logger.warning(
                    f"Proxy username ({self.account.proxy_username}) and password ({self.account.proxy_password}) "
                    f"are required if proxy server is set."
                )
        return None

    async def _is_login_form_visible(self) -> bool:
        """Check if Facebook login form is visible"""
        try:
            await self.page.get_by_label("Email or phone number").or_(
                self.page.get_by_label("Password")
            ).or_(
                self.page.get_by_role("button", name="Log in", exact=True)
            ).first.wait_for(state="visible", timeout=5000)
            return True
        except Exception:
            return False

    async def _decline_optional_cookies(self) -> bool:
        """Decline optional cookies popup if present"""
        try:
            await self.page.get_by_role('button', name='Decline optional cookies').nth(0).click(timeout=5000)
            logger.info("Declined optional cookies")
            return True
        except Exception:
            return False

    async def _clear_pre_login_popups(self):
        """Dismiss pre-login popup dialogs"""
        await self._decline_optional_cookies()

    async def _clear_post_login_popups(self):
        """Dismiss post-login popup dialogs"""
        label = 'Not now' if self.mobile else 'Not Now'
        try:
            await self.page.get_by_role('button', name=label).nth(0).click(timeout=5000)
            logger.info("Dismissed post-login popup")
        except Exception:
            pass  # No popup to dismiss

    async def _human_type(self, locator: Locator, text: str, mean_delay: float = 0.1, std_dev: float = 0.03):
        """
        Type text with human-like timing using a normal distribution for delays.

        Args:
            locator: Playwright locator to type into
            text: Text to type
            mean_delay: Mean delay between keystrokes in seconds (default 100ms)
            std_dev: Standard deviation of delay in seconds (default 30ms)
        """
        await locator.click()
        for char in text:
            await locator.press(char)
            # Sample delay from normal distribution, clamp to avoid negative/extreme values
            delay = max(0.02, random.gauss(mean_delay, std_dev))
            await asyncio.sleep(delay)

    def _find_oldest_post_timestamp(self, posts: list[dict]) -> datetime | None:
        """Find the oldest timestamp among intercepted posts"""
        oldest_timestamp = None

        for post in posts:
            # Try multiple possible timestamp fields
            ts = (
                recursively_get_dict_value(post, 'timestamp.story.creation_time') or
                recursively_get_dict_value(post, 'created_time')
            )

            if ts:
                try:
                    # Extract from dict if multiple values
                    if isinstance(ts, dict):
                        if len(set(ts.values())) == 1:
                            ts = list(ts.values()).pop()
                        else:
                            continue

                    # Handle both Unix timestamp and datetime string
                    if isinstance(ts, (int, float)):
                        post_datetime = datetime.fromtimestamp(ts, tz=timezone.utc)
                    elif isinstance(ts, str):
                        post_datetime = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        continue

                    if oldest_timestamp is None or post_datetime < oldest_timestamp:
                        oldest_timestamp = post_datetime
                except Exception:
                    continue

        return oldest_timestamp
