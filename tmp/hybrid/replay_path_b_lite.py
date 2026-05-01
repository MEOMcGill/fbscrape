"""
Phase 3 (Path B-lite): replay GraphQL requests via `page.request` — i.e. from
inside the live browser session, sharing its cookie jar and TLS stack.

Why this approach:
  - Cookies, auth state, and IP all come from the live BrowserContext.
  - The page's JS keeps running, so /ajax/bnzai heartbeat and other ambient
    telemetry happens naturally.
  - We bypass scroll-driven pagination entirely, so the DOM never grows
    and the renderer never wedges.
  - The interesting questions ("does FB accept the replay?") become
    irrelevant because the request originates from the live session.

What we test here (every request stays close to real UI traffic shape:
count=3, all other variables cloned from the captured template):
  1. Walk A (unfiltered) — N paginations from the current cursor,
     no extra variables. Should yield ~3 × N most-recent posts. This is
     the direct equivalent of N scroll events, but with no DOM growth.
  2. Walk B (beforeTime=Jan 1 2025) — N paginations with the same shape
     plus beforeTime=1735689600. FB's own UI fires this shape when a user
     applies the date filter. All returned posts should be older than
     Jan 1 2025 — that's how we verify server-side date filtering works.

Default N is 5, so each walk yields ~15 posts at count=3 — same volume
as 5 scrolls' worth of pagination.

Run:
    # Use any available account, default target FilomenaTassi:
    python tmp/hybrid/replay_path_b_lite.py

    # Specify a particular account by email or phone (account identifier):
    python tmp/hybrid/replay_path_b_lite.py \\
        --account user@example.com

    # Custom target handle:
    python tmp/hybrid/replay_path_b_lite.py \\
        --account user@example.com --target JohnYakabuskiMPP

    # Just list available accounts and exit:
    python tmp/hybrid/replay_path_b_lite.py --list-accounts

Output goes to: data/hybrid/path_b_lite_<UTC-timestamp>/
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import numpy as np

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

# --- Defaults -------------------------------------------------------------
DEFAULT_TARGET_HANDLE = "FordNationDougFord"
REPO_ROOT = Path(__file__).resolve().parents[2]

# January 1, 2025 00:00:00 UTC, as Unix seconds.
BEFORE_TIME_JAN_1_2025 = 1735689600

# Don't capture network traffic for this run — we don't need it, and it
# would balloon the output for what's an active experiment.
os.environ.pop("FB_NETWORK_CAPTURE_DIR", None)
os.environ.pop("FB_NETWORK_CAPTURE_ALL", None)

# --- Imports (after env vars handled) -------------------------------------
from fbscrape.accounts_pool import AccountsPool  # noqa: E402
from fbscrape.browser_session import BrowserSession  # noqa: E402
from fbscrape.logger import set_log_level  # noqa: E402
from fbscrape.utils import get_home_dir_path  # noqa: E402

DB_PATH = os.path.join(get_home_dir_path(), "db", "accounts.db")
TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"

# Headers managed by Playwright / TLS layer — don't pass them through.
# Cookies in particular are handled by the BrowserContext.
HEADER_DROP = {
    "host", "content-length", "connection", "accept-encoding", "cookie",
}


def parse_form(post_data):
    if not post_data:
        return {}
    parsed = parse_qs(post_data, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def clean_headers(raw):
    out = {}
    for k, v in raw.items():
        if k.startswith(":"):
            continue
        if k.lower() in HEADER_DROP:
            continue
        out[k] = v
    return out


def parse_response(text):
    """Walk JSON or JSONL response, return dict with posts/end_cursor/errors/creation_times."""
    out = {
        "posts": 0,
        "end_cursor": None,
        "errors": [],
        "post_ids": [],
        "creation_times": [],   # unix-seconds (int) of each post's creation_time
        "first_doc": None,
    }
    docs = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            docs = []
            break
    if not docs:
        try:
            docs = [json.loads(text)]
        except json.JSONDecodeError:
            return out
    out["first_doc"] = docs[0] if docs else None

    post_ids = set()
    creation_times = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "post_id" and isinstance(v, str):
                    post_ids.add(v)
                elif k == "creation_time" and isinstance(v, (int, float)):
                    creation_times.append(int(v))
                elif k in ("end_cursor", "endCursor") and v and not out["end_cursor"]:
                    out["end_cursor"] = v
                elif k == "errors" and isinstance(v, list):
                    out["errors"].extend(v)
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    for d in docs:
        walk(d)
    out["posts"] = len(post_ids)
    out["post_ids"] = sorted(post_ids)
    out["creation_times"] = creation_times
    return out


def build_body(template_form, modifications):
    """Apply modifications to form body, return URL-encoded string."""
    body = dict(template_form)
    for k in modifications.get("drop", []):
        body.pop(k, None)
    overrides = modifications.get("variable_overrides")
    if overrides:
        try:
            variables = json.loads(body.get("variables", "{}"))
        except json.JSONDecodeError:
            variables = {}
        variables.update(overrides)
        body["variables"] = json.dumps(variables, separators=(",", ":"))
    return urlencode(body)


async def wait_for_template(session, timeout_seconds=20):
    """Wait until at least one ProfileCometTimelineFeedRefetchQuery has been
    intercepted. If none arrives within timeout, trigger one scroll to provoke
    pagination."""
    elapsed = 0.0
    interval = 0.5
    while elapsed < timeout_seconds:
        # Look in the in-memory capture for the workhorse query.
        for rec in session.response_interceptor.network_capture:
            req = rec.get("request") or {}
            headers = req.get("headers") or {}
            if headers.get("x-fb-friendly-name") == TARGET_FRIENDLY_NAME:
                return rec
            form = parse_form(req.get("post_data"))
            if form.get("fb_api_req_friendly_name") == TARGET_FRIENDLY_NAME:
                return rec
        await asyncio.sleep(interval)
        elapsed += interval
    return None


async def fire_via_page_request(session, headers, body, label):
    """Fire one POST via page.request and return summary dict."""
    print(f"  → {label}")
    try:
        response = await session.page.request.post(
            GRAPHQL_URL,
            headers=headers,
            data=body,
            timeout=30000,
        )
    except Exception as e:
        return {"label": label, "error": f"page.request failed: {e}"}

    text = await response.text()
    parsed = parse_response(text)
    return {
        "label": label,
        "status": response.status,
        "body_size": len(text),
        "posts": parsed["posts"],
        "post_ids": parsed["post_ids"],
        "creation_times": parsed["creation_times"],
        "end_cursor": parsed["end_cursor"],
        "n_errors": len(parsed["errors"]),
        "errors": parsed["errors"][:3],
        "body_excerpt": text[:300],
    }


def fmt_unix_ts(ts):
    """Format a unix-seconds timestamp as 'YYYY-MM-DD HH:MM UTC'."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def organic_scroll_burst(session):
    """Trigger a small burst of real scrolls to mimic an intermittent reader.

    Pure page.request traffic without any scroll events would be a unique
    pattern (no real user pages forward through GraphQL data without ever
    scrolling). Sprinkling a few scrolls between page.request bursts makes
    the session pattern look like a normal scroller who occasionally
    double-checks a few feeds.

    Each burst: 2–5 scrolls, each followed by a normal-distribution sleep
    (mean 2.5s, stdev 0.5s, abs() to clamp negatives).
    """
    n_scrolls = int(np.random.randint(2, 6))  # 2..5 inclusive
    print(f"  [organic-scroll burst: {n_scrolls} scroll(s)]")
    for j in range(n_scrolls):
        try:
            await session.page.evaluate("window.scrollBy(0, window.innerHeight)")
        except Exception as e:
            print(f"    scroll {j+1}/{n_scrolls} failed: {e}")
            break
        await asyncio.sleep(abs(np.random.normal(2.5, 0.3)))


async def run_paginated_walk(session, template, n_pages, label,
                             extra_variables=None, scroll_burst_every=10):
    """Drive N paginations via page.request, with periodic organic scroll bursts.

    Args:
        session: live BrowserSession (for page.request)
        template: captured ProfileCometTimelineFeedRefetchQuery record (provides
                  body shape, headers, doc_id, fb_dtsg, lsd, etc.)
        n_pages: number of paginations to fire (each yields ~3 posts)
        label: name for this walk (printed and used in report)
        extra_variables: dict of additional variable overrides to apply on
                         every request — e.g. {"beforeTime": 1735689600} for
                         server-side date filtering. `cursor` always gets
                         overwritten by the walk loop and shouldn't be passed.
        scroll_burst_every: do an organic_scroll_burst after every N paginations
                            (default: 10). Not done after the final pagination.
    """
    template_req = template.get("request") or {}
    template_form = parse_form(template_req.get("post_data") or "")
    template_headers = clean_headers(template_req.get("headers") or {})
    extra_variables = extra_variables or {}

    print(f"\n{'='*70}\nPAGINATED WALK: {label}  "
          f"({n_pages} pages via page.request, "
          f"organic-scroll burst every {scroll_burst_every})\n{'='*70}")
    if extra_variables:
        print(f"  extra variables: {extra_variables}")

    # Start from whatever cursor the captured template has — that's the live
    # session's current pagination position. (For the filtered walk, FB will
    # still accept this and apply the filter on top.)
    cursor = None
    try:
        cursor = json.loads(template_form.get("variables", "{}")).get("cursor")
    except json.JSONDecodeError:
        pass

    all_post_ids: set = set()
    all_creation_times: list = []
    rows = []
    for i in range(n_pages):
        overrides = {**extra_variables, "cursor": cursor}
        body = build_body(template_form, {"variable_overrides": overrides})
        t0 = datetime.now(timezone.utc)
        r = await fire_via_page_request(session, template_headers, body, f"page {i+1}/{n_pages}")
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        if "error" in r:
            print(f"  page {i+1}: ERROR {r['error']}")
            break
        new_ids = set(r["post_ids"]) - all_post_ids
        all_post_ids.update(r["post_ids"])
        all_creation_times.extend(r["creation_times"])
        oldest = min(r["creation_times"]) if r["creation_times"] else None
        newest = max(r["creation_times"]) if r["creation_times"] else None
        rows.append({
            "page": i + 1,
            "status": r["status"],
            "posts": r["posts"],
            "new_posts": len(new_ids),
            "elapsed_seconds": round(elapsed, 2),
            "oldest_creation": oldest,
            "newest_creation": newest,
            "end_cursor": (r["end_cursor"] or "")[:30],
        })
        print(f"  page {i+1}: status={r['status']}, posts={r['posts']}, new={len(new_ids)}, "
              f"elapsed={elapsed:.2f}s, "
              f"newest={fmt_unix_ts(newest)}, oldest={fmt_unix_ts(oldest)}")
        if not r["end_cursor"]:
            print("  no end_cursor — stopping")
            break
        cursor = r["end_cursor"]

        # Periodic organic scroll burst — keep the session pattern looking
        # like an intermittent reader rather than pure GraphQL traffic.
        if (i + 1) % scroll_burst_every == 0 and i + 1 < n_pages:
            await organic_scroll_burst(session)

        await asyncio.sleep(abs(np.random.normal(2.5, 0.5)))

    walk_oldest = min(all_creation_times) if all_creation_times else None
    walk_newest = max(all_creation_times) if all_creation_times else None
    print(f"\n  Total unique post_ids: {len(all_post_ids)} across {len(rows)} requests")
    print(f"  Date range:   newest={fmt_unix_ts(walk_newest)},  oldest={fmt_unix_ts(walk_oldest)}")
    return {
        "label": label,
        "extra_variables": extra_variables,
        "n_pages_requested": n_pages,
        "n_pages_completed": len(rows),
        "unique_post_ids": len(all_post_ids),
        "post_ids": sorted(all_post_ids),
        "newest_creation": walk_newest,
        "oldest_creation": walk_oldest,
        "pages": rows,
    }


async def list_accounts(pool):
    """Print all accounts in the pool with their identifiers and active status."""
    accounts = await pool.get(None)
    if not accounts:
        print("(no accounts in pool)")
        return
    print(f"{'identifier':<45} {'active':<7} {'in_use':<7} {'last_used':<22}")
    print(f"{'-'*45} {'-'*7} {'-'*7} {'-'*22}")
    for a in accounts:
        last = str(a.last_used)[:22] if a.last_used else "-"
        print(f"{a.identifier[:45]:<45} {str(bool(a.active)):<7} {str(bool(a.in_use)):<7} {last:<22}")


async def resolve_account(pool, identifier):
    """If identifier given, fetch that specific account. Otherwise fall back
    to get_available() (which also marks the account in_use)."""
    if identifier:
        try:
            account = await pool.get(identifier)
        except ValueError as e:
            print(f"[path_b_lite] {e}", file=sys.stderr)
            sys.exit(1)
        if not account.active:
            print(f"[path_b_lite] WARNING: account {identifier} is marked inactive: "
                  f"{account.error_msg or '(no error_msg)'}")
        return account, False  # didn't auto-lock
    # No identifier — pick whatever's available (auto-locks via in_use=True).
    account = await pool.get_available()
    if not account:
        print("[path_b_lite] no available account!", file=sys.stderr)
        sys.exit(1)
    return account, True  # we acquired the lock; need to release on exit


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", default=None,
                    help="Account identifier (email or phone) to use. "
                         "If omitted, picks any available account.")
    ap.add_argument("--target", default=DEFAULT_TARGET_HANDLE,
                    help=f"Facebook handle to scrape (default: {DEFAULT_TARGET_HANDLE})")
    ap.add_argument("--list-accounts", action="store_true",
                    help="List all accounts in the pool and exit.")
    ap.add_argument("--n-pages", type=int, default=5,
                    help="Paginations to fire in EACH walk (default: 5 — yields ~15 posts at count=3)")
    args = ap.parse_args()

    set_log_level("INFO")
    pool = AccountsPool(DB_PATH)

    if args.list_accounts:
        await list_accounts(pool)
        return

    target_handle = args.target
    profile_url = f"https://www.facebook.com/{target_handle}/"

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = REPO_ROOT / "data" / "hybrid" / f"path_b_lite_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[path_b_lite] target:   {target_handle}")
    print(f"[path_b_lite] output:   {output_dir}")
    print()

    account, acquired_lock = await resolve_account(pool, args.account)
    print(f"[path_b_lite] account:  {account.display_name}  "
          f"(active={bool(account.active)}, "
          f"{'auto-acquired' if acquired_lock else 'specified'})")

    try:
        async with BrowserSession(account=account, pool=pool, headless=False) as session:
            # Navigate to profile so the live session has page state.
            print(f"[path_b_lite] navigating to {profile_url}")
            await session.page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)  # let initial GraphQL queries fire

            # Find a captured ProfileCometTimelineFeedRefetchQuery to use as template.
            print(f"[path_b_lite] waiting for {TARGET_FRIENDLY_NAME} to fire naturally...")
            template = await wait_for_template(session, timeout_seconds=5)
            if template is None:
                # Initial nav didn't trigger pagination — provoke it with one scroll.
                print(f"[path_b_lite] no pagination yet — triggering one scroll")
                await session.page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                await asyncio.sleep(4)
                template = await wait_for_template(session, timeout_seconds=15)
            if template is None:
                print(f"[path_b_lite] FAILED to capture template request — aborting")
                return

            print(f"[path_b_lite] template captured. Running paginated walks...")

            # Save the template for debugging.
            with open(output_dir / "template.json", "w") as f:
                json.dump(template, f, indent=2, default=str)

            # Walk 1: no extra variables — paginate forward from current cursor.
            # Should yield ~15 most-recent posts (5 pages × ~3 posts).
            walk_unfiltered = await run_paginated_walk(
                session, template,
                n_pages=args.n_pages,
                label="unfiltered (count=3 per page, just like UI scrolling)",
            )

            await asyncio.sleep(3)

            # Walk 2: same shape, but with beforeTime=Jan 1 2025 — server-side
            # date filter. Posts returned should all be older than Jan 1 2025.
            walk_filtered = await run_paginated_walk(
                session, template,
                n_pages=args.n_pages,
                label=f"beforeTime=Jan 1 2025 ({BEFORE_TIME_JAN_1_2025})",
                extra_variables={"beforeTime": BEFORE_TIME_JAN_1_2025},
            )

            # Comparison summary.
            print(f"\n{'='*70}\nWALK COMPARISON\n{'='*70}")
            print(f"  {'walk':<35} {'pages':<7} {'posts':<7} {'newest':<22} {'oldest':<22}")
            print(f"  {'-'*35} {'-'*7} {'-'*7} {'-'*22} {'-'*22}")
            for w in (walk_unfiltered, walk_filtered):
                pages = f"{w['n_pages_completed']}/{w['n_pages_requested']}"
                print(f"  {w['label'][:35]:<35} {pages:<7} {w['unique_post_ids']:<7} "
                      f"{fmt_unix_ts(w['newest_creation'])[:22]:<22} {fmt_unix_ts(w['oldest_creation'])[:22]:<22}")

            # Verify the date filter actually took effect.
            if walk_filtered["newest_creation"]:
                if walk_filtered["newest_creation"] < BEFORE_TIME_JAN_1_2025:
                    print(f"\n  ✓ Filter worked: newest filtered post is "
                          f"{fmt_unix_ts(walk_filtered['newest_creation'])} (before Jan 1 2025).")
                else:
                    print(f"\n  ✗ Filter did NOT take effect: newest post is "
                          f"{fmt_unix_ts(walk_filtered['newest_creation'])} (not before Jan 1 2025).")

            # Save full results.
            report = {
                "target": target_handle,
                "account": account.identifier,
                "timestamp": timestamp,
                "template_timestamp": template.get("timestamp"),
                "before_time_jan_1_2025": BEFORE_TIME_JAN_1_2025,
                "walks": [walk_unfiltered, walk_filtered],
            }
            with open(output_dir / "report.json", "w") as f:
                json.dump(report, f, indent=2, default=str)
            print(f"\n[path_b_lite] saved report: {output_dir / 'report.json'}")
    finally:
        # If we auto-acquired the lock via get_available(), release it so the
        # account is reusable. (Specified accounts via --account weren't locked.)
        if acquired_lock:
            try:
                await pool.release_account(account.identifier)
                print(f"[path_b_lite] released account: {account.identifier}")
            except Exception as e:
                print(f"[path_b_lite] failed to release account: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
