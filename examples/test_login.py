from typing import Tuple

from fbscrape.browser import BrowserManager, BrowserContext, PageController
from fbscrape.session import FacebookAuth
from fbscrape.utils import get_home_dir_path
import os

from test_browser import create_browser_context, mobile

def test_login(username: str, password: str,
               browser_manager: BrowserManager,
               page_controller: PageController
               ):
    auth = FacebookAuth(
        username=username,
        password=password,
        auth_json_path=os.path.join(get_home_dir_path(), "auth", f"{username}.json")
    )

    auth.cookie_login(browser_manager.context)
    page_controller.page.reload()

    if auth.need_to_log_in(page=page_controller.page):
        auth.manual_login(
            page=page_controller.page,
            mobile=mobile
        )

    if auth.need_to_log_in(page=page_controller.page):
        raise Exception(
            f"login failed for {username} with password {password}"
        )
    else:
        auth.save_session_state(context=browser_manager.context)
        print(f"login successful for {username} and saved session state to {auth.auth_json_path}")


def main(username: str, password: str):
    browser_manager, page_controller = create_browser_context()
    test_login(
        username=username,
        password=password,
        browser_manager=browser_manager,
        page_controller=page_controller
    )


if __name__ == "__main__":
    main(
        username="markandrewjohnson89@hotmail.com",
        password="watershipCross2#6"
    )