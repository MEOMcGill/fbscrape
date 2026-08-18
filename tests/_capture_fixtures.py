"""One-shot fixture-capture script.

Run this once (after `pip install -e '.[dev]'` and with at least one active
account in db/accounts.db) to populate tests/fixtures/scraping_results/ with
fresh real scrapes the unit tests load.

    python tests/_capture_fixtures.py [--db PATH]

Targets are deliberately hard-coded so re-running yields drop-in replacements
for the same fixture file. All captures run headless. The script is named with
a leading underscore so pytest doesn't try to collect it as a test module.

NOT auto-captured by this script:

- `user_timeline_hybrid_variant_b.json` — hand-curated 6-record bundle pulled
  from raw saves to cover the "Variant B" summary-feedback shape (totals
  inside `adaptive_ufi_action_renderers[]` rather than at the top of the
  feedback dict). The `zuck` fixture is 100% Variant A and missed a real
  bug where ~60% of production responses ship Variant B. Records were picked
  for structural diversity (reel / video / album / photo / hashtags / 5
  distinct authors). If FB changes the Variant B shape and you need a fresh
  set, replace by pulling new records from any raw scrape and re-confirming
  via `tests/unit/test_flatten_user_timeline.py::test_variant_b_records_are_actually_variant_b`.
"""


import argparse
import asyncio
import json
import os
import sys

# Allow `python tests/_capture_fixtures.py` from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fbscrape import FacebookScraper  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "fixtures", "scraping_results")

# Fixture targets. The exact values matter only insofar as they're known
# stable public profiles/pages — substitute if any go dark.
TARGETS = {
    "user_timeline_hybrid": {
        "handle": "zuck",
        "start_date": "2025-01-01",
        "end_date": "2025-07-01",
        "mode": "hybrid",
    },
    "search_hybrid": {
        "query_text": "mark zuckerberg",
        "start_date": "2025-06-01",
        "end_date": "2025-07-01",
    },
    "group_timeline": {
        # "Children of da KoRn" — public group. Numeric ids also work as
        # `handle` — both forms resolve via /groups/<handle>/. Bounded with
        # max_posts so the capture terminates on a quiet group; the dates are
        # a wide window so it isn't empty.
        "handle": "392585550772135",
        "start_date": "2020-01-01",
        "end_date": None,
        "max_posts": 25,
    },
    "page_transparency": {
        # Meta's official page — stable, public, has full transparency record.
        "page_id": "20531316728",
    },
    "profile_authenticity": {
        # Monique and Jocelyne Lamoureux (LamoureuxTwins) — stable, public
        # profile with a full authenticity record. Confirmed via live
        # ProfileAbout capture; NOT Zuckerberg's id (his is "4").
        "user_id": "100044331674441",
    },
    "comments_list_hybrid": {
        # A public post on Mark Carney's page with top-level comments.
        # Substitute if it goes dark. The post_id is the pfbid form — both
        # numeric and pfbid forms work in /<handle>/posts/<post_id>/.
        "handle": "MarkJCarney2025",
        "post_id": (
            "pfbid02fqwzpi9P7cbpefNM1CUF1qzBGD5oPKR5PBwN62nQthxyiojY4uSJ6AYx85P2Nx4Gl"
        ),
        # Bounded so the capture doesn't spin forever on a viral post.
        "max_results": 30,
    },
    "post_detail": {
        # A public Alberta-politics group post (cited by Google's AI Overview).
        # Substitute if it goes dark. Group posts need is_group=True.
        "handle": "albertansunitedtostoptheucp",
        "post_id": "27209929835285847",
        "is_group": True,
    },
    "profile_info": {
        # Zuckerberg's profile — stable, public, fully-hydrated header.
        "handle": "zuck",
    },
    "profile_about": {
        # A public Page with populated contact/basic-info/links sections
        # (phone, email, address, hours, website). Substitute if it goes
        # dark. Personal profiles rarely expose these same section keys —
        # see docstring on Query.ENDPOINT_REGISTRY["ProfileAbout"].
        "handle": "61582991935083",
    },
    "group_info": {
        # "Children of da KoRn" — same public group as group_timeline.
        "handle": "392585550772135",
    },
    "group_about": {
        # Same group — for its description, rules, and admin facepile.
        "handle": "392585550772135",
    },
}


async def _capture_user_timeline(scraper: FacebookScraper, mode: str, spec: dict):
    return await scraper.user_timeline(
        spec["handle"], spec["start_date"], spec["end_date"], mode=mode,
    )


async def _capture_search(scraper: FacebookScraper, spec: dict):
    return await scraper.search(
        spec["query_text"], spec["start_date"], spec["end_date"],
    )


async def _capture_group_timeline(scraper: FacebookScraper, spec: dict):
    return await scraper.group_timeline(
        spec["handle"], spec["start_date"], spec["end_date"],
        max_posts=spec.get("max_posts", -1),
    )


async def _capture_page_transparency(scraper: FacebookScraper, spec: dict):
    return await scraper.page_transparency(spec["page_id"])


async def _capture_profile_authenticity(scraper: FacebookScraper, spec: dict):
    return await scraper.profile_authenticity(spec["user_id"])


async def _capture_comments_list(scraper: FacebookScraper, spec: dict):
    return await scraper.comments_list(
        spec["handle"], spec["post_id"], max_results=spec.get("max_results", -1),
    )


async def _capture_post_detail(scraper: FacebookScraper, spec: dict):
    return await scraper.post_detail(
        spec["handle"], spec["post_id"], is_group=spec.get("is_group", False),
    )


async def _capture_profile_info(scraper: FacebookScraper, spec: dict):
    return await scraper.profile_info(spec["handle"])


async def _capture_profile_about(scraper: FacebookScraper, spec: dict):
    return await scraper.profile_about(spec["handle"])


async def _capture_group_info(scraper: FacebookScraper, spec: dict):
    return await scraper.group_info(spec["handle"])


async def _capture_group_about(scraper: FacebookScraper, spec: dict):
    return await scraper.group_about(spec["handle"])


CAPTURERS = {
    "user_timeline_hybrid":  lambda s: _capture_user_timeline(s, "hybrid", TARGETS["user_timeline_hybrid"]),
    "search_hybrid":         lambda s: _capture_search(s, TARGETS["search_hybrid"]),
    "group_timeline": lambda s: _capture_group_timeline(s, TARGETS["group_timeline"]),
    "page_transparency":     lambda s: _capture_page_transparency(s, TARGETS["page_transparency"]),
    "profile_authenticity":  lambda s: _capture_profile_authenticity(s, TARGETS["profile_authenticity"]),
    "comments_list_hybrid":  lambda s: _capture_comments_list(s, TARGETS["comments_list_hybrid"]),
    "post_detail":           lambda s: _capture_post_detail(s, TARGETS["post_detail"]),
    "profile_info":          lambda s: _capture_profile_info(s, TARGETS["profile_info"]),
    "profile_about":         lambda s: _capture_profile_about(s, TARGETS["profile_about"]),
    "group_info":            lambda s: _capture_group_info(s, TARGETS["group_info"]),
    "group_about":           lambda s: _capture_group_about(s, TARGETS["group_about"]),
}


async def main(db: str, only: list[str] | None):
    os.makedirs(OUT_DIR, exist_ok=True)
    names = only or list(CAPTURERS.keys())

    async with FacebookScraper(db=db, max_browser_sessions=1, headless=True) as scraper:
        for name in names:
            path = os.path.join(OUT_DIR, f"{name}.json")
            print(f"[capture] {name} → {path}")
            try:
                result = await CAPTURERS[name](scraper)
            except Exception as e:
                print(f"  ERROR: {e!r}")
                continue
            # Fixtures are single-object JSON envelopes (loaded via json.load in
            # tests/conftest.py), NOT the .jsonl.gz that ScrapingResult.save()
            # writes. Serialize the envelope directly so the capture output is
            # exactly what the tests load.
            envelope = {
                "query": result.query.to_dict(),
                "result": result.result,
                "data": list(result.iter_posts()),
                "time_started": str(result.time_started) if result.time_started else None,
                "time_taken": str(result.time_taken) if result.time_taken else None,
            }
            with open(path, "w") as f:
                json.dump(envelope, f)
            print(f"  result={result.result!r} records={len(envelope['data'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Absolute path: fbscrape.db.DB internally prepends `<repo_root>/db/` to
    # whatever you pass — so a relative "db/accounts.db" would resolve to
    # `<repo_root>/db/db/accounts.db`. Pass an absolute path to bypass.
    default_db = os.path.abspath(
        os.path.join(os.path.dirname(HERE), "db", "accounts.db")
    )
    ap.add_argument("--db", default=default_db, help="Path to accounts.db")
    ap.add_argument("--only", nargs="*", default=None,
                    choices=list(CAPTURERS.keys()),
                    help="Only capture these fixtures (default: all)")
    args = ap.parse_args()
    asyncio.run(main(args.db, args.only))
