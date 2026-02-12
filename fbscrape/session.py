"""
Facebook authentication and session management
"""

import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import Page, BrowserContext


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

    async def need_to_log_in(self, page: Page) -> bool:
        """
        Check if login is required

        Args:
            page: Playwright page object

        Returns:
            True if login is needed, False otherwise
        """
        # Check if the login layout is showing

        try:
            await page.get_by_label("Phone number, username, or email").or_(
                page.get_by_label("Password")
            ).or_(
                page.get_by_role("button", name="Log in", exact=True)
            ).first.wait_for(state="visible", timeout=5000)
            login_visible = True
        except:
            login_visible = False

        print(f"Login layout detected - {login_visible}")
        return login_visible

    async def cookie_login(self, context: BrowserContext):
        """Login using saved cookies"""
        print(f"Logging in to Facebook as {self.username} using saved cookies")
        if os.path.exists(self.auth_json_path):
            with open(self.auth_json_path, "r") as f:
                auth_dict = json.load(f)
            await context.add_cookies(auth_dict.get("cookies", []))
        print("Cookies loaded successfully")

    async def manual_login(self, page: Page, mobile: bool):
        """
        Execute Facebook login flow

        Args:
            page: Playwright page object
            mobile: Whether using mobile viewport
        """
        print(f"Logging in to Facebook as {self.username}")

        # On mobile, click the "Log in" button first
        if mobile:
            await page.get_by_role('button', name='Log in').click()
            await asyncio.sleep(5)

        # Fill username
        await page.get_by_label('Email or phone number').fill(self.username)
        await asyncio.sleep(1)

        # Fill password
        await page.get_by_label('Password').fill(self.password)
        await asyncio.sleep(1)

        # Click login button
        if mobile:
            await page.get_by_role('button', name='Log in').click()
        else:
            await page.get_by_role('button', name='Log in').nth(0).click()

        print("Login form submitted")

    async def save_session_state(self, context: BrowserContext):
        """
        Save browser session state to file

        Args:
            context: Browser context to save
        """
        await context.storage_state(path=self.auth_json_path)
        print(f"Session state saved to {self.auth_json_path}")

    async def clear_post_login_popups(self, page: Page, mobile: bool):
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
            await page.get_by_role('button', name=label).nth(0).click(timeout=5000)
            print("Dismissed post-login popup")
        except Exception as e:
            print("No post-login popup to dismiss")
