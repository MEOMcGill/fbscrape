from fbscrape.browser_session import BrowserSession
from fbscrape.account import Account
from fbscrape.accounts_pool import AccountsPool
from fbscrape.utils import get_home_dir_path, recursively_get_dict_value
from fbscrape.exceptions import FailedLoginError

import asyncio
import os
from typing import Tuple
import datetime

BASE_URL = "https://www.facebook.com"
mobile: bool = False
headless: bool = False

handle: str = "patrick.provost.79"
start_date: str = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
end_date: str = datetime.date.today().strftime("%Y-%m-%d")

# [(set(recursively_get_dict_value(i, '__isFeedUnit').values()).pop(), datetime.datetime.fromtimestamp(set(recursively_get_dict_value(i, 'timestamp.story.creation_time').values()).pop(), tz=zoneinfo.ZoneInfo("America/Montreal"))) for i in posts.posts[1:]]

async def scrape_home_page(
        handle: str = handle,
        start_date: str = start_date,
        end_date: str = end_date,
        headless: bool = headless,
        mobile: bool = mobile):

    # Work which will be done by Worker.py
    pool = AccountsPool(db_file=os.path.join(get_home_dir_path(), "db", "accounts.db"))
    await pool.release_account(identifier=None)
    account: Account = await pool.get_for_queue()

    try:
        async with BrowserSession(
            account=account,
            pool=pool,
            headless=headless,
            mobile=mobile
        ) as browser_session:
            result = await browser_session.user_timeline(handle=handle, start_date=start_date, end_date=end_date)
            pass

    except FailedLoginError as e:
        print("Failed to login !!")
        print(f"Error: {e}")
    except Exception as e:
        print(f"Error: {e}")

    await pool.release_account(identifier=account.identifier)

if __name__ == "__main__":
    asyncio.run(scrape_home_page())