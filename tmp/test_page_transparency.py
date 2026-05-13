"""
Smoke test for the PageTransparency endpoint.

Equivalent of:
    fbscrape scrape page-transparency 899800046546098 ... \
        --max-sessions 2 --log-level INFO

Runs scraper.page_transparency() against the TARGETS list in parallel (up
to MAX_SESSIONS at a time), prints a one-line summary per page, and saves
the raw ScrapingResult JSON to OUT_DIR.

TARGETS is a list of `(handle, page_id)` tuples. `page_id` is required
(numeric FB page id, sent as `variables.pageID`). `handle` is optional —
pass `None` to skip it; bootstrap navigation then goes straight to
`https://www.facebook.com/<page_id>/`. Find page_ids in DevTools (look
for `pageID` in any GraphQL request's `variables` while on the target page).

    python tmp/test_page_transparency.py
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from fbscrape.accounts_pool import AccountsPool
from fbscrape.logger import set_log_level
from fbscrape.models import ScrapingResult
from fbscrape.response import FacebookGraphQLParser
from fbscrape.scraper import FacebookScraper
from fbscrape.utils import gather, get_home_dir_path


# (handle, page_id) targets. `handle` may be None when only the numeric
# page_id is known — habsfanhub is the working baseline from the initial
# capture in tmp/endpoint_additions/PageTransparency/.
TARGETS: list[str] = [
    "899800046546098",
    "100044331674441",
    #"61577505662345",
    #"61578036082052",
    #"61586527284237",
    #"61586198466382",
    #"61584398009592",
    #"61583786342055",
    #"61578071465060",
]

OUT_DIR = os.path.join(get_home_dir_path(), "data", "page_transparency")
DB_PATH = os.path.join(get_home_dir_path(), "db", "accounts.db")
MAX_SESSIONS = 1
HEADLESS = False
LOG_LEVEL = "INFO"


async def main() -> None:
    set_log_level(LOG_LEVEL)
    os.makedirs(OUT_DIR, exist_ok=True)

    pool = AccountsPool(DB_PATH)
    parser = FacebookGraphQLParser()

    async with FacebookScraper(
        db=pool,
        max_browser_sessions=MAX_SESSIONS,
        headless=HEADLESS,
    ) as scraper:
        async for result in gather(
            scraper.page_transparency(page_id=pid)
            for pid in TARGETS
        ):
            r: ScrapingResult = result
            page_id = r.query.query["page_id"]
            handle = r.query.query.get("handle")
            label = handle or page_id
            n = len(r.data)
            page_name = r.data[0].get("name") if r.data else "—"
            print(
                f"{label:<25} page_id={page_id:<20} "
                f"result={r.result:<22} records={n} "
                f"name={page_name!r}  ({r.time_taken})"
            )

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            base = f"{label.replace('.', '_')}_pagetransparency_{ts}"

            r.save(os.path.join(OUT_DIR, f"{base}.json"), compress=False)

    print(f"\nOutputs in: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
