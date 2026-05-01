"""
Grep captured JS bundles for __csr / __dyn references to identify which
Facebook bundle(s) generate these form-body tokens.

Method: for each script-resource record in the capture, count literal
occurrences of __csr and __dyn (and a few neighboring request-builder
tokens). Rank bundles by hit density. Print short context windows around
the densest hits so we can recognize the request-builder shape.

Run:
    python tmp/hybrid/find_token_generators.py \
        data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

# Tokens we look for. The first two are the question; the rest are
# co-occurrence signals — a module that builds GraphQL POST bodies will
# typically reference all of them in close proximity.
PRIMARY_TOKENS = ["__csr", "__dyn"]
COOCCURRENCE_TOKENS = ["fb_dtsg", "lsd", "jazoest", "doc_id", "av", "__hs", "__rev", "__spin_r", "__spin_b", "__spin_t", "__hsi"]
ALL_TOKENS = PRIMARY_TOKENS + COOCCURRENCE_TOKENS

CONTEXT_RADIUS = 80  # chars on each side of a hit, for the printed snippet


def iter_records(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_js_record(record: dict) -> bool:
    rt = record.get("request", {}).get("resource_type")
    if rt == "script":
        return True
    url = record.get("url", "")
    return url.endswith(".js") or ".js?" in url


def short_url(url: str) -> str:
    # strip query and host noise; keep the path tail (the bundle hash)
    m = re.search(r"/([^/?#]+\.js)(?:\?|$)", url)
    if m:
        return m.group(1)
    return url[-80:]


def find_hits(body: str) -> dict[str, list[int]]:
    """Return {token: [offsets...]} of literal occurrences in the body."""
    out: dict[str, list[int]] = {}
    for tok in ALL_TOKENS:
        offsets = []
        start = 0
        while True:
            i = body.find(tok, start)
            if i < 0:
                break
            offsets.append(i)
            start = i + len(tok)
        if offsets:
            out[tok] = offsets
    return out


def context_snippet(body: str, offset: int, token: str) -> str:
    lo = max(0, offset - CONTEXT_RADIUS)
    hi = min(len(body), offset + len(token) + CONTEXT_RADIUS)
    snippet = body[lo:hi]
    # collapse whitespace for readable single-line output
    snippet = re.sub(r"\s+", " ", snippet)
    return snippet


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=Path, help="Path to the .jsonl capture file")
    ap.add_argument("--top", type=int, default=10, help="Show this many top bundles (default 10)")
    ap.add_argument("--snippets-per-bundle", type=int, default=4, help="Snippets per top bundle (default 4)")
    args = ap.parse_args()

    if not args.capture.exists():
        print(f"Capture file not found: {args.capture}", file=sys.stderr)
        sys.exit(1)

    # Aggregate: bundle URL -> {token: hit_count}, plus body sample for context.
    bundle_hits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    bundle_body_size: dict[str, int] = {}
    bundle_first_offsets: dict[str, dict[str, int]] = defaultdict(dict)
    bundle_body_sample: dict[str, str] = {}

    js_records_seen = 0
    js_records_with_body = 0

    for rec in iter_records(args.capture):
        if not is_js_record(rec):
            continue
        js_records_seen += 1
        body = rec.get("response", {}).get("body")
        if not body:
            continue
        js_records_with_body += 1
        url = rec.get("url", "")
        # Same bundle URL may appear more than once; aggregate.
        hits = find_hits(body)
        if not hits:
            continue
        for tok, offsets in hits.items():
            bundle_hits[url][tok] += len(offsets)
            if tok not in bundle_first_offsets[url]:
                bundle_first_offsets[url][tok] = offsets[0]
        bundle_body_size[url] = max(bundle_body_size.get(url, 0), len(body))
        # Keep one body sample per URL for snippet extraction.
        if url not in bundle_body_sample:
            bundle_body_sample[url] = body

    print(f"Capture: {args.capture}")
    print(f"JS records seen: {js_records_seen} (with body: {js_records_with_body})")
    print(f"Bundles with at least one token hit: {len(bundle_hits)}")
    print()

    # Score: total primary hits + 0.25 * cooccurrence hits. Tie-break by
    # smaller bundle (denser = more likely the actual generator, not just
    # something importing it).
    def score(url: str) -> float:
        h = bundle_hits[url]
        primary = sum(h.get(t, 0) for t in PRIMARY_TOKENS)
        cooc = sum(h.get(t, 0) for t in COOCCURRENCE_TOKENS)
        return primary + 0.25 * cooc

    ranked = sorted(bundle_hits.keys(), key=lambda u: (-score(u), bundle_body_size.get(u, 0)))

    print("=" * 100)
    print(f"TOP {args.top} BUNDLES BY TOKEN HIT SCORE")
    print("=" * 100)
    print()

    cols = ["score", "size", "csr", "dyn", "fb_dtsg", "lsd", "jazoest", "doc_id", "bundle"]
    print(f"{'rank':>4}  {'score':>6}  {'size':>8}  {'csr':>4}  {'dyn':>4}  {'fb_dtsg':>7}  {'lsd':>3}  {'jazoest':>7}  {'doc_id':>6}  bundle")
    for i, url in enumerate(ranked[: args.top], 1):
        h = bundle_hits[url]
        sz = bundle_body_size.get(url, 0)
        print(
            f"{i:>4}  "
            f"{score(url):>6.1f}  "
            f"{sz:>8}  "
            f"{h.get('__csr', 0):>4}  "
            f"{h.get('__dyn', 0):>4}  "
            f"{h.get('fb_dtsg', 0):>7}  "
            f"{h.get('lsd', 0):>3}  "
            f"{h.get('jazoest', 0):>7}  "
            f"{h.get('doc_id', 0):>6}  "
            f"{short_url(url)}"
        )
    print()

    print("=" * 100)
    print("CONTEXT SNIPPETS AROUND __csr / __dyn FOR TOP BUNDLES")
    print("=" * 100)
    print()

    for i, url in enumerate(ranked[: args.top], 1):
        body = bundle_body_sample.get(url, "")
        if not body:
            continue
        print(f"--- #{i} {short_url(url)}  ({bundle_body_size.get(url, 0)} chars) ---")
        for tok in PRIMARY_TOKENS:
            offsets = []
            start = 0
            while True:
                j = body.find(tok, start)
                if j < 0:
                    break
                offsets.append(j)
                start = j + len(tok)
                if len(offsets) >= args.snippets_per_bundle:
                    break
            for off in offsets:
                snippet = context_snippet(body, off, tok)
                print(f"  [{tok} @ {off}]  …{snippet}…")
        print()


if __name__ == "__main__":
    main()
