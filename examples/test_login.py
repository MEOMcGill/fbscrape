import asyncio
from typing import Tuple

from fbscrape.browser_session import BrowserManager, BrowserContext, PageController
from fbscrape.session import FacebookAuth
from fbscrape.utils import get_home_dir_path
import os

from test_browser import create_browser_context, mobile

async def test_login(username: str, password: str,
               browser_manager: BrowserManager,
               page_controller: PageController
               ):
    auth = FacebookAuth(
        username=username,
        password=password,
        auth_json_path=os.path.join(get_home_dir_path(), "auth", f"{username}.json")
    )

    await auth.cookie_login(browser_manager.context)
    await page_controller.page.reload()

    if await auth.need_to_log_in(page=page_controller.page):
        await auth.manual_login(
            page=page_controller.page,
            mobile=mobile
        )

    if await auth.need_to_log_in(page=page_controller.page):
        raise Exception(
            f"login failed for {username} with password {password}"
        )
    else:
        await auth.save_session_state(context=browser_manager.context)
        print(f"login successful for {username} and saved session state to {auth.auth_json_path}")


async def main(username: str, password: str):
    browser_manager, page_controller = await create_browser_context()
    await test_login(
        username=username,
        password=password,
        browser_manager=browser_manager,
        page_controller=page_controller
    )


if __name__ == "__main__":
    asyncio.run(main(username="markandrewjohnson89@hotmail.com",password="watershipCross2#6"))