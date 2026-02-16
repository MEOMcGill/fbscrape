from fbscrape.worker_pool import WorkerPool
from fbscrape.accounts_pool import AccountsPool
from fbscrape.models import Query
from fbscrape.utils import get_home_dir_path, recursively_get_dict_value
from fbscrape.exceptions import FailedLoginError

import asyncio
import os
from typing import Tuple
import datetime

mobile: bool = False
headless: bool = False

handles: list[str] = [
    "patrick.provost.79",
    "MarkJCarney2025",
    "PierrePoilievreMP",
    "pspp.quebec"
]
start_date: str = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
end_date: str = datetime.date.today().strftime("%Y-%m-%d")

pool = AccountsPool(db_file=os.path.join(get_home_dir_path(), "db", "accounts.db"))
max_workers: int = 2

async def test_workerpool():
    tasks = [
        Query(
            endpoint="UserTimeline",
            query={"handle": h, "start_date": start_date, "end_date": end_date},
            params={}
        )
        for h in handles
    ]
    async with WorkerPool(pool=pool, max_workers=max_workers) as worker_pool:
        futures = [await worker_pool.submit_task(q) for q in tasks]
        results = await asyncio.gather(*futures)
        pass
    pass

if __name__ == "__main__":
    asyncio.run(test_workerpool())