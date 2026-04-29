"""
Equivalent of:
    fbscrape scrape user-timeline homelandsecurity \
        --start-date 2025-01-20 --end-date 2026-04-17 \
        --max-sessions 1 --log-level DEBUG
"""

import asyncio
import os
import json

from fbscrape.accounts_pool import AccountsPool
from fbscrape.logger import set_log_level
from fbscrape.models import ScrapingResult
from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather, get_home_dir_path

SEEDS_FILE = os.path.join(
    get_home_dir_path(), "data", "seeds", "facebook_politicians.jsonl"
)


def load_facebook_seeds(only_actives: bool = True) -> list[dict]:
    """
    Load Facebook seeds from the local JSONL file produced by the elastic
    export (see /Users/mikad/MEOMcGill/fbscrape/data/seeds/facebook_politicians.jsonl).

    The JSONL records have shape `{user_id, user_name, seed: {...}}` where
    `seed` carries the same fields the MEO API's /phh/seedlist returned.
    This function returns the flat `seed` dicts so downstream code that
    reads `s["ID"]`, `s["Handle"]`, `s["MainType"]`, etc. keeps working.

    When only_actives=True, keep only rows where both SeedStatus and
    HandleStatus equal 1 (matches the server-side `only_actives` filter).
    """
    seeds: list[dict] = []
    with open(SEEDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            seed = record.get("seed", {})
            if only_actives and (
                seed.get("SeedStatus") != 1 or seed.get("HandleStatus") != 1
            ):
                continue
            seeds.append(seed)
    return seeds


START_DATE = "2024-10-01"
END_DATE = "2026-04-17"
MAX_SESSIONS = 5
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
    SEEDS = load_facebook_seeds(only_actives=True)
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
