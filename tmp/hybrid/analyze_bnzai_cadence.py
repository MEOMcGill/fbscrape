"""
Test whether /ajax/bnzai is a wall-clock heartbeat or pagination-triggered.

For each /ajax/bnzai hit in steady state (post-bootstrap), compute:
  - Wall-clock gap from previous bnzai hit (seconds)
  - Number of paginations that fired in that window
  - Average pagination cadence in that window (seconds/pagination)

If bnzai is timer-driven: gap stays ~30s regardless of pagination count.
If bnzai is pagination-triggered: gap = N_paginations × pagination_cadence,
  i.e. gap correlates with pagination cadence and N_paginations clusters.

Run:
    python tmp/hybrid/analyze_bnzai_cadence.py \\
        data/hybrid/batch_<UTC-timestamp>/
"""

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs


TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"


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


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def analyze_one(jsonl_path: Path):
    """Return list of (gap_seconds, n_paginations_in_window, pagination_cadence_in_window)."""
    bnzai_times = []
    pagination_times = []
    handle = jsonl_path.stem

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = rec.get("url", "")
            ts = rec.get("timestamp")
            if not ts:
                continue
            t = parse_ts(ts)
            if "/ajax/bnzai" in url:
                bnzai_times.append(t)
            elif get_friendly_name(rec) == TARGET_FRIENDLY_NAME:
                pagination_times.append(t)

    bnzai_times.sort()
    pagination_times.sort()

    if not bnzai_times or not pagination_times:
        return handle, []

    # Drop bootstrap bnzai cluster — keep only post-first-pagination hits.
    first_pag = pagination_times[0]
    bnzai_steady = [t for t in bnzai_times if t > first_pag]

    if len(bnzai_steady) < 2:
        return handle, []

    rows = []
    for i in range(1, len(bnzai_steady)):
        prev = bnzai_steady[i - 1]
        curr = bnzai_steady[i]
        gap = (curr - prev).total_seconds()
        # Paginations in this window
        pags_in_window = [t for t in pagination_times if prev < t <= curr]
        n_pags = len(pags_in_window)
        if n_pags >= 2:
            pag_gaps = [(pags_in_window[j] - pags_in_window[j - 1]).total_seconds()
                        for j in range(1, n_pags)]
            cadence = statistics.mean(pag_gaps)
        else:
            cadence = None
        rows.append((gap, n_pags, cadence))
    return handle, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="batch dir or single jsonl file")
    args = ap.parse_args()

    p = Path(args.path)
    if p.is_file():
        files = [p]
    else:
        files = sorted(p.glob("network_*.jsonl"))

    if not files:
        print("no files found", file=sys.stderr); sys.exit(1)

    all_rows = []  # (handle, gap, n_pags, cadence)
    for f in files:
        handle, rows = analyze_one(f)
        for gap, n, cad in rows:
            all_rows.append((handle, gap, n, cad))

    if not all_rows:
        print("no steady-state bnzai data found"); sys.exit(0)

    # Per-capture detail
    print(f"\n{'='*100}")
    print("PER-INTERVAL DETAIL (each row = one bnzai-to-bnzai interval, post-bootstrap)")
    print(f"{'='*100}")
    print(f"  {'capture':<55} {'gap_s':>7} {'#pag':>5} {'pag_cadence_s':>14}")
    print(f"  {'-'*55} {'-'*7} {'-'*5} {'-'*14}")
    for handle, gap, n, cad in all_rows:
        cad_s = f"{cad:.2f}" if cad is not None else "-"
        print(f"  {handle[:55]:<55} {gap:>7.1f} {n:>5} {cad_s:>14}")

    # Aggregate stats
    print(f"\n{'='*70}")
    print("AGGREGATE")
    print(f"{'='*70}")
    gaps = [g for _, g, _, _ in all_rows]
    pags_per_interval = [n for _, _, n, _ in all_rows]
    cadences = [c for _, _, _, c in all_rows if c is not None]

    print(f"\n  bnzai gap (wall-clock seconds):")
    print(f"    mean:    {statistics.mean(gaps):.1f}s")
    print(f"    median:  {statistics.median(gaps):.1f}s")
    print(f"    stdev:   {statistics.stdev(gaps):.1f}s" if len(gaps) > 1 else "")
    print(f"    range:   {min(gaps):.1f}s — {max(gaps):.1f}s")

    print(f"\n  paginations per bnzai interval:")
    print(f"    mean:    {statistics.mean(pags_per_interval):.1f}")
    print(f"    median:  {statistics.median(pags_per_interval):.1f}")
    print(f"    stdev:   {statistics.stdev(pags_per_interval):.1f}" if len(pags_per_interval) > 1 else "")
    print(f"    range:   {min(pags_per_interval)} — {max(pags_per_interval)}")

    if cadences:
        print(f"\n  pagination cadence in each bnzai interval (sec/pag):")
        print(f"    mean:    {statistics.mean(cadences):.2f}s")
        print(f"    median:  {statistics.median(cadences):.2f}s")
        print(f"    range:   {min(cadences):.2f}s — {max(cadences):.2f}s")

    # The decisive test: is gap correlated with cadence?
    if cadences and len(cadences) > 5:
        # Pearson correlation between gap and cadence
        # If timer-driven: correlation should be ~0 (gap doesn't depend on cadence)
        # If pagination-tied with constant N: gap = N * cadence → correlation should be ~1.0
        paired = [(g, c) for _, g, _, c in all_rows if c is not None]
        n = len(paired)
        mx = sum(g for g, _ in paired) / n
        my = sum(c for _, c in paired) / n
        sxy = sum((g - mx) * (c - my) for g, c in paired)
        sxx = sum((g - mx) ** 2 for g, _ in paired)
        syy = sum((c - my) ** 2 for _, c in paired)
        if sxx > 0 and syy > 0:
            corr = sxy / (sxx ** 0.5 * syy ** 0.5)
            print(f"\n  Pearson correlation (gap vs. pagination cadence): {corr:.3f}")
            print(f"    → ~0.0 = timer-driven (gap doesn't depend on how fast we paginate)")
            print(f"    → ~1.0 = pagination-tied (gap = N × cadence)")

        # Also check: is N (paginations per interval) more constant, or is gap more constant?
        # CV = stdev / mean. Lower CV = more constant.
        cv_gap = statistics.stdev(gaps) / statistics.mean(gaps) if statistics.mean(gaps) > 0 else 0
        cv_n = statistics.stdev(pags_per_interval) / statistics.mean(pags_per_interval) if statistics.mean(pags_per_interval) > 0 else 0
        print(f"\n  Coefficient of variation (lower = more constant):")
        print(f"    gap (wall-clock seconds):       CV = {cv_gap:.2f}")
        print(f"    paginations per interval:       CV = {cv_n:.2f}")
        print(f"    → if gap CV is much lower: timer-driven")
        print(f"    → if N CV is much lower:   pagination-tied with fixed N")


if __name__ == "__main__":
    main()
