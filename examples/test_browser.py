import os
from typing import Tuple
from fbscrape.browser_session import BrowserSession
from fbscrape.account import Account
from fbscrape.accounts_pool import AccountsPool
from fbscrape.utils import get_home_dir_path
from fbscrape.exceptions import FailedLoginError
import asyncio

BASE_URL = "https://www.facebook.com"
mobile: bool = False
headless: bool = False

async def create_browser_context(headless: bool = headless, mobile: bool = mobile):
    # Work which will be done by Worker.py
    pool = AccountsPool(db_file=os.path.join(get_home_dir_path(), "db", "accounts.db"))
    await pool.release_account(identifier=None)
    account: Account = await pool.get(identifier="+12192636156")

    try:
        async with BrowserSession(
            account=account,
            pool=pool,
            headless=headless,
            mobile=mobile
        ) as browser_session:
            await browser_session.save_cookies()
    except FailedLoginError as e:
        print("Failed to login !!")
        print(f"Error: {e}")

    await pool.release_account(identifier=account.identifier)

if __name__ == "__main__":
    asyncio.run(create_browser_context())