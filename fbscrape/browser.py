"""
Browser management and page control for Facebook scraping
"""

import os
from playwright.sync_api import sync_playwright, Page, BrowserContext, Playwright
from .utils import is_post_url


class BrowserManager:
    """Manages Playwright browser instance and lifecycle"""

    def __init__(self):
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    def create_playwright_instance(self):
        """Start Playwright instance"""
        self.playwright = sync_playwright().start()

    def create_browser_context(
        self,
        headless: bool,
        mobile: bool,
        auth_storage_path: str | None = None
    ) -> BrowserContext:
        """
        Create browser context with optional saved session

        Args:
            headless: Whether to run browser in headless mode
            mobile: Whether to use mobile viewport
            auth_storage_path: Path to saved authentication state

        Returns:
            Browser context
        """
        if self.playwright is None:
            raise RuntimeError("Playwright instance not created. Call create_playwright_instance() first.")

        # Determine storage state
        storage_state = None
        if auth_storage_path and os.path.exists(auth_storage_path):
            storage_state = auth_storage_path

        if mobile:
            # Use iPhone 13 mobile device emulation
            device = self.playwright.devices['iPhone 13']
            browser = self.playwright.webkit.launch(headless=headless)
            context = browser.new_context(
                **device,
                storage_state=storage_state
            )
        else:
            # Use desktop Chrome
            browser = self.playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=storage_state
            )

        self.context = context
        return context

    def close(self):
        """Cleanup resources"""
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()


class PageController:
    """Controls page navigation and element interaction"""

    def __init__(self, page: Page):
        self.page = page

    def goto(self, url: str, timeout: int = 30000):
        """Navigate to URL"""
        self.page.goto(url, timeout=timeout)

    def is_on_page(self, url: str) -> bool:
        """Check if currently on a specific URL"""
        return self.page.url == url

    def scroll_to_element(self, element):
        """Scroll element into view"""
        element.scroll_into_view_if_needed()

    def find_elements(self, selector: str):
        """Query elements by selector"""
        return self.page.locator(selector)

    def check_error_conditions(self) -> str | None:
        """
        Check for Facebook error conditions

        Returns:
            Error code string if error detected, None otherwise
        """
        # Check for "Retry" button (multiple error cases)
        retry_button = self.page.get_by_role("button", name="Retry")
        if retry_button.count() > 0:
            # Case 1: Account is private
            account_is_private = self.page.get_by_text("account is private")
            if account_is_private.count() > 0:
                return 'account is private'

            # Case 2: Failed to load
            failed_to_load = self.page.get_by_text("Failed to Load")
            if failed_to_load.count() > 0:
                return 'failed to load'

        # Check for "Reload page" button
        reload_button = self.page.get_by_role('button', name='Reload page')
        if reload_button.count() > 0:
            something_went_wrong = self.page.get_by_text("Something went wrong")
            if something_went_wrong.count() > 0:
                return 'something went wrong - reload'

        # Check if profile is not available
        profile_not_available = self.page.get_by_text("Profile isn't available")
        if profile_not_available.count() > 0:
            return 'profile is not available'

        # Check for "Sorry, this page isn't available"
        page_not_available = self.page.get_by_text("Sorry, this page isn't available")
        if page_not_available.count() > 0:
            return 'page not available'

        # Check for "No Posts Yet"
        no_posts = self.page.get_by_text("No Posts Yet")
        if no_posts.count() > 0:
            return 'no posts'

        # Check for "This account is private"
        private_account = self.page.get_by_text("This account is private")
        if private_account.count() > 0:
            return 'account is private'

        return None

    def find_lowest_post_element(self):
        """
        Find the lowest (last) post element on the page

        Returns:
            Post element if found, None otherwise
        """
        result = self.page.locator("a")
        lowest_post = None

        # Iterate from bottom to top
        for i in range(result.count()):
            j = result.count() - 1 - i  # Start from last index
            elt = result.nth(j)
            href = elt.get_attribute("href")

            if is_post_url(href):
                lowest_post = elt
                break

        return lowest_post

    def find_lowest_post_element_new(self):
        """
        Find the lowest (last) post element on the page using aria-posinset

        Returns:
            Post element if found, None otherwise
        """
        # Find all elements with aria-posinset attribute
        posts_with_position = self.page.locator('[aria-posinset]')

        if posts_with_position.count() == 0:
            return None

            # Find the post with the highest position number
        max_position = 0
        lowest_post = None

        for i in range(posts_with_position.count()):
            post = posts_with_position.nth(i)
            position_str = post.get_attribute('aria-posinset')

            try:
                position = int(position_str)
                if position > max_position:
                    max_position = position
                    lowest_post = post
            except (ValueError, TypeError):
                continue

        return lowest_post
