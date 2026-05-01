"""
Path B investigation: batch capture across 15 handles, 3 concurrent sessions.

Verifies that the patterns observed on single-handle captures (single workhorse
query, stable auth tokens, standard cursor pagination, ~30s /ajax/bnzai
heartbeat) hold across many independent sessions / accounts / target profiles.

Output: data/hybrid/batch_<UTC-timestamp>/
  - posts_<handle>.json                          one per target
  - network_<close_ts>_<account_id>.jsonl        one per browser session (auto-saved)
  - manifest.json                                handle → posts file + scrape result

Run:
    python tmp/hybrid/capture_batch.py

Notes:
  - 15 targets × ~3 minutes/scrape ÷ 3 concurrent ≈ 15 minutes wall-clock.
  - Capture files are large (~80MB each at ~150 posts) — total ~1.2GB on disk
    after this run. All under data/ which is gitignored.
  - Network captures are emitted per-session, not per-handle. To map captures
    back to handles, use the timestamp-ordering recorded in manifest.json or
    parse the URL of the first navigation request in each capture file.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# --- Targets --------------------------------------------------------------
# Picked from data/posts/2024-10-01_2026-04-17 — handles that previously
# scraped successfully and produced ~150 posts (range 127-178). Same depth
# regime as the FilomenaTassi single-handle capture, just spread across
# 15 different profiles for variance check.
TARGETS = [
    {"handle": "BeckyDruhanNS",        "id": 2502,  "prior_posts": 141},
    {"handle": "JessDixonMPP",         "id": 2585,  "prior_posts": 149},
    {"handle": "AndrewScheerMP",       "id": 3129,  "prior_posts": 153},
    {"handle": "kellyblockmp",         "id": 2897,  "prior_posts": 156},
    {"handle": "jennredmondpei",       "id": 2675,  "prior_posts": 138},
    {"handle": "claireratteeskeena",   "id": 9562,  "prior_posts": 162},
    {"handle": "bouazziofficiel",      "id": 2752,  "prior_posts": 135},
    {"handle": "ToddDohertyMP",        "id": 2943,  "prior_posts": 164},
    {"handle": "dan_grice",            "id": 11898, "prior_posts": 131},
    {"handle": "PatriciaArab",         "id": 2486,  "prior_posts": 131},
    {"handle": "SoniaLeBelCAQ",        "id": 2700,  "prior_posts": 166},
    {"handle": "surmakinga",           "id": 2603,  "prior_posts": 168},
    {"handle": "mpkevinlamoureux",     "id": 3033,  "prior_posts": 127},
    {"handle": "PCYarmouth",           "id": 2483,  "prior_posts": 170},
    {"handle": "peter_tabuns",         "id": 2586,  "prior_posts": 178},
]

START_DATE = "2024-10-01"
END_DATE = "2026-04-17"
MAX_SESSIONS = 3

# --- Output ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUTPUT_DIR = REPO_ROOT / "data" / "hybrid" / f"batch_{TIMESTAMP}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# --- Capture toggles (must be set before fbscrape import) -----------------
os.environ["FB_NETWORK_CAPTURE_DIR"] = str(OUTPUT_DIR)
os.environ["FB_NETWORK_CAPTURE_ALL"] = "1"

# --- Imports (after env vars set) -----------------------------------------
from fbscrape.accounts_pool import AccountsPool  # noqa: E402
from fbscrape.logger import set_log_level  # noqa: E402
from fbscrape.models import ScrapingResult  # noqa: E402
from fbscrape.scraper import FacebookScraper  # noqa: E402
from fbscrape.utils import gather, get_home_dir_path  # noqa: E402

DB_PATH = os.path.join(get_home_dir_path(), "db", "accounts.db")


async def main() -> None:
    set_log_level("DEBUG")

    print(f"[path_b/batch] targets:  {len(TARGETS)} handles")
    print(f"[path_b/batch] dates:    {START_DATE} → {END_DATE}")
    print(f"[path_b/batch] parallel: {MAX_SESSIONS} concurrent sessions")
    print(f"[path_b/batch] output:   {OUTPUT_DIR}")
    print(f"[path_b/batch] capture:  ALL responses (env FB_NETWORK_CAPTURE_ALL=1)")
    for i, t in enumerate(TARGETS):
        print(f"  {i+1:>2}. {t['handle']:<25} (ID={t['id']}, prior posts={t['prior_posts']})")
    print()

    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "start_date": START_DATE,
        "end_date": END_DATE,
        "max_sessions": MAX_SESSIONS,
        "targets": TARGETS,
        "results": [],
    }

    pool = AccountsPool(DB_PATH)
    async with FacebookScraper(
        db=pool,
        max_browser_sessions=MAX_SESSIONS,
        scroll_threshold=500,
        headless=False,
        mobile=False,
    ) as scraper:
        async for result in gather(
            scraper.user_timeline(handle=t["handle"], start_date=START_DATE, end_date=END_DATE)
            for t in TARGETS
        ):
            data: ScrapingResult = result
            handle = data.query.query.get("handle")

            print(f"[{handle}]: {data.result}  posts={len(data.posts)}  elapsed={data.time_taken}")

            posts_path = OUTPUT_DIR / f"posts_{handle}.json"
            with open(posts_path, "w", encoding="utf-8") as f:
                json.dump(data.to_dict(), f, indent=2, default=str)

            manifest["results"].append({
                "handle": handle,
                "result": data.result,
                "posts": len(data.posts),
                "time_taken_seconds": data.time_taken.total_seconds() if data.time_taken else None,
                "time_started": data.time_started.isoformat() if data.time_started else None,
                "posts_file": str(posts_path.relative_to(REPO_ROOT)),
            })

    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Surface what landed for clarity.
    capture_files = sorted(OUTPUT_DIR.glob("network_*.jsonl"))
    manifest["capture_files"] = [str(p.relative_to(REPO_ROOT)) for p in capture_files]

    manifest_path = OUTPUT_DIR / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print()
    print(f"[path_b/batch] saved manifest: {manifest_path}")
    print(f"[path_b/batch] {len(capture_files)} network capture files in {OUTPUT_DIR}")
    if capture_files:
        for cf in capture_files:
            size_mb = cf.stat().st_size / (1024 * 1024)
            print(f"  {cf.name} ({size_mb:.1f} MB)")
    else:
        print("[path_b/batch] WARNING: no network_*.jsonl produced — capture instrumentation may be off.")


if __name__ == "__main__":
    asyncio.run(main())
