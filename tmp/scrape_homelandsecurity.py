"""
Equivalent of:
    fbscrape scrape user-timeline homelandsecurity \
        --start-date 2025-01-20 --end-date 2026-04-17 \
        --max-sessions 1 --log-level DEBUG
"""

import asyncio
import os

from fbscrape.accounts_pool import AccountsPool
from fbscrape.logger import set_log_level
from fbscrape.models import ScrapingResult
from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather, get_home_dir_path

HANDLES = ["homelandsecurity"]
START_DATE = "2025-01-20"
END_DATE = "2026-04-17"
MAX_SESSIONS = 1
SCROLL_THRESHOLD = 500
HEADLESS = False
MOBILE = False
LOG_LEVEL = "DEBUG"


async def main():
    set_log_level(LOG_LEVEL)

    db_path = os.path.join(get_home_dir_path(), "db", "accounts.db")
    output_dir = os.path.join(
        get_home_dir_path(), "data", "posts", f"{START_DATE}_{END_DATE}"
    )
    os.makedirs(output_dir, exist_ok=True)

    pool = AccountsPool(db_path)
    async with FacebookScraper(
        db=pool,
        max_browser_sessions=MAX_SESSIONS,
        scroll_threshold=SCROLL_THRESHOLD,
        headless=HEADLESS,
        mobile=MOBILE,
    ) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=h, start_date=START_DATE, end_date=END_DATE)
            for h in HANDLES
        ):
            data: ScrapingResult = result
            handle = data.query.query.get("handle")
            print(f"{handle}: {data.result} ({len(data.posts)} posts, {data.time_taken})")

            filename = (
                f"{handle.replace('.', '_')}"
                f"_{data.query.endpoint}"
                f"_{START_DATE}_{END_DATE}.json"
            )
            data.save(os.path.join(output_dir, filename))

    print(f"\nResults saved to: {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
