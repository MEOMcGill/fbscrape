from fbscrape.accounts_pool import AccountsPool
from fbscrape.account import Account
from fbscrape.utils import get_home_dir_path, parse_cookies
from fbscrape.logger import logger

import asyncio
import os

async def test_accounts_pool():

    pool = AccountsPool(
        db_file=os.path.join(get_home_dir_path(), "db", "accounts.db"),
    )

    # add from json file
    path_user_data = os.path.join(get_home_dir_path(), "auth", "markandrewjohnson89@hotmail.com.json")
    cookies: dict = parse_cookies(open(path_user_data, "r").read())

    email="markandrewjohnson89@hotmail.com"
    password="watershipCross2#6"
    await pool.add_account(
        email=email,
        password=password,
        cookies=cookies,
        username="Mark Johnson"
    )

    # get an account
    acc = await pool.get(email=None)
    if isinstance(acc, Account):
        logger.info(f"got account {acc.username}")
    elif isinstance(acc, list):
        for a in acc:
            logger.info(f"got account {a.username}")

    # get active accounts

if __name__ == "__main__":
    asyncio.run(test_accounts_pool())