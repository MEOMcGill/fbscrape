"""
Path B investigation: scrape one previously-scraped handle with full network
capture turned on.

Picks a handle that we've already scraped successfully (so we know it works
and produces a manageable number of posts), runs the existing scrape pipeline,
and dumps:

  - posts.json          — the ScrapingResult (same shape as production output)
  - network_*.jsonl     — every XHR + non-XHR response captured during the
                          session, dumped on BrowserSession.close().

The capture is keyed on two env vars set at the top of this file BEFORE any
fbscrape import (so BrowserSession.close() and ResponseInterceptor pick them up
when they fire later):

  - FB_NETWORK_CAPTURE_DIR : where to dump the JSONL capture file.
  - FB_NETWORK_CAPTURE_ALL=1: capture every response (CSS / JS / images / fonts
                              etc. — body bytes are skipped for binary types,
                              metadata + size are always recorded).

Output goes to: data/hybrid/<handle>_<UTC-timestamp>/

Pre-flight: all the hybrid TEMP code in response.py and
browser_session.py must still be present (search `# TEMP:`). It is — but if
the investigation has been concluded and that code stripped, this script will
silently produce no capture files.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# --- Target ---------------------------------------------------------------
# Picked from data/posts/2024-10-01_2026-04-17 — handle scraped successfully
# and produced 151 posts (~3-4x the depth of the JohnYakabuskiMPP capture).
# We're using this one to verify the pagination + per-cycle XHR patterns
# observed at 42 posts also hold at ~50 paginations.
TARGET_HANDLE = "FilomenaTassi"
TARGET_ID = 3155

START_DATE = "2024-10-01"
END_DATE = "2026-04-17"

# --- Output ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUTPUT_DIR = REPO_ROOT / "data" / "hybrid" / f"{TARGET_HANDLE}_{TIMESTAMP}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Capture toggles (must be set before fbscrape import) -----------------
os.environ["FB_NETWORK_CAPTURE_DIR"] = str(OUTPUT_DIR)
os.environ["FB_NETWORK_CAPTURE_ALL"] = "1"

# --- Imports (after env vars set) -----------------------------------------
from fbscrape.accounts_pool import AccountsPool  # noqa: E402
from fbscrape.logger import set_log_level  # noqa: E402
from fbscrape.scraper import FacebookScraper  # noqa: E402
from fbscrape.utils import get_home_dir_path  # noqa: E402

DB_PATH = os.path.join(get_home_dir_path(), "db", "accounts.db")


async def main() -> None:
    set_log_level("DEBUG")

    print(f"[path_b] target:   {TARGET_HANDLE} (ID={TARGET_ID})")
    print(f"[path_b] dates:    {START_DATE} → {END_DATE}")
    print(f"[path_b] output:   {OUTPUT_DIR}")
    print(f"[path_b] capture:  ALL responses (env FB_NETWORK_CAPTURE_ALL=1)")
    print()

    pool = AccountsPool(DB_PATH)
    async with FacebookScraper(
        db=pool,
        max_browser_sessions=1,
        scroll_threshold=500,
        headless=False,
        mobile=False,
    ) as scraper:
        result = await scraper.user_timeline(
            handle=TARGET_HANDLE,
            start_date=START_DATE,
            end_date=END_DATE,
        )

    print()
    print(f"[path_b] result:   {result.result}")
    print(f"[path_b] posts:    {len(result.posts)}")
    print(f"[path_b] elapsed:  {result.time_taken}")

    posts_path = OUTPUT_DIR / "posts.json"
    with open(posts_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    print(f"[path_b] saved:    {posts_path}")

    # The network capture file is written automatically by BrowserSession.close()
    # via the FB_NETWORK_CAPTURE_DIR env var. Surface what landed for clarity.
    capture_files = sorted(OUTPUT_DIR.glob("network_*.jsonl"))
    if capture_files:
        for cf in capture_files:
            size_mb = cf.stat().st_size / (1024 * 1024)
            print(f"[path_b] capture:  {cf.name} ({size_mb:.1f} MB)")
    else:
        print("[path_b] WARNING:  no network_*.jsonl in output dir — capture instrumentation may have been removed.")


if __name__ == "__main__":
    asyncio.run(main())
