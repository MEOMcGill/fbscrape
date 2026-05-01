"""
Extract every JS body from a network capture into individual .js files.

Why: PyCharm and most editors choke on the raw JSONL (lines >1 MB and JSON-
escaped quotes). Splitting out one .js file per bundle lets you use normal
"Find in Files" search to navigate Facebook's bundles.

Run:
    python tmp/hybrid/explode_js_bodies.py \
        data/hybrid/FilomenaTassi_20260429T200954Z/network_20260429T201407Z_3302009288.jsonl

Output: a sibling directory `js_bundles/` containing one .js per unique bundle URL.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <capture.jsonl> [output_dir]", file=sys.stderr)
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"Capture not found: {src}", file=sys.stderr)
        sys.exit(1)

    out = Path(sys.argv[2]) if len(sys.argv) >= 3 else src.parent / "js_bundles"
    out.mkdir(exist_ok=True)

    seen: dict[str, int] = {}
    written = 0

    with src.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("request", {}).get("resource_type") != "script":
                continue
            body = rec.get("response", {}).get("body")
            if not body:
                continue
            url = rec.get("url", "")
            m = re.search(r"/([^/?#]+)\.js", url)
            base = (m.group(1) if m else f"unknown_{i}")[:80]
            # dedupe: same URL captured more than once -> keep first only
            if base in seen:
                continue
            seen[base] = i
            (out / f"{base}.js").write_text(body)
            written += 1

    print(f"Wrote {written} JS bundle(s) to {out}")
    print("Open the directory in your editor and use 'Find in Files' to search.")


if __name__ == "__main__":
    main()
