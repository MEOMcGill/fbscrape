"""
First-pass analyzer for a Path B network capture JSONL.

Produces a summary that answers the surface-level questions for Path B viability:

  1. Inventory: how many records, broken down by resource_type / xhr / graphql / status.
  2. GraphQL query breakdown: which friendly names fire, how often, and which
     return post-bearing responses.
  3. Non-GraphQL XHR breakdown: URL-path groupings (these are what direct
     GraphQL replay would NOT have surrounding it — relevant to detection risk).
  4. Token snapshot: pulls fb_dtsg / lsd / jazoest / __rev / __hsi / __spin_*
     from GraphQL request bodies, shows unique values per token (= rotation cadence).

Run:

    python tmp/hybrid/analyze_capture.py \\
        data/hybrid/<handle>_<ts>/network_<ts>_<id>.jsonl

Optionally specify multiple files; results are summed across them.

Output:
  - Stdout: human-readable summary.
  - <input>.summary.json: machine-readable summary in the same dir as the input.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def parse_form_body(post_data: str | None) -> dict[str, str]:
    """Best-effort parse of a urlencoded form body. Multi-valued keys collapse
    to the last value; we don't care about repeats here."""
    if not post_data:
        return {}
    try:
        parsed = parse_qs(post_data, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}
    except Exception:
        return {}


def get_friendly_name(record: dict) -> str | None:
    """Extract GraphQL friendly name from headers or post body."""
    req = record.get("request", {}) or {}
    headers = req.get("headers", {}) or {}
    name = headers.get("x-fb-friendly-name")
    if name:
        return name
    form = parse_form_body(req.get("post_data"))
    return form.get("fb_api_req_friendly_name")


def graphql_response_has_posts(body: str | None) -> bool:
    """Heuristic: response body mentions a Story node, edge, or post_id field."""
    if not body:
        return False
    # Cheap substring checks before json parse — these strings appear in
    # any timeline-bearing GraphQL response.
    if '"post_id"' not in body and '"timeline_list_feed_units"' not in body:
        return False
    return True


def url_path(url: str) -> str:
    """Strip query string from URL for grouping."""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url


def analyze(jsonl_paths: list[Path]) -> dict:
    total = 0
    by_resource_type: Counter = Counter()
    by_status: Counter = Counter()
    xhr_count = 0
    graphql_count = 0
    non_graphql_xhr_count = 0
    non_xhr_count = 0
    body_skipped_count = 0
    total_bytes_in = 0  # response body sizes
    total_bytes_out = 0  # request body sizes

    # GraphQL grouping
    gql_by_friendly: Counter = Counter()
    gql_post_bearing_by_friendly: Counter = Counter()
    gql_doc_ids_by_friendly: dict[str, set] = defaultdict(set)
    gql_status_by_friendly: dict[str, Counter] = defaultdict(Counter)

    # Non-GraphQL XHR — URL-path grouping
    non_gql_xhr_paths: Counter = Counter()

    # Non-XHR (CSS/JS/images) — resource_type breakdown only
    non_xhr_paths: Counter = Counter()

    # Token rotation tracking — across all GraphQL requests
    token_keys = ["fb_dtsg", "lsd", "jazoest", "__rev", "__hsi", "__spin_r", "__spin_t", "__spin_b", "__csr", "__dyn"]
    token_values: dict[str, set[str]] = {k: set() for k in token_keys}

    for path in jsonl_paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                total += 1
                req = rec.get("request") or {}
                resp = rec.get("response") or {}
                rt = req.get("resource_type", "?")
                status = resp.get("status", "?")
                is_xhr = bool(rec.get("is_xhr"))
                is_gql = bool(rec.get("is_graphql"))

                by_resource_type[rt] += 1
                by_status[status] += 1
                if is_xhr:
                    xhr_count += 1
                else:
                    non_xhr_count += 1
                    non_xhr_paths[rt] += 1

                if resp.get("body_skipped"):
                    body_skipped_count += 1

                bsz = resp.get("body_size") or 0
                total_bytes_in += bsz
                psz = req.get("post_data_size") or 0
                total_bytes_out += psz

                if is_gql:
                    graphql_count += 1
                    name = get_friendly_name(rec) or "<unknown>"
                    gql_by_friendly[name] += 1
                    gql_status_by_friendly[name][status] += 1
                    if graphql_response_has_posts(resp.get("body")):
                        gql_post_bearing_by_friendly[name] += 1
                    form = parse_form_body(req.get("post_data"))
                    doc_id = form.get("doc_id")
                    if doc_id:
                        gql_doc_ids_by_friendly[name].add(doc_id)
                    for k in token_keys:
                        v = form.get(k)
                        if v is not None:
                            token_values[k].add(v)
                elif is_xhr:
                    non_graphql_xhr_count += 1
                    non_gql_xhr_paths[url_path(rec.get("url", ""))] += 1

    return {
        "total": total,
        "by_resource_type": dict(by_resource_type.most_common()),
        "by_status": dict(by_status.most_common()),
        "xhr_count": xhr_count,
        "non_xhr_count": non_xhr_count,
        "graphql_count": graphql_count,
        "non_graphql_xhr_count": non_graphql_xhr_count,
        "body_skipped_count": body_skipped_count,
        "total_response_bytes": total_bytes_in,
        "total_request_bytes": total_bytes_out,
        "graphql_by_friendly_name": dict(gql_by_friendly.most_common()),
        "graphql_post_bearing_by_friendly": dict(gql_post_bearing_by_friendly.most_common()),
        "graphql_doc_ids_by_friendly": {k: sorted(v) for k, v in gql_doc_ids_by_friendly.items()},
        "graphql_status_by_friendly": {k: dict(c.most_common()) for k, c in gql_status_by_friendly.items()},
        "non_graphql_xhr_paths": dict(non_gql_xhr_paths.most_common()),
        "non_xhr_resource_types": dict(non_xhr_paths.most_common()),
        "token_unique_values_count": {k: len(v) for k, v in token_values.items()},
    }


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def print_summary(s: dict) -> None:
    print(f"\n{'='*70}\nINVENTORY\n{'='*70}")
    print(f"  total records:        {s['total']}")
    print(f"    XHR:                {s['xhr_count']}")
    print(f"      GraphQL:          {s['graphql_count']}")
    print(f"      non-GraphQL XHR:  {s['non_graphql_xhr_count']}")
    print(f"    non-XHR:            {s['non_xhr_count']}")
    print(f"  body skipped (binary): {s['body_skipped_count']}")
    print(f"  total response bytes: {fmt_bytes(s['total_response_bytes'])}")
    print(f"  total request bytes:  {fmt_bytes(s['total_request_bytes'])}")
    print(f"\n  by resource_type:")
    for rt, n in s["by_resource_type"].items():
        print(f"    {rt:>16}  {n}")
    print(f"\n  by HTTP status:")
    for st, n in s["by_status"].items():
        print(f"    {str(st):>16}  {n}")

    print(f"\n{'='*70}\nGRAPHQL QUERIES (by friendly name)\n{'='*70}")
    by_name = s["graphql_by_friendly_name"]
    post_bearing = s["graphql_post_bearing_by_friendly"]
    doc_ids = s["graphql_doc_ids_by_friendly"]
    statuses = s["graphql_status_by_friendly"]
    print(f"  {'friendly name':<55} {'total':>6} {'posts':>6} {'doc_ids':>8} status")
    print(f"  {'-'*55} {'-'*6} {'-'*6} {'-'*8} {'-'*15}")
    for name, n in by_name.items():
        pb = post_bearing.get(name, 0)
        di_count = len(doc_ids.get(name, []))
        st_summary = ", ".join(f"{k}:{v}" for k, v in statuses.get(name, {}).items())
        print(f"  {name:<55} {n:>6} {pb:>6} {di_count:>8} {st_summary}")

    # Show doc_ids for any friendly names that yielded posts (those are our targets)
    targets = {n: doc_ids.get(n, []) for n in post_bearing if doc_ids.get(n)}
    if targets:
        print(f"\n  doc_ids for post-bearing queries:")
        for n, ids in targets.items():
            print(f"    {n}:")
            for did in ids:
                print(f"      {did}")

    print(f"\n{'='*70}\nNON-GRAPHQL XHR (surrounding telemetry)\n{'='*70}")
    print(f"  {'count':>6}  url path")
    for url, n in s["non_graphql_xhr_paths"].items():
        print(f"  {n:>6}  {url}")

    print(f"\n{'='*70}\nNON-XHR resource types (page chrome)\n{'='*70}")
    for rt, n in s["non_xhr_resource_types"].items():
        print(f"  {rt:>16}  {n}")

    print(f"\n{'='*70}\nTOKEN ROTATION\n{'='*70}")
    print(f"  Unique values across all GraphQL requests in this session:")
    print(f"  (1 = stable across the entire session;  N>1 = rotates every N requests on average)")
    for k, n in s["token_unique_values_count"].items():
        marker = "  STABLE" if n <= 1 else f"  ROTATES ({n} unique)"
        print(f"    {k:<14} {n:>4}{marker}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+", help="One or more network capture JSONL files.")
    ap.add_argument("--no-write", action="store_true", help="Skip writing summary.json output.")
    args = ap.parse_args()

    paths = [Path(p) for p in args.jsonl]
    for p in paths:
        if not p.exists():
            print(f"error: {p} does not exist", file=sys.stderr)
            sys.exit(1)

    summary = analyze(paths)
    print_summary(summary)

    if not args.no_write:
        out_path = paths[0].parent / (paths[0].stem + ".summary.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=lambda o: list(o) if isinstance(o, set) else str(o))
        print(f"\n[saved] {out_path}")


if __name__ == "__main__":
    main()
