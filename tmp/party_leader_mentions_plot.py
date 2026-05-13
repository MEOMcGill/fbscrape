"""Biweekly post counts + leader-mention counts for a given party. Saves total + mean plots."""
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import polars as pl

from fbscrape.response import FacebookGraphQLParser

SEEDS = Path("/Users/mikad/MEOMcGill/fbscrape/data/seeds/facebook_politicians.jsonl")
POSTS_DIR = Path("/Users/mikad/MEOMcGill/fbscrape/data/posts/2024-10-01_2026-04-29")
OUT_DIR = Path("/Users/mikad/Documents")
WINDOW_START = datetime(2025, 4, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 4, 29, tzinfo=timezone.utc)
WINDOW_DAYS = 14


def load_seeds(party):
    out = []
    with SEEDS.open() as fh:
        for line in fh:
            s = json.loads(line)["seed"]
            if s.get("Party") == party:
                out.append({"row_id": s["ID"], "handle": s["Handle"]})
    return out


def build_dataframe(seeds, leader_regex):
    parser = FacebookGraphQLParser()
    rx = re.compile(leader_regex, re.IGNORECASE)
    rows = []
    files_with_data = 0
    for s in seeds:
        matches = list(POSTS_DIR.glob(f"{s['row_id']}_{s['handle']}_*.json.gz"))
        if not matches:
            continue
        try:
            recs = json.load(gzip.open(matches[0], "rt")).get("data", []) or []
        except Exception:
            continue
        if not recs:
            continue
        files_with_data += 1
        for rec in recs:
            flat = parser.flatten(rec, endpoint="UserTimeline")
            if not flat:
                continue
            ts = flat.get("created_at")
            if ts is None:
                continue
            text = flat.get("text") or ""
            rows.append({
                "row_id": s["row_id"],
                "handle": s["handle"],
                "ts": datetime.fromtimestamp(ts, tz=timezone.utc),
                "mentions_leader": bool(rx.search(text)),
            })
    df = pl.DataFrame(rows)
    df = df.filter((pl.col("ts") >= WINDOW_START) & (pl.col("ts") < WINDOW_END))
    df = df.with_columns(
        ((pl.col("ts") - pl.lit(WINDOW_START)).dt.total_days() // WINDOW_DAYS).alias("bin_idx")
    )
    # Active = politicians with ≥1 post in the full study window.
    active_pool = set(df["row_id"].unique().to_list())
    n_active = len(active_pool)

    summary = (
        df.group_by("bin_idx")
        .agg(
            pl.len().alias("total_posts"),
            pl.col("mentions_leader").sum().alias("total_leader"),
        )
        .sort("bin_idx")
    )
    summary = summary.with_columns([
        (pl.col("total_posts") / n_active).alias("mean_posts"),
        (pl.col("total_leader") / n_active).alias("mean_leader"),
    ])
    bins = summary["bin_idx"].to_list()
    starts = [(WINDOW_START + timedelta(days=int(i) * WINDOW_DAYS)).date() for i in bins]
    summary = summary.with_columns(pl.Series("window_start", starts))
    return summary, files_with_data, n_active


def plot_pair(summary, party, leader, total_path, mean_path, color, n_active):
    dates = summary["window_start"].to_list()
    total_posts = summary["total_posts"].to_list()
    total_leader = summary["total_leader"].to_list()
    mean_posts = summary["mean_posts"].to_list()
    mean_leader = summary["mean_leader"].to_list()

    # TOTAL plot
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(dates, total_posts, marker="o", linewidth=2, color=color, label=f"All posts by active {party} politicians")
    ax.plot(dates, total_leader, marker="s", linewidth=2, color="#444", label=f'Posts mentioning "{leader}"')
    ax.set_title(f"Total biweekly posts — {party} politicians ({n_active} active) vs. mentions of {leader}")
    ax.set_xlabel("Window start (UTC, 14-day bins)")
    ax.set_ylabel("Number of posts")
    ax.legend(loc="best", frameon=False)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(total_path, dpi=150)
    plt.close(fig)
    print(f"Saved: {total_path}")

    # MEAN plot (per active politician)
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(dates, mean_posts, marker="o", linewidth=2, color=color, label=f"Mean posts per active {party} politician")
    ax.plot(dates, mean_leader, marker="s", linewidth=2, color="#444", label=f'Mean posts mentioning "{leader}"')
    ax.set_title(f"Biweekly mean posts per active {party} politician (n={n_active}) — overall vs. {leader}")
    ax.set_xlabel("Window start (UTC, 14-day bins)")
    ax.set_ylabel(f"Mean posts per active politician (n={n_active})")
    ax.legend(loc="best", frameon=False)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(mean_path, dpi=150)
    plt.close(fig)
    print(f"Saved: {mean_path}")


def run(party, leader_regex, leader_label, color):
    seeds = load_seeds(party)
    summary, n_files, n_active = build_dataframe(seeds, leader_regex)
    print(f"\n=== {party} / {leader_label} ===")
    print(f"Politicians in seeds: {len(seeds)}, with non-empty post files: {n_files}, active in study window: {n_active}")
    pl.Config.set_tbl_rows(40)
    print(summary)
    base = f"{party.lower()}_{leader_label.lower()}"
    plot_pair(
        summary,
        party=party,
        leader=leader_label,
        total_path=OUT_DIR / f"{base}_total.png",
        mean_path=OUT_DIR / f"{base}_mean.png",
        color=color,
        n_active=n_active,
    )


if __name__ == "__main__":
    run("Conservative", r"poilievre", "Poilievre", color="#1f4ea1")
    run("Liberal", r"carney", "Carney", color="#c0392b")
