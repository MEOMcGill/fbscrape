"""
Analyze scroll request burst patterns from FB network captures.

For each ProfileCometTimelineFeedRefetchQuery (the pagination anchor that
fires once per scroll), look at all other requests within a [-2s, +5s]
window and identify which requests are:
  - page-load only (only fire once at session start)
  - per-scroll recurring (fire near every / most pagination calls)
  - sporadic / engagement-driven (fire occasionally)

Streams JSONL line-by-line; discards response bodies immediately so
session files of hundreds of MB stay tractable. Standard library only.

Run:
    python tmp/hybrid/analyze_scroll_flow.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BATCH_DIR = Path("/Users/mikad/MEOMcGill/fbscrape/data/hybrid/batch_20260429T202827Z")
OUT_MD = Path("/Users/mikad/MEOMcGill/fbscrape/tmp/hybrid/scroll_request_flow.md")

# 3 largest captures (= longest scrape sessions).
SESSION_FILES = [
    "network_20260429T210202Z__12192636156.jsonl",  # 752 MB
    "network_20260429T210652Z__15126126036.jsonl",  # 626 MB
    "network_20260429T210904Z__16156262335.jsonl",  # 460 MB
]

ANCHOR_FRIENDLY = "ProfileCometTimelineFeedRefetchQuery"
WINDOW_BEFORE_S = 2.0
WINDOW_AFTER_S = 5.0


def parse_ts(s: str) -> float:
    # "2026-04-29T20:28:43.612344+00:00" -> epoch float
    return datetime.fromisoformat(s).timestamp()


def parse_form_body(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    try:
        parsed = parse_qs(post_data, keep_blank_values=True)
        return {k: v[-1] for k, v in parsed.items()}
    except Exception:
        return {}


def get_friendly_name(req: dict) -> str | None:
    headers = req.get("headers") or {}
    name = headers.get("x-fb-friendly-name") or headers.get("X-FB-Friendly-Name")
    if name:
        return name
    form = parse_form_body(req.get("post_data"))
    return form.get("fb_api_req_friendly_name")


# CDN / FBCDN URLs vary heavily — bucket them by domain pattern + path prefix.
CDN_PATTERNS = [
    (re.compile(r"^https://scontent-[^.]+\.xx\.fbcdn\.net/v/.*"), "scontent-*/v/* (image CDN)"),
    (re.compile(r"^https://scontent-[^.]+\.xx\.fbcdn\.net/m1/.*"), "scontent-*/m1/* (chunk CDN)"),
    (re.compile(r"^https://video-[^.]+\.xx\.fbcdn\.net/v/.*"), "video-*/v/* (video CDN)"),
    (re.compile(r"^https://static\.xx\.fbcdn\.net/rsrc\.php/.*"), "static.xx.fbcdn.net/rsrc.php (static rsrc)"),
    (re.compile(r"^https://static\.xx\.fbcdn\.net/.*\.svg.*"), "static.xx.fbcdn.net/*.svg"),
    (re.compile(r"^https://static\.xx\.fbcdn\.net/btmanifest/.*"), "static.xx.fbcdn.net/btmanifest"),
    (re.compile(r"^https://www\.facebook\.com/rsrc\.php/.*"), "www.facebook.com/rsrc.php"),
]


def url_bucket(url: str) -> str:
    """Group URLs into stable buckets (strip query, normalize CDN fan-out)."""
    if not url:
        return "<empty>"
    for pat, label in CDN_PATTERNS:
        if pat.match(url):
            return label
    try:
        p = urlparse(url)
        # Drop query string. Drop trailing /<digits>/ id-like segments only for known noisy paths.
        path = p.path
        return f"{p.netloc}{path}"
    except Exception:
        return url


def request_label(rec: dict) -> str:
    """One canonical label combining bucket + friendly-name (for GraphQL)."""
    if rec.get("is_graphql"):
        name = rec.get("_friendly") or "<unknown>"
        return f"GQL {name}"
    rt = (rec.get("request") or {}).get("resource_type", "?")
    bucket = url_bucket(rec.get("url", ""))
    return f"{rt}:{bucket}"


def stream_session(path: Path) -> list[dict]:
    """
    Read a JSONL capture file, returning a list of slim records:
      {ts, url, is_xhr, is_graphql, _friendly, request:{resource_type, post_data?}, response:{status}}

    Drops response.body / response.headers / request.headers (kept only enough
    to extract friendly name).
    """
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            req = rec.get("request") or {}
            ts = rec.get("timestamp")
            if not ts:
                continue
            try:
                ts_f = parse_ts(ts)
            except Exception:
                continue
            friendly = get_friendly_name(req) if rec.get("is_graphql") else None
            slim = {
                "ts": ts_f,
                "url": rec.get("url", ""),
                "is_xhr": bool(rec.get("is_xhr")),
                "is_graphql": bool(rec.get("is_graphql")),
                "_friendly": friendly,
                "request": {"resource_type": req.get("resource_type", "?")},
                "status": (rec.get("response") or {}).get("status"),
            }
            out.append(slim)
    out.sort(key=lambda r: r["ts"])
    return out


def find_anchors(records: list[dict]) -> list[int]:
    """Indices in records where ProfileCometTimelineFeedRefetchQuery fires."""
    return [i for i, r in enumerate(records) if r["is_graphql"] and r["_friendly"] == ANCHOR_FRIENDLY]


def windows_around_anchors(records: list[dict], anchor_indices: list[int]) -> list[list[tuple[float, dict]]]:
    """For each anchor, list neighbor records (with relative dt from anchor) in window."""
    n = len(records)
    out = []
    for ai in anchor_indices:
        a_ts = records[ai]["ts"]
        # walk backward
        lo = ai
        while lo > 0 and (a_ts - records[lo - 1]["ts"]) <= WINDOW_BEFORE_S:
            lo -= 1
        # walk forward
        hi = ai
        while hi < n - 1 and (records[hi + 1]["ts"] - a_ts) <= WINDOW_AFTER_S:
            hi += 1
        window = []
        for j in range(lo, hi + 1):
            r = records[j]
            window.append((r["ts"] - a_ts, r))
        out.append(window)
    return out


def classify_labels(
    all_records: list[dict],
    anchor_indices: list[int],
    windows: list[list[tuple[float, dict]]],
) -> dict:
    """
    Classify every distinct request label into:
      page-load only: appears only before the first anchor
      per-scroll: appears in a window for >=50% of anchors (excluding the
                  anchor itself), and there's >1 anchor
      sporadic: everything else (still appears in some windows but not most)
      unrelated: never appears in any window (e.g., very late post-scroll)

    Also tracks counts per anchor.
    """
    if not anchor_indices:
        return {"per_scroll": {}, "sporadic": {}, "page_load_only": {}, "unrelated": {}}

    first_anchor_idx = anchor_indices[0]
    last_anchor_idx = anchor_indices[-1]

    # Labels seen anywhere
    label_total: Counter = Counter()
    # Labels seen before first anchor
    label_before_first: Counter = Counter()
    # Labels seen after last anchor
    label_after_last: Counter = Counter()
    for i, r in enumerate(all_records):
        lbl = request_label(r)
        label_total[lbl] += 1
        if i < first_anchor_idx:
            label_before_first[lbl] += 1
        if i > last_anchor_idx:
            label_after_last[lbl] += 1

    # Per-anchor occurrences
    per_anchor_counts: dict[str, list[int]] = defaultdict(lambda: [0] * len(windows))
    # Per-anchor relative-time stats (when label first appears in a window)
    per_anchor_first_dt: dict[str, list[float | None]] = defaultdict(lambda: [None] * len(windows))

    for ai_idx, win in enumerate(windows):
        anchor_ts_ref = 0.0  # dt is already relative
        for dt, r in win:
            lbl = request_label(r)
            # Skip the anchor record itself
            if dt == anchor_ts_ref and lbl == f"GQL {ANCHOR_FRIENDLY}":
                continue
            per_anchor_counts[lbl][ai_idx] += 1
            cur_first = per_anchor_first_dt[lbl][ai_idx]
            if cur_first is None or dt < cur_first:
                per_anchor_first_dt[lbl][ai_idx] = dt

    n_anchors = len(windows)

    per_scroll: dict[str, dict] = {}
    sporadic: dict[str, dict] = {}
    page_load: dict[str, dict] = {}
    unrelated: dict[str, dict] = {}

    for lbl, total in label_total.items():
        if lbl == f"GQL {ANCHOR_FRIENDLY}":
            continue
        per_anchor = per_anchor_counts.get(lbl, [0] * n_anchors)
        windows_with = sum(1 for c in per_anchor if c > 0)
        coverage = windows_with / n_anchors if n_anchors else 0.0

        # Mean count per anchor (only counts within windows)
        mean_per_anchor = sum(per_anchor) / n_anchors if n_anchors else 0.0

        # Median first-dt across anchors that saw it
        first_dts = [d for d in per_anchor_first_dt.get(lbl, []) if d is not None]
        first_dts.sort()
        median_first_dt = first_dts[len(first_dts) // 2] if first_dts else None

        info = {
            "total": total,
            "in_windows_total": sum(per_anchor),
            "coverage": coverage,
            "mean_per_anchor": mean_per_anchor,
            "median_first_dt": median_first_dt,
            "before_first_anchor": label_before_first.get(lbl, 0),
            "after_last_anchor": label_after_last.get(lbl, 0),
            "windows_with_at_least_one": windows_with,
        }

        if coverage >= 0.5 and n_anchors >= 5:
            per_scroll[lbl] = info
        elif windows_with == 0:
            # never in any window
            if label_before_first.get(lbl, 0) > 0 and label_after_last.get(lbl, 0) == 0:
                page_load[lbl] = info
            else:
                unrelated[lbl] = info
        else:
            # in some windows, not enough for per-scroll
            # If most occurrences happen before first anchor, treat as page_load
            if label_before_first.get(lbl, 0) >= 0.8 * total:
                page_load[lbl] = info
            else:
                sporadic[lbl] = info

    return {
        "per_scroll": per_scroll,
        "sporadic": sporadic,
        "page_load_only": page_load,
        "unrelated": unrelated,
        "n_anchors": n_anchors,
    }


def representative_burst(windows: list[list[tuple[float, dict]]], n_examples: int = 3) -> list[list[tuple[float, str]]]:
    """Pick a few representative anchor windows (skip first 2 — those tend to
    overlap with page-load chatter — and last 1)."""
    if len(windows) < 6:
        chosen = windows[1 : 1 + n_examples] if len(windows) >= 1 + n_examples else windows
    else:
        # take some from middle of the session
        mid = len(windows) // 2
        chosen = windows[mid : mid + n_examples]
    out = []
    for win in chosen:
        out.append([(dt, request_label(r)) for dt, r in win])
    return out


def fmt_dt(dt: float) -> str:
    sign = "+" if dt >= 0 else "-"
    return f"{sign}{abs(dt):>5.2f}s"


def render_report(per_session: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Scroll request flow — capture analysis")
    lines.append("")
    lines.append(
        "Goal: characterize what requests fire each time `ProfileCometTimelineFeedRefetchQuery` "
        "(the pagination GraphQL) is dispatched during a scrolling profile-timeline scrape. "
        "Each anchor = one scroll-triggered pagination."
    )
    lines.append("")
    lines.append("**Method:** for every `ProfileCometTimelineFeedRefetchQuery` request in the capture, "
                 f"collect all other captured requests within `[-{WINDOW_BEFORE_S:g}s, +{WINDOW_AFTER_S:g}s]` "
                 "of its timestamp. Aggregate across all anchors to find recurring vs. one-off neighbors.")
    lines.append("")
    lines.append("**Sessions analyzed:** the 3 longest captures from "
                 "`data/hybrid/batch_20260429T202827Z/`.")
    lines.append("")
    for sess in per_session:
        lines.append(f"- `{sess['file']}` — {sess['n_records']} records, {sess['n_anchors']} pagination anchors")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Aggregate findings across sessions
    agg_per_scroll: dict[str, list[dict]] = defaultdict(list)
    agg_sporadic: dict[str, list[dict]] = defaultdict(list)
    agg_page_load: dict[str, list[dict]] = defaultdict(list)
    total_anchors = 0
    for sess in per_session:
        c = sess["classified"]
        total_anchors += c["n_anchors"]
        for lbl, info in c["per_scroll"].items():
            agg_per_scroll[lbl].append(info)
        for lbl, info in c["sporadic"].items():
            agg_sporadic[lbl].append(info)
        for lbl, info in c["page_load_only"].items():
            agg_page_load[lbl].append(info)

    # Representative burst — use first session's mid-session example
    lines.append("## Representative scroll burst (relative to anchor t=0)")
    lines.append("")
    for sess in per_session[:1]:
        lines.append(f"From `{sess['file']}` — sample anchor windows from mid-session.")
        lines.append("")
        for ex_i, burst in enumerate(sess["representative_bursts"], 1):
            lines.append(f"### Anchor example {ex_i}")
            lines.append("```")
            lines.append(f"{'time':>9}  {'label':<70}")
            lines.append(f"{'-'*9}  {'-'*70}")
            burst.sort(key=lambda x: x[0])
            for dt, lbl in burst:
                marker = "  <== ANCHOR" if (dt == 0.0 and lbl == f"GQL {ANCHOR_FRIENDLY}") else ""
                lines.append(f"{fmt_dt(dt):>9}  {lbl:<70}{marker}")
            lines.append("```")
            lines.append("")

    # Per-scroll recurring table — aggregated coverage / mean / first-dt
    lines.append("## Per-scroll recurring requests (aggregated across all sessions)")
    lines.append("")
    lines.append(f"Aggregated across **{total_anchors}** anchor windows total.")
    lines.append("")
    lines.append("| Request | Avg per anchor | Coverage* | Median first dt | Notes |")
    lines.append("|---|---:|---:|---:|---|")
    # Sort by mean coverage * mean count
    rows = []
    for lbl, infos in agg_per_scroll.items():
        total_in = sum(i["in_windows_total"] for i in infos)
        total_cov_anchors = sum(i["windows_with_at_least_one"] for i in infos)
        mean_per = total_in / total_anchors if total_anchors else 0.0
        coverage = total_cov_anchors / total_anchors if total_anchors else 0.0
        first_dts = [i["median_first_dt"] for i in infos if i["median_first_dt"] is not None]
        first_dts.sort()
        med_first = first_dts[len(first_dts) // 2] if first_dts else None
        rows.append((lbl, mean_per, coverage, med_first))
    rows.sort(key=lambda r: (-r[2], -r[1]))
    for lbl, mean_per, coverage, med_first in rows:
        first_str = f"{med_first:+.2f}s" if med_first is not None else "—"
        lines.append(f"| `{lbl}` | {mean_per:.2f} | {coverage*100:.0f}% | {first_str} | |")
    lines.append("")
    lines.append("\\* Coverage = fraction of anchor windows that contain at least one of these requests.")
    lines.append("")

    # Sporadic
    lines.append("## Sporadic / engagement-driven (appear in some windows but not most)")
    lines.append("")
    lines.append("| Request | Avg per anchor | Coverage |")
    lines.append("|---|---:|---:|")
    rows = []
    for lbl, infos in agg_sporadic.items():
        total_in = sum(i["in_windows_total"] for i in infos)
        total_cov_anchors = sum(i["windows_with_at_least_one"] for i in infos)
        mean_per = total_in / total_anchors if total_anchors else 0.0
        coverage = total_cov_anchors / total_anchors if total_anchors else 0.0
        rows.append((lbl, mean_per, coverage))
    rows.sort(key=lambda r: -r[2])
    for lbl, mean_per, coverage in rows[:30]:
        lines.append(f"| `{lbl}` | {mean_per:.2f} | {coverage*100:.0f}% |")
    if len(rows) > 30:
        lines.append(f"| ... ({len(rows)-30} more truncated) | | |")
    lines.append("")

    # Page-load only
    lines.append("## Page-load only (fire before first pagination, then never)")
    lines.append("")
    page_load_rows = []
    for lbl, infos in agg_page_load.items():
        total_pre = sum(i["before_first_anchor"] for i in infos)
        page_load_rows.append((lbl, total_pre))
    page_load_rows.sort(key=lambda r: -r[1])
    lines.append("| Request | Total occurrences before first anchor |")
    lines.append("|---|---:|")
    for lbl, total_pre in page_load_rows[:30]:
        lines.append(f"| `{lbl}` | {total_pre} |")
    if len(page_load_rows) > 30:
        lines.append(f"| ... ({len(page_load_rows)-30} more truncated) | |")
    lines.append("")

    # Endpoint focus — bnzai, bulk-route-definitions, etc.
    lines.append("## Focus endpoints")
    lines.append("")
    lines.append("These were called out in the investigation as candidate ambient/telemetry endpoints "
                 "that pure GraphQL replay would miss.")
    lines.append("")
    focus_keys = [
        "/ajax/bulk-route-definitions/",
        "/ajax/bnzai",
        "/ajax/bootloader-endpoint/",
        "/ajax/relay-ef/",
        "/ajax/route-definition/",
    ]
    lines.append("| Endpoint | Per-anchor avg | Coverage | Class |")
    lines.append("|---|---:|---:|---|")
    for key in focus_keys:
        for lbl_dict, klass in [
            (agg_per_scroll, "per-scroll"),
            (agg_sporadic, "sporadic"),
            (agg_page_load, "page-load only"),
        ]:
            for lbl, infos in lbl_dict.items():
                if key in lbl:
                    total_in = sum(i["in_windows_total"] for i in infos)
                    total_cov_anchors = sum(i["windows_with_at_least_one"] for i in infos)
                    mean_per = total_in / total_anchors if total_anchors else 0.0
                    coverage = total_cov_anchors / total_anchors if total_anchors else 0.0
                    lines.append(f"| `{lbl}` | {mean_per:.2f} | {coverage*100:.0f}% | {klass} |")
    lines.append("")

    # Order-within-burst summary
    lines.append("## Typical ordering within a scroll burst")
    lines.append("")
    lines.append("Median first-occurrence dt (relative to anchor at t=0) for top per-scroll requests:")
    lines.append("")
    rows = []
    for lbl, infos in agg_per_scroll.items():
        first_dts = [i["median_first_dt"] for i in infos if i["median_first_dt"] is not None]
        if not first_dts:
            continue
        first_dts.sort()
        med_first = first_dts[len(first_dts) // 2]
        total_cov_anchors = sum(i["windows_with_at_least_one"] for i in infos)
        coverage = total_cov_anchors / total_anchors if total_anchors else 0.0
        rows.append((med_first, lbl, coverage))
    rows.sort(key=lambda r: r[0])
    lines.append("```")
    lines.append(f"{'dt':>8}  {'coverage':>9}  label")
    lines.append(f"{'-'*8}  {'-'*9}  {'-'*60}")
    for med_first, lbl, coverage in rows:
        lines.append(f"{med_first:+8.2f}s  {coverage*100:>7.0f}%   {lbl}")
    lines.append("```")
    lines.append("")

    # Implications
    lines.append("## Implications for Path B")
    lines.append("")
    lines.append("Path B-lite (replay `ProfileCometTimelineFeedRefetchQuery` from inside the live page "
                 "without scrolling) would NOT fire any requests classified as **per-scroll** above. "
                 "Whether Facebook gates on those is unproven — but the per-scroll list is the "
                 "concrete inventory of \"what real scrolling generates that pure replay would not.\"")
    lines.append("")
    lines.append("Page-load-only requests aren't a Path-B problem: they fire once when the page boots "
                 "(which Path B-lite still does via Camoufox), and then never again. ")
    lines.append("")
    lines.append("Sporadic requests are mostly engagement / viewport-driven (image preloads of upcoming "
                 "posts, video thumbnail fetches, hover-triggered route prefetches). Pure replay would "
                 "be silent on these too, but they correlate with content rather than with the scroll "
                 "event itself, so missing them mainly looks like \"user isn't actually viewing posts.\" ")
    lines.append("")
    lines.append("Bulk-route-definitions in particular: see the focus table — its coverage tells us "
                 "whether it's a tight per-scroll signal or just hover-driven noise.")

    return "\n".join(lines)


def main() -> None:
    per_session = []

    for fname in SESSION_FILES:
        path = BATCH_DIR / fname
        if not path.exists():
            print(f"[skip] missing: {path}", file=sys.stderr)
            continue
        print(f"[load] {path.name} ({path.stat().st_size / 1e6:.0f} MB)", file=sys.stderr)
        records = stream_session(path)
        anchors = find_anchors(records)
        print(f"        {len(records)} records, {len(anchors)} ProfileCometTimelineFeedRefetchQuery anchors", file=sys.stderr)
        windows = windows_around_anchors(records, anchors)
        classified = classify_labels(records, anchors, windows)
        rep = representative_burst(windows)
        per_session.append({
            "file": fname,
            "n_records": len(records),
            "n_anchors": len(anchors),
            "classified": classified,
            "representative_bursts": rep,
        })

    md = render_report(per_session)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"[wrote] {OUT_MD}", file=sys.stderr)


if __name__ == "__main__":
    main()
