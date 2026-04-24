"""
Equivalent of:
    fbscrape scrape user-timeline homelandsecurity \
        --start-date 2025-01-20 --end-date 2026-04-17 \
        --max-sessions 1 --log-level DEBUG
"""

import asyncio
import configparser
import os
from urllib.parse import urljoin
import json
import requests

from fbscrape.accounts_pool import AccountsPool
from fbscrape.logger import set_log_level
from fbscrape.models import ScrapingResult
from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather, get_home_dir_path

MEO_CONFIG_PATH = os.path.join(
    get_home_dir_path(), "meo_facebook_scraper_config.cfg"
)


def fetch_meo_facebook_seeds(only_actives: bool = True) -> list[dict]:
    """
    Fetch MEO's Facebook seed list from the MEO API.

    Credentials and base URL are read from the `[meo-api-credentials]` section
    of `meo_facebook_scraper_config.cfg`.

    Returns the raw seed records (list of dicts with ID, SeedName, Handle,
    MainType, Collection, HandleStatus, InfoStartDate, InfoEndDate, etc.).
    """
    cfg = configparser.ConfigParser()
    cfg.read(MEO_CONFIG_PATH)
    username = cfg["meo-api-credentials"]["username"]
    password = cfg["meo-api-credentials"]["password"]
    base_url = cfg["meo-api-credentials"]["domain"]

    login = requests.post(
        urljoin(base_url, "/meologin"),
        params={"username": username, "password": password},
    )
    login.raise_for_status()
    token = login.json()["access_token"]

    resp = requests.get(
        urljoin(base_url, "/phh/seedlist"),
        params={"query": "Platform:facebook", "only_actives": only_actives},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()


START_DATE = "2026-04-01"
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
    # Filenames look like "{ID}_{handle}_UserTimeline_{START}_{END}.json".
    # Dedup on (ID, handle) parsed from the filename prefix.
    already_scraped = set()
    suffix = f"_UserTimeline_{START_DATE}_{END_DATE}.json"
    for f in os.listdir(output_dir):
        if not f.endswith(suffix):
            continue
        prefix = f[: -len(suffix)]
        id_str, _, handle = prefix.partition("_")
        if id_str.isdigit() and handle:
            already_scraped.add((int(id_str), handle))
    SEEDS = fetch_meo_facebook_seeds()
    SEEDS = [
        s for s in SEEDS
        if s["MainType"] == "politician"
        and not s["Handle"].isdigit()
        and (s["ID"], s["Handle"].replace(".", "_")) not in already_scraped
    ]

    pool = AccountsPool(db_path)
    async with FacebookScraper(
        db=pool,
        max_browser_sessions=MAX_SESSIONS,
        scroll_threshold=SCROLL_THRESHOLD,
        headless=HEADLESS,
        mobile=MOBILE,
    ) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=h["Handle"], start_date=START_DATE, end_date=END_DATE)
            for h in SEEDS
        ):
            data: ScrapingResult = result
            handle = data.query.query.get("handle")

            print(f"{handle}: {data.result} ({len(data.posts)} posts, {data.time_taken})")

            data_to_save = data.to_dict()
            by_handle = {s["Handle"]: s for s in SEEDS}
            seed_info = by_handle[handle]
            data_to_save["seed_info"] = seed_info

            filename = (
                f"{seed_info['ID']}"
                f"_{handle.replace('.', '_')}"
                f"_{data.query.endpoint}"
                f"_{START_DATE}_{END_DATE}.json"
            )

            with open(os.path.join(output_dir, filename), "w") as f:
                json.dump(data_to_save, f, indent=2)
            #data.save(os.path.join(output_dir, filename))

    print(f"\nResults saved to: {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
