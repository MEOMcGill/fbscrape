"""
Batch analyzer: runs the Phase 1 / Phase 2 / Phase 2.5 checks across every
network_*.jsonl in a batch capture directory, and aggregates findings to
test whether the patterns we observed in single-handle captures hold
across many independent sessions.

Per-capture, checks:
  - Workhorse GraphQL friendly name + doc_id constancy
  - Auth-token stability (fb_dtsg, lsd, jazoest, __rev, __hsi, __spin_*)
  - __csr / __dyn rotation rates
  - Cursor-pagination contract (all pairs match)
  - Variables-payload constancy (only `cursor` should vary)
  - /ajax/bnzai heartbeat interval
  - /ajax/bulk-route-definitions/ presence rate

Aggregates: consistency / variance across captures.

Run:
    python tmp/hybrid/analyze_batch.py \\
        data/hybrid/batch_<UTC-timestamp>/
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse


TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"
EXPECTED_DOC_ID = "26563935306593088"
STABLE_TOKENS = ("fb_dtsg", "lsd", "jazoest", "__rev", "__hsi", "__spin_r", "__spin_t", "__spin_b")
ROTATING_TOKENS = ("__csr", "__dyn")


def parse_form(post_data):
    if not post_data:
        return {}
    try:
        parsed = parse_qs(post_data, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}
    except Exception:
        return {}


def get_friendly_name(rec):
    headers = (rec.get("request") or {}).get("headers") or {}
    name = headers.get("x-fb-friendly-name")
    if name:
        return name
    return parse_form((rec.get("request") or {}).get("post_data")).get("fb_api_req_friendly_name")


def url_path(url):
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}"
    except Exception:
        return url


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def parse_response_docs(body):
    if not body:
        return []
    out = []
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
    try:
        return [json.loads(body)]
    except json.JSONDecodeError:
        return []


def walk(obj, target_keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in target_keys:
                yield (k, v)
            yield from walk(v, target_keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item, target_keys)


def extract_response_info(body):
    docs = parse_response_docs(body)
    end_cursor = None
    has_next_page = None
    post_ids = set()
    for doc in docs:
        for k, v in walk(doc, {"end_cursor", "endCursor", "has_next_page", "hasNextPage", "post_id"}):
            if k in ("end_cursor", "endCursor") and v and end_cursor is None:
                end_cursor = v
            elif k in ("has_next_page", "hasNextPage") and has_next_page is None:
                has_next_page = bool(v)
            elif k == "post_id" and isinstance(v, str):
                post_ids.add(v)
    return end_cursor, has_next_page, len(post_ids)


def identify_handle(records, manifest_results):
    """Find the handle scraped in this capture. Look for the profile-page
    document load (`https://www.facebook.com/<handle>/`)."""
    for rec in records:
        rt = (rec.get("request") or {}).get("resource_type")
        if rt != "document":
            continue
        u = rec.get("url", "")
        try:
            p = urlparse(u)
        except Exception:
            continue
        if "facebook.com" not in p.netloc:
            continue
        path = p.path.strip("/")
        if path and "/" not in path and "?" not in path:
            return path
    # Fallback: extract profile id from any GraphQL request and match by manifest.
    for rec in records:
        if get_friendly_name(rec) != TARGET_FRIENDLY_NAME:
            continue
        try:
            variables = json.loads(parse_form((rec.get("request") or {}).get("post_data")).get("variables", "{}"))
        except json.JSONDecodeError:
            continue
        pid = variables.get("id")
        if pid:
            return f"<profile_id={pid}>"
    return "<unknown>"


def analyze_capture(jsonl_path: Path):
    """Return per-capture summary dict."""
    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # Handle identification
    handle = identify_handle(records, None)

    # GraphQL inventory
    gql_by_name = Counter()
    gql_post_bearing_by_name = Counter()
    doc_ids_by_name = defaultdict(set)
    pagination_records = []
    token_values = {k: set() for k in STABLE_TOKENS + ROTATING_TOKENS}

    for rec in records:
        if not rec.get("is_graphql"):
            continue
        name = get_friendly_name(rec) or "<unknown>"
        gql_by_name[name] += 1
        form = parse_form((rec.get("request") or {}).get("post_data"))
        doc_id = form.get("doc_id")
        if doc_id:
            doc_ids_by_name[name].add(doc_id)
        body = (rec.get("response") or {}).get("body")
        _ec, _hnp, n_posts = extract_response_info(body)
        if n_posts > 0:
            gql_post_bearing_by_name[name] += 1
        if name == TARGET_FRIENDLY_NAME:
            pagination_records.append(rec)
            for k in token_values:
                v = form.get(k)
                if v is not None:
                    token_values[k].add(v)

    # Cursor flow + variables constancy
    pagination_records.sort(key=lambda r: r.get("timestamp", ""))
    cursor_pairs_total = max(0, len(pagination_records) - 1)
    cursor_pairs_match = 0
    variables_keys = set()
    variables_unique_per_key = defaultdict(set)
    end_cursors = []
    cursors_in = []
    for rec in pagination_records:
        form = parse_form((rec.get("request") or {}).get("post_data"))
        try:
            variables = json.loads(form.get("variables", "{}"))
        except json.JSONDecodeError:
            variables = {}
        cursors_in.append(variables.get("cursor"))
        for k, v in variables.items():
            variables_keys.add(k)
            variables_unique_per_key[k].add(json.dumps(v, sort_keys=True, default=str))
        body = (rec.get("response") or {}).get("body")
        ec, _, _ = extract_response_info(body)
        end_cursors.append(ec)

    for i in range(cursor_pairs_total):
        if end_cursors[i] is not None and cursors_in[i + 1] is not None and end_cursors[i] == cursors_in[i + 1]:
            cursor_pairs_match += 1

    # /ajax/bnzai heartbeat
    bnzai = [r for r in records if "/ajax/bnzai" in (r.get("url") or "")]
    bnzai_times = sorted(parse_ts(r["timestamp"]) for r in bnzai)
    bnzai_gaps = [(bnzai_times[i + 1] - bnzai_times[i]).total_seconds() for i in range(len(bnzai_times) - 1)]
    # Drop the bootstrap cluster (first ~few seconds) — heartbeat starts after
    bnzai_steady_gaps = [g for g in bnzai_gaps if g > 5]

    # /ajax/bulk-route-definitions/ presence
    bulk_route = [r for r in records if "/ajax/bulk-route-definitions/" in (r.get("url") or "")]

    # Total record counts
    total_records = len(records)
    xhr_records = sum(1 for r in records if r.get("is_xhr"))
    graphql_records = sum(1 for r in records if r.get("is_graphql"))

    summary = {
        "file": jsonl_path.name,
        "handle": handle,
        "total_records": total_records,
        "xhr_records": xhr_records,
        "graphql_records": graphql_records,
        "n_pagination": len(pagination_records),
        "post_bearing_friendly_names": dict(gql_post_bearing_by_name),
        "all_friendly_names_count": len(gql_by_name),
        "doc_ids_workhorse": sorted(doc_ids_by_name.get(TARGET_FRIENDLY_NAME, [])),
        "cursor_pairs": (cursor_pairs_match, cursor_pairs_total),
        "variables_total_keys": len(variables_keys),
        "variables_constant_keys": sum(1 for k, vs in variables_unique_per_key.items() if len(vs) == 1),
        "variables_varying_keys": sorted(k for k, vs in variables_unique_per_key.items() if len(vs) > 1),
        "stable_tokens_unique": {k: len(token_values[k]) for k in STABLE_TOKENS},
        "rotating_tokens_unique": {k: len(token_values[k]) for k in ROTATING_TOKENS},
        "bnzai_count": len(bnzai),
        "bnzai_steady_gaps": [round(g, 1) for g in bnzai_steady_gaps],
        "bnzai_steady_mean": round(sum(bnzai_steady_gaps) / len(bnzai_steady_gaps), 1) if bnzai_steady_gaps else None,
        "bulk_route_count": len(bulk_route),
    }
    return summary


def print_per_capture(summaries):
    print(f"\n{'='*120}\nPER-CAPTURE SUMMARY\n{'='*120}")
    print(f"  {'handle':<25} {'#GQL':<5} {'#pag':<5} {'cursor':<8} {'vars':<10} {'stable?':<9} {'__csr':<6} {'__dyn':<6} {'bnzai':<6} {'bnzai_mean':<10}")
    print(f"  {'-'*25} {'-'*5} {'-'*5} {'-'*8} {'-'*10} {'-'*9} {'-'*6} {'-'*6} {'-'*6} {'-'*10}")
    for s in summaries:
        cursor = f"{s['cursor_pairs'][0]}/{s['cursor_pairs'][1]}"
        vars_summary = f"{s['variables_constant_keys']}/{s['variables_total_keys']}"
        all_stable = all(v == 1 for v in s["stable_tokens_unique"].values())
        stable_marker = "✓" if all_stable else "✗"
        csr = s["rotating_tokens_unique"]["__csr"]
        dyn = s["rotating_tokens_unique"]["__dyn"]
        bnzai_mean = f"{s['bnzai_steady_mean']:.1f}s" if s["bnzai_steady_mean"] else "-"
        print(f"  {s['handle'][:25]:<25} {s['graphql_records']:<5} {s['n_pagination']:<5} "
              f"{cursor:<8} {vars_summary:<10} {stable_marker:<9} {csr:<6} {dyn:<6} {s['bnzai_count']:<6} {bnzai_mean:<10}")


def print_aggregate(summaries):
    print(f"\n{'='*70}\nAGGREGATE CONSISTENCY CHECKS\n{'='*70}")

    # 1. Single workhorse query?
    workhorse_only = sum(1 for s in summaries
                         if list(s["post_bearing_friendly_names"].keys()) == [TARGET_FRIENDLY_NAME])
    print(f"\n  1. Single post-bearing query is '{TARGET_FRIENDLY_NAME}':")
    print(f"     {workhorse_only}/{len(summaries)} captures match")
    other = []
    for s in summaries:
        for name in s["post_bearing_friendly_names"]:
            if name != TARGET_FRIENDLY_NAME:
                other.append((s["handle"], name, s["post_bearing_friendly_names"][name]))
    if other:
        print(f"     Other post-bearing queries seen:")
        for h, n, c in other:
            print(f"       {h}: {n} (×{c})")

    # 2. doc_id constancy
    all_doc_ids = set()
    for s in summaries:
        all_doc_ids.update(s["doc_ids_workhorse"])
    print(f"\n  2. doc_id for '{TARGET_FRIENDLY_NAME}' across all captures:")
    print(f"     Unique doc_ids seen: {len(all_doc_ids)}")
    for d in sorted(all_doc_ids):
        n_caps = sum(1 for s in summaries if d in s["doc_ids_workhorse"])
        marker = " (EXPECTED)" if d == EXPECTED_DOC_ID else ""
        print(f"       {d}{marker}  used in {n_caps}/{len(summaries)} captures")

    # 3. Cursor flow integrity
    all_match = sum(1 for s in summaries if s["cursor_pairs"][0] == s["cursor_pairs"][1] and s["cursor_pairs"][1] > 0)
    no_pagination = sum(1 for s in summaries if s["cursor_pairs"][1] == 0)
    print(f"\n  3. Cursor pagination contract (all pairs match):")
    print(f"     {all_match}/{len(summaries) - no_pagination} captures with paginations have all-match")
    if no_pagination:
        print(f"     ({no_pagination} captures had 0 paginations — too short or no posts)")
    for s in summaries:
        m, t = s["cursor_pairs"]
        if t > 0 and m != t:
            print(f"     ⚠  {s['handle']}: {m}/{t} matched")

    # 4. Variables constancy (42 of 43 should be constant; only `cursor` should vary)
    print(f"\n  4. Variables constancy (only `cursor` should vary):")
    perfect = sum(1 for s in summaries
                  if s["variables_total_keys"] - s["variables_constant_keys"] == 1
                  and s["variables_varying_keys"] == ["cursor"])
    print(f"     {perfect}/{len(summaries)} captures have exactly one varying key, and it's `cursor`")
    weird = [(s["handle"], s["variables_varying_keys"]) for s in summaries
             if s["variables_varying_keys"] and s["variables_varying_keys"] != ["cursor"]]
    if weird:
        print(f"     Captures with non-`cursor` varying variables:")
        for h, keys in weird:
            print(f"       {h}: {keys}")

    # 5. Stable-token stability
    print(f"\n  5. Stable auth tokens unique-count (1 = stable across session):")
    for tok in STABLE_TOKENS:
        per_capture = [s["stable_tokens_unique"][tok] for s in summaries]
        all_stable = all(v == 1 for v in per_capture)
        marker = "✓" if all_stable else f"✗ ({per_capture})"
        print(f"     {tok:<14} {marker}")

    # 6. __csr / __dyn rotation rates
    print(f"\n  6. Rotating tokens — unique-count per session, normalized by paginations:")
    for tok in ROTATING_TOKENS:
        rates = []
        for s in summaries:
            if s["n_pagination"] > 0:
                rates.append(s["rotating_tokens_unique"][tok] / s["n_pagination"])
        if rates:
            print(f"     {tok}: avg unique/pagination = {sum(rates)/len(rates):.2f}  "
                  f"(min={min(rates):.2f}, max={max(rates):.2f})")

    # 7. /ajax/bnzai heartbeat
    print(f"\n  7. /ajax/bnzai steady-state heartbeat (post-bootstrap):")
    means = [s["bnzai_steady_mean"] for s in summaries if s["bnzai_steady_mean"]]
    if means:
        avg = sum(means) / len(means)
        print(f"     {len(means)}/{len(summaries)} captures have ≥2 steady-state hits")
        print(f"     mean inter-arrival across captures: {avg:.1f}s")
        print(f"     range: {min(means):.1f}s — {max(means):.1f}s")
        # Per-capture distribution
        suspiciously_off = [s for s in summaries
                            if s["bnzai_steady_mean"] is not None
                            and abs(s["bnzai_steady_mean"] - 30) > 5]
        if suspiciously_off:
            print(f"     ⚠  Captures with mean far from 30s:")
            for s in suspiciously_off:
                print(f"       {s['handle']}: mean={s['bnzai_steady_mean']}s, gaps={s['bnzai_steady_gaps']}")
    else:
        print(f"     No captures had enough /ajax/bnzai hits to compute heartbeat")

    # 8. /ajax/bulk-route-definitions/ presence
    print(f"\n  8. /ajax/bulk-route-definitions/ ratio per pagination:")
    for s in summaries:
        if s["n_pagination"] > 0:
            ratio = s["bulk_route_count"] / s["n_pagination"]
            print(f"     {s['handle']:<25} {s['bulk_route_count']:>4} calls / {s['n_pagination']:>3} pags = {ratio:.2f}/pag")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch_dir", help="Path to a batch directory (or a network_*.jsonl file)")
    args = ap.parse_args()

    p = Path(args.batch_dir)
    if p.is_file():
        files = [p]
    else:
        files = sorted(p.glob("network_*.jsonl"))

    if not files:
        print(f"error: no network_*.jsonl files under {p}", file=sys.stderr)
        sys.exit(1)

    print(f"Analyzing {len(files)} capture files in {p}\n")

    summaries = []
    for f in files:
        try:
            s = analyze_capture(f)
        except Exception as e:
            print(f"  ERROR analyzing {f.name}: {e}")
            continue
        summaries.append(s)

    print_per_capture(summaries)
    print_aggregate(summaries)


if __name__ == "__main__":
    main()
