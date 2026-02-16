from fbscrape.worker import Worker
from fbscrape.accounts_pool import AccountsPool
from fbscrape.models import Query
from fbscrape.utils import get_home_dir_path, recursively_get_dict_value
from fbscrape.exceptions import FailedLoginError

import asyncio
import os
from typing import Tuple
import datetime

BASE_URL = "https://www.facebook.com"
mobile: bool = False
headless: bool = False

handle: str = "patrick.provost.79"
start_date: str = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
end_date: str = datetime.date.today().strftime("%Y-%m-%d")

async def test_worker():
    queue = asyncio.Queue()
    await queue.put(
        Query(
            endpoint="UserTimeline",
            query={"handle": handle, "start_date": start_date, "end_date": end_date},
            params={}
        )
    )
    pool = AccountsPool(db_file=os.path.join(get_home_dir_path(), "db", "accounts.db"))
    async with Worker(id="worker-1", pool=pool) as worker:
        await worker.run(task_queue=queue)
        pass
    pass

if __name__ == "__main__":
    asyncio.run(test_worker())