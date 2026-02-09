# fbscrape

A modular Facebook scraping framework built on Playwright with clean separation of concerns.

## Overview

`fbscrape` is a core scraping library that provides reusable components for building Facebook scrapers:

- **Browser Management** - Playwright lifecycle and page control
- **Authentication** - Facebook login with cookie persistence
- **Response Interception** - Capture GraphQL responses
- **Scraping Logic** - Timeline scraping with date-based filtering

## Installation

```bash
pip install playwright
playwright install chromium webkit
```

## Quick Start

```python
from fbscrape import BrowserManager, PageController, FacebookAuth, ResponseInterceptor, FacebookScraper
import os

# 1. Setup browser
browser_manager = BrowserManager()
browser_manager.create_playwright_instance()
browser_manager.create_browser_context(headless=False, mobile=False)

# 2. Create page controller
page_controller = PageController(browser_manager.context.new_page())
page_controller.goto("https://www.facebook.com")

# 3. Authenticate
auth = FacebookAuth("email@example.com", "password", "auth.json")
if auth.need_to_log_in(page_controller.page):
    auth.manual_login(page_controller.page, mobile=False)
    auth.save_session_state(browser_manager.context)

# 4. Setup response interception
response_interceptor = ResponseInterceptor()
response_interceptor.setup_interception(page_controller.page)

# 5. Scrape timeline
scraper = FacebookScraper(page_controller, response_interceptor)
result = scraper.scrape_user_homepage(
    handle="username",
    start_date="2024-01-01",
    end_date="2024-03-31"
)

print(f"Collected {len(result.posts)} posts")
```

## Components

### BrowserManager

Manages Playwright browser lifecycle.

```python
from fbscrape.browser import BrowserManager

browser_manager = BrowserManager()
browser_manager.create_playwright_instance()

# Desktop browser
context = browser_manager.create_browser_context(
    headless=False,
    mobile=False,
    auth_storage_path="auth.json"  # Optional: load saved session
)

# Mobile browser (iPhone 13 WebKit)
context = browser_manager.create_browser_context(
    headless=False,
    mobile=True,
    auth_storage_path="auth.json"
)

# Cleanup
browser_manager.close()
```

**Tested in:** `examples/test_browser.py`

### PageController

Controls page navigation and element interaction.

```python
from fbscrape.browser import PageController

page_controller = PageController(context.new_page())

# Navigate
page_controller.goto("https://www.facebook.com")

# Check current page
if page_controller.is_on_page("https://www.facebook.com/"):
    print("On Facebook homepage")

# Check for errors
error = page_controller.check_error_conditions()
if error:
    print(f"Error detected: {error}")
    # Returns: 'failed to load', 'account is private', 'no posts', etc.

# Scroll element into view
element = page_controller.page.locator("div").first
page_controller.scroll_to_element(element)
```

**Tested in:** `examples/test_browser.py`

### FacebookAuth

Handles Facebook authentication with cookie persistence.

```python
from fbscrape.session import FacebookAuth

auth = FacebookAuth(
    username="email@example.com",
    password="your_password",
    auth_json_path="./auth/email.json"
)

# Load saved cookies
auth.cookie_login(browser_manager.context)
page_controller.page.reload()

# Check if login is needed
if auth.need_to_log_in(page_controller.page):
    # Perform manual login
    auth.manual_login(
        page=page_controller.page,
        mobile=False
    )

    # Save session for future use
    auth.save_session_state(context=browser_manager.context)

    # Clear post-login popups ("Not Now" dialogs)
    auth.clear_post_login_popups(page_controller.page, mobile=False)
```

**Tested in:** `examples/test_login.py`

### ResponseInterceptor

Captures Facebook GraphQL responses for data extraction.

```python
from fbscrape.response import ResponseInterceptor

interceptor = ResponseInterceptor()

# Setup interception (must be called BEFORE navigation)
interceptor.setup_interception(page_controller.page)

# After scraping completes, retrieve collected data
posts = interceptor.get_posts()
users = interceptor.get_users()

print(f"Intercepted {len(posts)} posts")

# Clear data for next scrape
interceptor.flush()
```

**Note:** Response interception captures raw GraphQL data. The parser extracts entire post nodes from Facebook's API responses.

**Tested in:** `examples/test_scrape_page.py`

### FacebookScraper

Scrapes a user's timeline with date-based filtering.

```python
from fbscrape.scraper import FacebookScraper
from datetime import date, timedelta

scraper = FacebookScraper(
    page_controller=page_controller,
    response_interceptor=response_interceptor
)

# Scrape last 90 days
start_date = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
end_date = date.today().strftime("%Y-%m-%d")

result = scraper.scrape_user_homepage(
    handle="username",           # Facebook username/handle
    start_date=start_date,       # YYYY-MM-DD format
    end_date=end_date,           # YYYY-MM-DD format
    channel=None                 # Optional: RabbitMQ channel for polling
)

# Check result
print(f"Result: {result.result}")  # 'scraped until user-specified starting date was reached'
print(f"Posts collected: {len(result.posts)}")
print(f"Time taken: {result.time_taken}")

# Possible result values:
# - 'scraped until user-specified starting date was reached' (success)
# - 'scraped until first ever post was reached' (success)
# - 'no posts' (empty timeline)
# - 'account is private' (restricted access)
# - 'failed to load' (network/Facebook error)
# - 'logged out while scraping' (session expired)
```

**Tested in:** `examples/test_scrape_page.py`

### Utility Functions

```python
from fbscrape.utils import parse_facebook_date, save_jsonl, get_home_dir_path
from datetime import datetime

# Parse Facebook date strings
date = parse_facebook_date("2h")  # Returns datetime 2 hours ago
date = parse_facebook_date("5m")  # Returns datetime 5 minutes ago

# Save data to JSONL
save_jsonl("posts.jsonl", result.posts)

# Get project home directory
home_dir = get_home_dir_path()
print(f"Project root: {home_dir}")
```

## Complete Example

See `examples/test_scrape_page.py` for a full working example:

```bash
cd examples
python test_scrape_page.py
```

This example:
1. Creates browser context
2. Logs in to Facebook (cookie-based if available, manual otherwise)
3. Sets up response interception
4. Scrapes a user's timeline for the last 90 days
5. Saves posts to `data/posts/{handle}_posts_{start}_{end}.jsonl`

## Data Structure

Scraped posts are raw GraphQL response nodes saved as-is:

```json
{
  "id": "post_id",
  "timestamp": {"story": {"creation_time": 1234567890}},
  "message": {"text": "Post content..."},
  ...
}
```

The scraper saves the entire GraphQL node to preserve all available data. You can then extract specific fields as needed for your use case.
