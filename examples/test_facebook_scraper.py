from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather, get_home_dir_path
from fbscrape.models import ScrapingResult
from fbscrape.logger import set_log_level

import asyncio
import datetime
import os

set_log_level("INFO")

mobile: bool = False
headless: bool = False
max_browser_sessions: int = 2

handles: list[str] = [
    "AndrewScheerMP",
    "patrick.provost.79",
    "MarkJCarney2025",
    "PierrePoilievreMP",
    "pspp.quebec",
    "ambermac",
    "RomanBaberMP",
    "DanielleSmithAB",
    "AnitaOakville",
    "michellerempelgarner",
    "melissalantsman",
    "larrybrockmp",
    "dallasbrodievancouver",
    "AndrewLawtonMedia",
    "AaronGunn.ca",
    "WabKinew",
    "DougFord",
    "jamijivani"
]

start_date: str = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
start_date: str = "2025-06-01"
end_date: str = datetime.date.today().strftime("%Y-%m-%d")

async def scrape_scraper():
    async with FacebookScraper(headless=headless, mobile=mobile, max_browser_sessions=max_browser_sessions) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=h, start_date=start_date, end_date=end_date)
            for h in handles
        ):
            data: ScrapingResult = result
            print(
                f"Query: {data.query}\n"
                f"Success: {data.result}\n"
                f"Posts: {len(data.posts)}\n"
                f"Time taken: {data.time_taken}"
            )

            path_folder = os.path.join(get_home_dir_path(), "data", "posts", f"{data.query.start_date.date()}_{data.query.end_date.date()}")
            os.makedirs(path_folder, exist_ok=True)
            data.save(
                os.path.join(
                    path_folder,
                    f"{data.query.query.get('handle').replace('.', '_')}"
                    f"_{data.query.endpoint}"
                    f"_{data.query.start_date.date()}"
                    f"_{data.query.end_date.date()}"
                    f".json"
                )
            )
    pass

if __name__ == "__main__":
    asyncio.run(scrape_scraper())