from typing import Tuple
from fbscrape.browser import BrowserManager, PageController

BASE_URL = "https://www.facebook.com"
mobile: bool = False
headless: bool = False

def create_browser_context(headless: bool = headless, mobile: bool = mobile) -> Tuple[BrowserManager, PageController]:
    browser_manager = BrowserManager()
    browser_manager.create_playwright_instance()
    browser_manager.create_browser_context(
        headless=headless,
        mobile=mobile
    )
    page_controller = PageController(
        browser_manager.context.new_page()
    )
    page_controller.goto(
        BASE_URL
    )

    return browser_manager, page_controller