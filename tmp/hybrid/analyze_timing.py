"""
Phase 2.5 analyzer: temporal-pattern analysis of all network activity around
each ProfileCometTimelineFeedRefetchQuery (= pagination event).

Answers the question: when we fire pagination requests during scrolling,
what other XHRs fire alongside them? Is there a consistent pattern that
direct GraphQL replay would have to mimic to look organic?

For each pagination event P_i:
  - Lists all XHRs that fired between P_(i-1) and P_i (the "scroll cycle").
  - The activity before the first pagination is the "bootstrap".

Then aggregates:
  - Which URLs fire ONLY at bootstrap (one-time setup, safe to ignore for replay).
  - Which URLs fire in EVERY scroll cycle (must-mimic for replay).
  - Which URLs fire in SOME scroll cycles (probably triggered by ad-hoc UI events).

Run:
    python tmp/hybrid/analyze_timing.py \\
        data/hybrid/<dir>/network_*.jsonl
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"


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


def url_path(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}"
    except Exception:
        return url


def parse_ts(s: str) -> datetime:
    """Parse ISO-8601 timestamp string. Returns naive datetime in UTC."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def label_record(rec: dict, target_name: str) -> str:
    """Short label for the timeline view: GQL friendly name, or URL path."""
    if rec.get("is_graphql"):
        n = get_friendly_name(rec) or "<unknown>"
        if n == target_name:
            return f"** {n} **"
        return f"GQL:{n}"
    if rec.get("is_xhr"):
        return f"XHR:{url_path(rec.get('url', ''))}"
    rt = (rec.get("request") or {}).get("resource_type", "?")
    return f"{rt}:{url_path(rec.get('url', ''))}"


def load_all(jsonl_paths: list[Path]) -> list[dict]:
    """Load all records, sort by timestamp. Skip non-XHR / non-GraphQL by default
    since those are page chrome (CSS / JS / images), not behavioral signals."""
    out = []
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
                # Restrict to XHR (GraphQL is a subset of XHR). Non-XHR resources
                # are page chrome — they're triggered by DOM rendering, not by the
                # scroll behavior, so they don't matter for replay-detection
                # analysis.
                if not rec.get("is_xhr"):
                    continue
                out.append(rec)
    out.sort(key=lambda r: r.get("timestamp", ""))
    return out


def classify(records: list[dict], target_name: str):
    """Return (paginations, cycles, bootstrap, target_name).

    paginations: list of records whose friendly name == target_name
    cycles:      list of (pagination_record, cycle_records). cycle_records is
                 the list of XHRs that fired AFTER this pagination and BEFORE
                 the next pagination.
    bootstrap:   list of XHRs that fired BEFORE the first pagination.
    """
    paginations = [r for r in records if get_friendly_name(r) == target_name]
    if not paginations:
        return [], [], [], target_name

    pag_set = set(id(r) for r in paginations)

    # Bootstrap = everything before the first pagination.
    first_pag_ts = paginations[0].get("timestamp", "")
    bootstrap = [r for r in records if r.get("timestamp", "") < first_pag_ts]

    # Cycles = list of (pagination, [post-pagination requests up to next pagination])
    cycles = []
    for i, pag in enumerate(paginations):
        start_ts = pag.get("timestamp", "")
        end_ts = paginations[i + 1].get("timestamp", "") if i + 1 < len(paginations) else None
        cycle_records = []
        for r in records:
            ts = r.get("timestamp", "")
            if ts <= start_ts:
                continue
            if end_ts is not None and ts >= end_ts:
                continue
            if id(r) in pag_set:
                continue
            cycle_records.append(r)
        cycles.append((pag, cycle_records))

    return paginations, cycles, bootstrap, target_name


def fmt_dt(ts: str) -> str:
    try:
        return ts.split("T")[1].split(".")[0]
    except Exception:
        return ts


def print_bootstrap(bootstrap: list[dict], first_pag_ts: str):
    print(f"\n{'='*70}\nBOOTSTRAP (before first pagination)\n{'='*70}")
    if not bootstrap:
        print("  (none)")
        return
    span = (parse_ts(first_pag_ts) - parse_ts(bootstrap[0]["timestamp"])).total_seconds()
    print(f"  {len(bootstrap)} XHR over first {span:.1f}s")
    print()
    by_path = Counter()
    for r in bootstrap:
        if r.get("is_graphql"):
            label = f"GraphQL:{get_friendly_name(r) or '<unknown>'}"
        else:
            label = url_path(r.get("url", ""))
        by_path[label] += 1
    for label, n in by_path.most_common():
        print(f"    {n:>3}  {label}")


def print_cycles(cycles: list[tuple[dict, list[dict]]]):
    print(f"\n{'='*70}\nPER-PAGINATION SCROLL CYCLES\n{'='*70}")
    print(f"  Each row is one pagination + the XHRs that fire BEFORE the next one.\n")
    print(f"  {'P#':<4} {'time':<10} {'Δ next':<8} {'#xhr':<5} URLs in this cycle")
    print(f"  {'-'*4} {'-'*10} {'-'*8} {'-'*5} {'-'*40}")
    for i, (pag, cycle_records) in enumerate(cycles):
        ts = fmt_dt(pag.get("timestamp", ""))
        if i + 1 < len(cycles):
            d = (parse_ts(cycles[i + 1][0]["timestamp"]) - parse_ts(pag["timestamp"])).total_seconds()
            d_s = f"{d:.1f}s"
        else:
            d_s = "(last)"
        labels = []
        for r in cycle_records:
            if r.get("is_graphql"):
                labels.append(f"GQL:{get_friendly_name(r) or '?'}")
            else:
                labels.append(url_path(r.get("url", "")))
        # Compress the labels — show counts.
        from collections import Counter as C
        c = C(labels)
        joined = ", ".join(f"{n}× {l}" if n > 1 else l for l, n in c.most_common())
        print(f"  P{i:<3} {ts:<10} {d_s:<8} {len(cycle_records):<5} {joined or '(none)'}")


def print_aggregate_pattern(cycles: list[tuple[dict, list[dict]]], bootstrap: list[dict]):
    print(f"\n{'='*70}\nAGGREGATE PER-CYCLE PATTERN\n{'='*70}")
    if not cycles:
        return

    # For each URL path: count which cycles it appears in (presence), and total occurrences.
    cycle_presence: dict[str, int] = defaultdict(int)
    cycle_total: dict[str, int] = defaultdict(int)
    bootstrap_count: dict[str, int] = defaultdict(int)

    for r in bootstrap:
        path = url_path(r.get("url", ""))
        bootstrap_count[path] += 1

    for pag, cycle_records in cycles:
        seen_in_cycle = set()
        for r in cycle_records:
            if r.get("is_graphql"):
                continue  # We're already tracking these separately.
            path = url_path(r.get("url", ""))
            cycle_total[path] += 1
            seen_in_cycle.add(path)
        for path in seen_in_cycle:
            cycle_presence[path] += 1

    n_cycles = len(cycles)
    print(f"  Across {n_cycles} scroll cycles, non-GraphQL XHRs broken down:\n")
    print(f"  {'url path':<55} {'cycles':<8} {'presence':<10} {'avg/cycle':<10} {'bootstrap?':<10}")
    print(f"  {'-'*55} {'-'*8} {'-'*10} {'-'*10} {'-'*10}")

    all_paths = sorted(set(cycle_presence) | set(bootstrap_count),
                       key=lambda p: -cycle_presence.get(p, 0))
    for path in all_paths:
        cyc_n = cycle_presence.get(path, 0)
        cyc_total = cycle_total.get(path, 0)
        boot_n = bootstrap_count.get(path, 0)
        presence = f"{cyc_n}/{n_cycles}"
        avg = f"{cyc_total / n_cycles:.2f}" if n_cycles else "-"
        boot = f"yes ({boot_n})" if boot_n else "no"
        print(f"  {path[:55]:<55} {cyc_total:<8} {presence:<10} {avg:<10} {boot:<10}")

    # Classify URLs.
    print(f"\n  Classification:")
    bootstrap_only = [p for p in all_paths if bootstrap_count.get(p, 0) > 0 and cycle_presence.get(p, 0) == 0]
    every_cycle = [p for p in all_paths if cycle_presence.get(p, 0) == n_cycles]
    most_cycles = [p for p in all_paths if 0 < cycle_presence.get(p, 0) < n_cycles]

    if bootstrap_only:
        print(f"\n    Bootstrap-only ({len(bootstrap_only)} URLs — fire only at page load, safe to skip in replay):")
        for p in bootstrap_only:
            print(f"      {p}  (×{bootstrap_count[p]} at bootstrap)")
    if every_cycle:
        print(f"\n    Every-cycle ({len(every_cycle)} URLs — fire in EVERY pagination cycle, replay would need to mimic):")
        for p in every_cycle:
            print(f"      {p}")
    if most_cycles:
        print(f"\n    Some-cycles ({len(most_cycles)} URLs — sporadic, probably triggered by specific UI events):")
        for p in most_cycles:
            cyc_n = cycle_presence[p]
            print(f"      {p}  (in {cyc_n}/{n_cycles} cycles)")


def print_bnzai_detail(records: list[dict]):
    """The /ajax/bnzai endpoint is FB's behavioral beacon. If anything is
    'must-mimic for organic appearance' it's likely this — so look at it
    in detail."""
    bnzai = [r for r in records if "/ajax/bnzai" in (r.get("url") or "")]
    if not bnzai:
        return
    print(f"\n{'='*70}\n/ajax/bnzai DETAIL (FB's behavioral beacon)\n{'='*70}")
    print(f"  {len(bnzai)} hits total. URL query strings:\n")
    for r in bnzai[:10]:
        u = r.get("url", "")
        # Show URL with query truncated to first ~100 chars
        if len(u) > 110:
            u = u[:110] + "..."
        ts = fmt_dt(r.get("timestamp", ""))
        method = (r.get("request") or {}).get("method", "?")
        body_size = (r.get("request") or {}).get("post_data_size") or 0
        print(f"    {ts}  {method}  body={body_size}B")
    print()
    # Inter-arrival gaps — are they regular?
    times = sorted(parse_ts(r["timestamp"]) for r in bnzai)
    if len(times) > 1:
        gaps = [(times[i+1] - times[i]).total_seconds() for i in range(len(times)-1)]
        print(f"  Inter-arrival gaps (s): {[f'{g:.1f}' for g in gaps]}")
        print(f"  → mean={sum(gaps)/len(gaps):.1f}s, min={min(gaps):.1f}s, max={max(gaps):.1f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+", help="One or more network capture JSONL files.")
    ap.add_argument("--friendly-name", default=DEFAULT_FRIENDLY_NAME)
    args = ap.parse_args()

    paths = [Path(p) for p in args.jsonl]
    for p in paths:
        if not p.exists():
            print(f"error: {p} does not exist", file=sys.stderr)
            sys.exit(1)

    records = load_all(paths)
    paginations, cycles, bootstrap, target_name = classify(records, args.friendly_name)

    print(f"\nLoaded {len(records)} XHR records (non-XHR resources excluded).")
    print(f"Target friendly name: '{target_name}'")
    print(f"Paginations: {len(paginations)}")

    if not paginations:
        sys.exit(0)

    print_bootstrap(bootstrap, paginations[0]["timestamp"])
    print_cycles(cycles)
    print_aggregate_pattern(cycles, bootstrap)
    print_bnzai_detail(records)


if __name__ == "__main__":
    main()
