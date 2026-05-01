"""
Phase 2 analyzer: examine the variables flow across all
ProfileCometTimelineFeedRefetchQuery requests in a capture.

Answers the question: do we understand pagination well enough to replay it?

For each request: extract input variables (cursor, count, afterTime,
beforeTime, id, plus any other interesting fields).
For each response: extract output cursor (end_cursor), has_next_page,
post count.
Then verify the cursor flow: does response[N].end_cursor == request[N+1].cursor?

Run:
    python tmp/hybrid/analyze_pagination.py \\
        data/hybrid/<dir>/network_*.jsonl

Optional: --friendly-name to inspect a different query (default is the
post-bearing one identified in Phase 1).
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

DEFAULT_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"

# Variables we care about per request. Anything else is bucketed as "other".
KEY_VARIABLES = ("cursor", "count", "afterTime", "beforeTime", "id", "feedLocation", "stream_count")


def parse_form(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    try:
        parsed = parse_qs(post_data, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}
    except Exception:
        return {}


def get_friendly_name(rec: dict) -> str | None:
    headers = (rec.get("request") or {}).get("headers") or {}
    name = headers.get("x-fb-friendly-name")
    if name:
        return name
    form = parse_form((rec.get("request") or {}).get("post_data"))
    return form.get("fb_api_req_friendly_name")


def parse_variables(post_data: str | None) -> dict:
    form = parse_form(post_data)
    raw = form.get("variables")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def parse_response_docs(body: str | None) -> list[dict]:
    """FB GraphQL can be JSON or JSONL (when @stream / @defer is used).
    Return a list of parsed top-level docs."""
    if not body:
        return []
    out: list[dict] = []
    # Try JSONL first (one doc per line).
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out = []
            break
    if out:
        return out
    # Fall back to single-doc JSON.
    try:
        return [json.loads(body)]
    except json.JSONDecodeError:
        return []


def walk(obj, target_keys: set[str]):
    """Yield (key, value) for any nested dict key in target_keys."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in target_keys:
                yield (k, v)
            yield from walk(v, target_keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item, target_keys)


def extract_response_info(body: str | None) -> tuple[str | None, bool | None, int]:
    """Return (end_cursor, has_next_page, unique_post_count) from response body."""
    docs = parse_response_docs(body)
    end_cursor: str | None = None
    has_next_page: bool | None = None
    post_ids: set = set()
    for doc in docs:
        for k, v in walk(doc, {"end_cursor", "endCursor", "has_next_page", "hasNextPage", "post_id"}):
            if k in ("end_cursor", "endCursor") and v and end_cursor is None:
                end_cursor = v
            elif k in ("has_next_page", "hasNextPage") and has_next_page is None:
                has_next_page = bool(v)
            elif k == "post_id" and isinstance(v, str):
                post_ids.add(v)
    return end_cursor, has_next_page, len(post_ids)


def trunc(s, n: int = 22) -> str:
    if s is None:
        return "<None>"
    s = str(s)
    if len(s) <= n:
        return s
    return s[: n // 2 - 1] + "…" + s[-(n // 2):]


def analyze(jsonl_paths: list[Path], friendly_name: str) -> list[dict]:
    records = []
    for p in jsonl_paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if get_friendly_name(rec) != friendly_name:
                    continue
                records.append(rec)

    records.sort(key=lambda r: r.get("timestamp", ""))

    rows = []
    for i, rec in enumerate(records):
        req = rec.get("request") or {}
        resp = rec.get("response") or {}
        variables = parse_variables(req.get("post_data"))
        end_cursor, has_next_page, num_posts = extract_response_info(resp.get("body"))
        rows.append({
            "idx": i,
            "ts": rec.get("timestamp", "?"),
            "variables": variables,
            "status": resp.get("status"),
            "num_posts": num_posts,
            "end_cursor": end_cursor,
            "has_next_page": has_next_page,
        })
    return rows


def print_table(rows: list[dict], friendly_name: str) -> None:
    if not rows:
        print(f"No records of friendly name '{friendly_name}' found.")
        return

    print(f"\n{len(rows)} records of '{friendly_name}':\n")

    hdr = f"{'idx':<4} {'time (HH:MM:SS)':<10} {'cursor_in':<22} {'cnt':<4} {'beforeT':<11} {'afterT':<7} {'st':<4} {'#posts':<7} {'next?':<6} {'end_cursor':<22}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        ts = r["ts"]
        # ISO 2026-04-29T19:36:22.901+00:00 → 19:36:22
        try:
            t = ts.split("T")[1].split(".")[0] if "T" in ts else "?"
        except Exception:
            t = "?"
        v = r["variables"]
        ci = trunc(v.get("cursor"))
        cnt = str(v.get("count")) if v.get("count") is not None else "-"
        bt = str(v.get("beforeTime")) if v.get("beforeTime") is not None else "-"
        at = str(v.get("afterTime")) if v.get("afterTime") is not None else "-"
        st = str(r["status"])
        np = str(r["num_posts"])
        nxt = str(r["has_next_page"]) if r["has_next_page"] is not None else "-"
        ec = trunc(r["end_cursor"])
        print(f"{r['idx']:<4} {t:<10} {ci:<22} {cnt:<4} {bt:<11} {at:<7} {st:<4} {np:<7} {nxt:<6} {ec:<22}")


def verify_cursor_flow(rows: list[dict]) -> None:
    print(f"\n{'='*70}\nCURSOR FLOW VERIFICATION\n{'='*70}")
    if len(rows) < 2:
        print("  Fewer than 2 records — no flow to verify.")
        return
    pairs = len(rows) - 1
    matches = 0
    mismatches = []
    for i in range(pairs):
        prev_end = rows[i]["end_cursor"]
        next_in = rows[i + 1]["variables"].get("cursor")
        if prev_end is not None and next_in is not None and prev_end == next_in:
            matches += 1
        else:
            mismatches.append((i, prev_end, next_in))

    print(f"  Pairs checked: {pairs}")
    print(f"  Matches:       {matches}")
    print(f"  Mismatches:    {len(mismatches)}")
    if mismatches:
        print(f"\n  Mismatched pairs (showing up to 5):")
        for i, prev_end, next_in in mismatches[:5]:
            print(f"    pair {i:>2}→{i+1:<2}  prev.end_cursor={trunc(prev_end, 30)}")
            print(f"               next.cursor_in ={trunc(next_in, 30)}")
        if len(mismatches) > 5:
            print(f"    ... and {len(mismatches) - 5} more")
    if matches == pairs and pairs > 0:
        print("\n  → FB uses standard cursor pagination: response.end_cursor flows directly")
        print("    into next request.cursor. Replay is straightforward.")
    elif matches > 0:
        print("\n  → Partial match. Some cursors are inherited cleanly, others aren't.")
        print("    Worth inspecting mismatches by hand — could indicate page-internal")
        print("    paginations, restarts, or batched queries.")


def print_variables_constancy(rows: list[dict]) -> None:
    print(f"\n{'='*70}\nVARIABLES CONSTANCY (across all paginated requests)\n{'='*70}")
    if not rows:
        return

    # Per-key uniqueness — what changes vs. what doesn't.
    all_keys: set[str] = set()
    for r in rows:
        all_keys.update(r["variables"].keys())

    print(f"  Total unique variable keys: {len(all_keys)}")
    print()
    print(f"  {'key':<55} unique  example value")
    print(f"  {'-'*55} {'------'} {'-'*30}")

    key_summaries = []
    for k in sorted(all_keys):
        values = [r["variables"].get(k) for r in rows]
        # Use string-coerced values for set-uniqueness so dicts/lists work.
        unique = sorted({json.dumps(v, sort_keys=True, default=str) for v in values})
        key_summaries.append((k, len(unique), values[0]))

    # First print fixed (1 unique) keys, then varying keys.
    for k, n, example in sorted(key_summaries, key=lambda x: (x[1], x[0])):
        ex = trunc(json.dumps(example, default=str), 35)
        marker = "" if n == 1 else f"  ← varies"
        print(f"  {k:<55} {n:>6}  {ex}{marker}")


def print_post_count_summary(rows: list[dict]) -> None:
    print(f"\n{'='*70}\nPOST YIELD\n{'='*70}")
    total = sum(r["num_posts"] for r in rows)
    if not rows:
        return
    print(f"  Total unique post_ids across all responses: {total}")
    print(f"  Per-request range: min={min(r['num_posts'] for r in rows)}, "
          f"max={max(r['num_posts'] for r in rows)}, "
          f"avg={total/len(rows):.1f}")
    counts = [r["num_posts"] for r in rows]
    print(f"  Sequence: {counts}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+", help="One or more network capture JSONL files.")
    ap.add_argument("--friendly-name", default=DEFAULT_FRIENDLY_NAME,
                    help=f"GraphQL friendly name to filter to (default: {DEFAULT_FRIENDLY_NAME})")
    args = ap.parse_args()

    paths = [Path(p) for p in args.jsonl]
    for p in paths:
        if not p.exists():
            print(f"error: {p} does not exist", file=sys.stderr)
            sys.exit(1)

    rows = analyze(paths, args.friendly_name)
    print_table(rows, args.friendly_name)
    verify_cursor_flow(rows)
    print_post_count_summary(rows)
    print_variables_constancy(rows)


if __name__ == "__main__":
    main()
