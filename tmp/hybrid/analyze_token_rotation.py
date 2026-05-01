"""
Correlate HasteBitMap token rotations with JS chunk loads.

Hypothesis: __csr / __dyn / __hsdp / __hblp / __sjsp change value only when new
JS resources are loaded (Bootloader registers a resource, ServerJSDefine
defines a module). To verify, walk the capture chronologically and check, for
each consecutive pair of ProfileCometTimelineFeedRefetchQuery POSTs:
  (a) Did any of the five HasteBitMap tokens change value?
  (b) Did any JS resources get fetched in the interval between the two POSTs?

If the hypothesis is right:
  - "token changed AND JS fetched" should be common.
  - "token changed AND NO JS fetched" should be rare or zero.
  - "token same AND JS fetched" can happen (re-fetches, irrelevant resources).
  - "token same AND no JS fetched" should be common (steady-state scrolling).

Run:
    python tmp/hybrid/analyze_token_rotation.py \\
        data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl


HASTE_FIELDS = ["__csr", "__dyn", "__hsdp", "__hblp", "__sjsp"]


def parse_timestamp(s: str) -> datetime:
    return datetime.fromisoformat(s)


def fingerprint(s: str) -> str:
    if not s:
        return "(empty)"
    return s[:6] + ".." + hashlib.md5(s.encode()).hexdigest()[:4]


def short_url(url: str) -> str:
    tail = url.split("/")[-1].split("?")[0]
    return tail[:50] + ("…" if len(tail) > 50 else "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=Path)
    args = ap.parse_args()

    if not args.capture.exists():
        print(f"capture not found: {args.capture}", file=sys.stderr)
        sys.exit(1)

    pctfrq = []        # list of (timestamp, {field: value})
    js_fetches = []    # list of (timestamp, url, body_size)

    with args.capture.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = rec.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = parse_timestamp(ts_str)
            except ValueError:
                continue
            req = rec.get("request", {})
            post = req.get("post_data") or ""
            if "ProfileCometTimelineFeedRefetchQuery" in post:
                fields = dict(parse_qsl(post, keep_blank_values=True))
                pctfrq.append((ts, {f: fields.get(f, "") for f in HASTE_FIELDS}))
                continue
            if req.get("resource_type") == "script":
                size = rec.get("response", {}).get("body_size") or 0
                js_fetches.append((ts, rec.get("url", ""), size))

    pctfrq.sort(key=lambda x: x[0])
    js_fetches.sort(key=lambda x: x[0])

    print(f"Capture: {args.capture}")
    print(f"PCTFRQs:    {len(pctfrq)}")
    print(f"JS fetches: {len(js_fetches)}")
    print()

    print("Unique values seen per token:")
    for f in HASTE_FIELDS:
        unique = {v[1][f] for v in pctfrq}
        print(f"  {f:<8}  {len(unique)} unique  across {len(pctfrq)} PCTFRQs")
    print()

    # Per-pair correlation table.
    correlation = {f: {"change_with_js": 0, "change_without_js": 0,
                       "no_change_with_js": 0, "no_change_without_js": 0}
                   for f in HASTE_FIELDS}

    print("=" * 110)
    print("CONSECUTIVE PCTFRQ PAIRS — token change vs JS fetches in the interval")
    print("=" * 110)
    print(f"{'pair':>4}  {'gap(s)':>7}  {'JS#':>4}  {'changed':<32}  fingerprints (csr / dyn / hsdp / hblp / sjsp)")
    for i in range(1, len(pctfrq)):
        t_prev, vals_prev = pctfrq[i - 1]
        t_now, vals_now = pctfrq[i]
        gap = (t_now - t_prev).total_seconds()
        js_in = [j for j in js_fetches if t_prev < j[0] <= t_now]
        nj = len(js_in)
        changes = [f for f in HASTE_FIELDS if vals_prev[f] != vals_now[f]]
        for f in HASTE_FIELDS:
            changed = vals_prev[f] != vals_now[f]
            key = ("change" if changed else "no_change") + ("_with_js" if nj else "_without_js")
            correlation[f][key] += 1
        change_str = ",".join(c.lstrip("_") for c in changes) or "-"
        fps = "  ".join(fingerprint(vals_now[f]) for f in HASTE_FIELDS)
        print(f"  {i:>4}  {gap:>7.2f}  {nj:>4}  {change_str:<32}  {fps}")
    print()

    print("=" * 110)
    print("CORRELATION SUMMARY")
    print("=" * 110)
    print(f"{'field':<8}  {'changed & JS':>13}  {'changed & no-JS':>16}  {'same & JS':>10}  {'same & no-JS':>13}")
    total_pairs = max(0, len(pctfrq) - 1)
    for f in HASTE_FIELDS:
        c = correlation[f]
        print(f"  {f:<8}  {c['change_with_js']:>13}  "
              f"{c['change_without_js']:>16}  {c['no_change_with_js']:>10}  "
              f"{c['no_change_without_js']:>13}")
    print()
    print(f"Total consecutive pairs analyzed: {total_pairs}")
    print()
    print("Reading the table:")
    print("  - 'changed & no-JS' near zero  -> hypothesis confirmed (token only rotates")
    print("                                     when new JS loads).")
    print("  - 'same & JS' may be nonzero   -> some JS fetches don't add new bits")
    print("                                     (re-fetches of already-loaded resources).")
    print("  - 'same & no-JS' should dominate at steady-state.")
    print()

    print("=" * 110)
    print("DETAIL — what JS loaded immediately before each token change?")
    print("=" * 110)
    for i in range(1, len(pctfrq)):
        t_prev, vals_prev = pctfrq[i - 1]
        t_now, vals_now = pctfrq[i]
        changes = [f for f in HASTE_FIELDS if vals_prev[f] != vals_now[f]]
        if not changes:
            continue
        js_in = [j for j in js_fetches if t_prev < j[0] <= t_now]
        gap = (t_now - t_prev).total_seconds()
        head = f"pair #{i}  gap={gap:.2f}s  changed={','.join(c.lstrip('_') for c in changes)}"
        if not js_in:
            print(f"\n  {head}  ⚠ NO JS fetched in window")
            continue
        print(f"\n  {head}  ({len(js_in)} JS in window)")
        for ts, url, size in js_in:
            print(f"    [{ts.strftime('%H:%M:%S.%f')[:-3]}]  {size:>8}b  {short_url(url)}")
    print()


if __name__ == "__main__":
    main()
