import os.path

from test_login import test_login
from test_browser import create_browser_context
from fbscrape.utils import get_config_path, save_jsonl, get_home_dir_path
from fbscrape.scraper import FacebookScraper
from fbscrape.response import ResponseInterceptor
import configparser
import datetime

path_data = os.path.join(
    get_home_dir_path(),
    "data",
    "posts"
)

handle: str = "MarkJCarney2025"
# handle: str = "changealberta"

def scrape_page():
    cfg = configparser.ConfigParser()
    cfg.read(get_config_path())

    browser_manager, page_controller = create_browser_context()

    test_login(
        username=cfg['facebook-login']['username-1'],
        password=cfg['facebook-login']['password-1'],
        browser_manager=browser_manager,
        page_controller=page_controller
    )

    response_interceptor = ResponseInterceptor()
    response_interceptor.setup_interception(page_controller.page)

    homepage_scraper = FacebookScraper(
        page_controller=page_controller,
        response_interceptor=response_interceptor,
    )

    start_date: str = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    end_date: str = datetime.date.today().strftime("%Y-%m-%d")

    posts = homepage_scraper.scrape_user_homepage(
        handle=handle,
        start_date=start_date,
        end_date=end_date,
    )

    save_jsonl(
        os.path.join(path_data, f"{handle}_posts_{start_date}_{end_date}.jsonl"),
        posts.posts
    )


if __name__ == "__main__":
    scrape_page()