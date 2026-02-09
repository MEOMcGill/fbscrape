"""
Facebook authentication and session management
"""

import json
import os
import time
from datetime import datetime
from playwright.sync_api import Page, BrowserContext


class FacebookAuth:
    """Manages Facebook login and session state"""

    def __init__(self, username: str, password: str, auth_json_path: str):
        """
        Initialize Facebook authentication

        Args:
            username: Facebook username/email/phone
            password: Facebook password
            auth_json_path: Path to save/load authentication state
        """
        self.username = username
        self.password = password
        self.auth_json_path = auth_json_path

    def cookies_expired(self) -> bool:
        """
        Check if saved cookies have expired

        Returns:
            True if cookies are expired or don't exist, False otherwise
        """
        if not os.path.exists(self.auth_json_path):
            return True

        try:
            with open(self.auth_json_path, "r") as f:
                auth_dict = json.load(f)

            for cookie in auth_dict.get("cookies", []):
                if datetime.fromtimestamp(cookie["expires"]) < datetime.now():
                    return True

            return False
        except Exception as e:
            print(f"Error checking cookie expiration: {e}")
            return True

    def need_to_log_in(self, page: Page) -> bool:
        """
        Check if login is required

        Args:
            page: Playwright page object

        Returns:
            True if login is needed, False otherwise
        """
        # Check if the login layout is showing
        if (
            page.get_by_label("Phone number, username, or email").is_visible()
            or page.get_by_label("Password").is_visible()
            or page.get_by_role("button", name="Log in", exact=True).is_visible()
        ):
            print("Login layout is showing - need to log in")
            return True

        return False

    def cookie_login(self, context: BrowserContext):
        """Login using saved cookies"""
        print(f"Logging in to Facebook as {self.username} using saved cookies")
        with open(self.auth_json_path, "r") as f:
            auth_dict = json.load(f)

        context.add_cookies(auth_dict.get("cookies", []))
        print("Cookies loaded successfully")

    def manual_login(self, page: Page, mobile: bool):
        """
        Execute Facebook login flow

        Args:
            page: Playwright page object
            mobile: Whether using mobile viewport
        """
        print(f"Logging in to Facebook as {self.username}")

        # On mobile, click the "Log in" button first
        if mobile:
            page.get_by_role('button', name='Log in').click()
            time.sleep(5)

        # Fill username
        page.get_by_label('Email or phone number').fill(self.username)
        time.sleep(1)

        # Fill password
        page.get_by_label('Password').fill(self.password)
        time.sleep(1)

        # Click login button
        if mobile:
            page.get_by_role('button', name='Log in').click()
        else:
            page.get_by_role('button', name='Log in').nth(0).click()

        print("Login form submitted")

    def save_session_state(self, context: BrowserContext):
        """
        Save browser session state to file

        Args:
            context: Browser context to save
        """
        context.storage_state(path=self.auth_json_path)
        print(f"Session state saved to {self.auth_json_path}")

    def clear_post_login_popups(self, page: Page, mobile: bool):
        """
        Dismiss post-login popup dialogs

        Args:
            page: Playwright page object
            mobile: Whether using mobile viewport
        """
        if mobile:
            label = 'Not now'
        else:
            label = "Not Now"

        try:
            page.get_by_role('button', name=label).nth(0).click(timeout=5000)
            print("Dismissed post-login popup")
        except Exception as e:
            print("No post-login popup to dismiss")
