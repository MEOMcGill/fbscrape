"""Analyze Poilievre mention rate among Conservative politicians, in 2-week windows since 2025-04-01."""
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from fbscrape.response import FacebookGraphQLParser

SEEDS = Path("/Users/mikad/MEOMcGill/fbscrape/data/seeds/facebook_politicians.jsonl")
POSTS_DIR = Path("/Users/mikad/MEOMcGill/fbscrape/data/posts/2024-10-01_2026-04-29")
WINDOW_START = datetime(2025, 4, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 4, 29, tzinfo=timezone.utc)
WINDOW_DAYS = 14
POILIEVRE_RE = re.compile(r"poilievre", re.IGNORECASE)


def load_conservative_seeds():
    seeds = []
    with SEEDS.open() as fh:
        for line in fh:
            rec = json.loads(line)
            s = rec.get("seed", {})
            if s.get("Party") == "Conservative":
                seeds.append({"row_id": s["ID"], "seed_id": s["SeedID"], "handle": s["Handle"], "seed_name": s.get("SeedName")})
    return seeds


def find_post_file(row_id, handle):
    matches = list(POSTS_DIR.glob(f"{row_id}_{handle}_*.json.gz"))
    return matches[0] if matches else None


def load_records(path):
    with gzip.open(path, "rt") as fh:
        payload = json.load(fh)
    return payload.get("data", []) or []


def main():
    seeds = load_conservative_seeds()
    print(f"Conservative politicians (Party == 'Conservative'): {len(seeds)}")

    parser = FacebookGraphQLParser()
    rows = []
    missing = 0
    empty = 0
    found = 0
    for s in seeds:
        path = find_post_file(s["row_id"], s["handle"])
        if path is None:
            missing += 1
            continue
        records = load_records(path)
        if not records:
            empty += 1
            continue
        found += 1
        for rec in records:
            flat = parser.flatten(rec, endpoint="UserTimeline")
            if not flat:
                continue
            ts = flat.get("created_at")
            text = flat.get("text") or ""
            if ts is None:
                continue
            rows.append({
                "seed_id": s["seed_id"],
                "handle": s["handle"],
                "creation_time": ts,
                "text": text,
            })

    print(f"Post files found: {found}, empty: {empty}, missing: {missing}")
    print(f"Total flattened posts: {len(rows)}")

    df = pl.DataFrame(rows)
    df = df.with_columns(
        pl.from_epoch(pl.col("creation_time"), time_unit="s")
            .dt.replace_time_zone("UTC")
            .alias("ts")
    )

    df_window = df.filter((pl.col("ts") >= WINDOW_START) & (pl.col("ts") < WINDOW_END))
    print(f"Posts in window (>= 2025-04-01, < 2026-04-29): {len(df_window)}")

    # Build 2-week bins anchored at WINDOW_START
    df_window = df_window.with_columns(
        ((pl.col("ts") - pl.lit(WINDOW_START)).dt.total_days() // WINDOW_DAYS).alias("bin_idx")
    )
    df_window = df_window.with_columns(
        pl.col("text").str.contains(r"(?i)poilievre").alias("mentions_poilievre")
    )

    # Per-politician per-window counts
    per_pol = (
        df_window
        .group_by(["bin_idx", "seed_id", "handle"])
        .agg(
            pl.len().alias("n_posts"),
            pl.col("mentions_poilievre").sum().alias("n_poilievre"),
        )
    )

    # Active politicians per window = anyone with >= 1 post.
    # Mean across the active set (the natural interpretation of "mean # of FB posts" per window).
    summary_active = (
        per_pol
        .group_by("bin_idx")
        .agg(
            pl.len().alias("n_active_politicians"),
            pl.col("n_posts").mean().alias("mean_posts_active"),
            pl.col("n_poilievre").mean().alias("mean_poilievre_active"),
            pl.col("n_posts").sum().alias("total_posts"),
            pl.col("n_poilievre").sum().alias("total_poilievre"),
        )
        .sort("bin_idx")
    )

    # Mean across ALL conservative politicians (including those who posted nothing in the window)
    n_total_pol = len(seeds)
    summary_active = summary_active.with_columns([
        (pl.col("total_posts") / n_total_pol).alias(f"mean_posts_all_{n_total_pol}"),
        (pl.col("total_poilievre") / n_total_pol).alias(f"mean_poilievre_all_{n_total_pol}"),
    ])

    # Attach window start dates
    summary_active = summary_active.with_columns(
        pl.col("bin_idx").map_elements(
            lambda i: (WINDOW_START + pl.duration(days=int(i * WINDOW_DAYS)).cast(pl.Datetime("us", "UTC"))),
            return_dtype=pl.Datetime("us", "UTC"),
        ).alias("window_start_calc")
    ) if False else summary_active  # quick approach below

    # Add window date columns (manual computation)
    bins = summary_active["bin_idx"].to_list()
    starts = [(WINDOW_START.replace(tzinfo=None) + (i * __import__("datetime").timedelta(days=WINDOW_DAYS))).date() for i in bins]
    ends = [(WINDOW_START.replace(tzinfo=None) + ((i + 1) * __import__("datetime").timedelta(days=WINDOW_DAYS))).date() for i in bins]
    summary_active = summary_active.with_columns([
        pl.Series("window_start", [s.isoformat() for s in starts]),
        pl.Series("window_end_excl", [e.isoformat() for e in ends]),
    ]).select([
        "window_start", "window_end_excl", "bin_idx",
        "n_active_politicians", "mean_posts_active", "mean_poilievre_active",
        f"mean_posts_all_{n_total_pol}", f"mean_poilievre_all_{n_total_pol}",
        "total_posts", "total_poilievre",
    ])

    pl.Config.set_tbl_rows(100)
    pl.Config.set_tbl_cols(20)
    pl.Config.set_float_precision(3)
    print(summary_active)

    out_csv = Path("/Users/mikad/MEOMcGill/fbscrape/tmp/poilievre_biweekly.csv")
    summary_active.write_csv(out_csv)
    print(f"Saved: {out_csv}")


if __name__ == "__main__":
    main()
