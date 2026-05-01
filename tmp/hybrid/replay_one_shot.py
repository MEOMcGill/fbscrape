"""
Phase 3: one-shot replay test for ProfileCometTimelineFeedRefetchQuery.

Reads a captured request from a network_*.jsonl, then fires 5 variants of it
directly via `requests`:

  1. baseline       — exact replay (sanity check)
  2. strip_csr      — remove __csr from body (is it server-validated?)
  3. strip_dyn      — remove __dyn from body
  4. count_25       — raise variables.count from 3 to 25 (throughput ceiling)
  5. before_time    — set variables.beforeTime to a past unix timestamp
                      (server-side date filtering check)

For each variant: print HTTP status, response body size, # posts parsed,
end_cursor, and any GraphQL `errors` field in the body.

WARNING: each variant fires ONE real authenticated request from your
machine using the cookies/tokens captured in the JSONL. ~5 requests is
small but non-zero behavioral signal on that account. Keep `--max-variants`
low while debugging.

Run:
    python tmp/hybrid/replay_one_shot.py \\
        data/hybrid/<dir>/network_*.jsonl

Optional:
    --variant <name>      run only one variant
    --max-variants <N>    run at most N (default: all 5)
    --dry-run             print what would be sent, don't actually fire
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import requests


TARGET_FRIENDLY_NAME = "ProfileCometTimelineFeedRefetchQuery"
TARGET_URL = "https://www.facebook.com/api/graphql/"

# Headers that are HTTP/2 pseudo-headers (start with `:`) or that `requests`
# manages itself (host, content-length). These are stripped before sending.
HEADER_DROP = {"host", "content-length", "connection", "accept-encoding"}


def parse_form(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    parsed = parse_qs(post_data, keep_blank_values=True)
    return {k: v[-1] for k, v in parsed.items()}


def get_friendly_name(rec: dict) -> str | None:
    headers = (rec.get("request") or {}).get("headers") or {}
    n = headers.get("x-fb-friendly-name")
    if n:
        return n
    return parse_form((rec.get("request") or {}).get("post_data")).get("fb_api_req_friendly_name")


def find_template_request(jsonl_path: Path) -> dict:
    """Find the first ProfileCometTimelineFeedRefetchQuery in the capture."""
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if get_friendly_name(rec) == TARGET_FRIENDLY_NAME:
                return rec
    raise SystemExit(f"No {TARGET_FRIENDLY_NAME} request found in {jsonl_path}")


def clean_headers(raw: dict[str, str]) -> dict[str, str]:
    """Drop HTTP/2 pseudo-headers and headers managed by `requests`."""
    out = {}
    for k, v in raw.items():
        kl = k.lower()
        if kl.startswith(":"):
            continue
        if kl in HEADER_DROP:
            continue
        out[k] = v
    return out


def build_body(template_form: dict[str, str], modifications: dict) -> str:
    """Apply modifications to the form body, return URL-encoded string.

    Modifications can include:
      - "drop": list of keys to remove
      - "variable_overrides": dict to merge into the parsed `variables` JSON
    """
    body = dict(template_form)

    # Drop specified keys
    for k in modifications.get("drop", []):
        body.pop(k, None)

    # Overlay variables overrides
    overrides = modifications.get("variable_overrides")
    if overrides:
        try:
            variables = json.loads(body.get("variables", "{}"))
        except json.JSONDecodeError:
            variables = {}
        variables.update(overrides)
        body["variables"] = json.dumps(variables, separators=(",", ":"))

    return urlencode(body)


def parse_response(text: str) -> dict:
    """Best-effort parse of FB GraphQL response (might be JSON or JSONL)."""
    out = {"posts": 0, "end_cursor": None, "errors": [], "first_doc": None}
    docs = []
    # Try JSONL first
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            docs = []
            break
    if not docs:
        try:
            docs = [json.loads(text)]
        except json.JSONDecodeError:
            return out
    out["first_doc"] = docs[0]

    # Walk all docs for post_id, end_cursor, errors
    post_ids = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "post_id" and isinstance(v, str):
                    post_ids.add(v)
                elif k in ("end_cursor", "endCursor") and v and not out["end_cursor"]:
                    out["end_cursor"] = v
                elif k == "errors" and isinstance(v, list):
                    out["errors"].extend(v)
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    for d in docs:
        walk(d)
    out["posts"] = len(post_ids)
    return out


def variants_to_run(template_form: dict[str, str]) -> list[tuple[str, dict, str]]:
    """Return [(name, modifications, description)]."""
    yesterday_unix = int((datetime.now(timezone.utc).timestamp())) - 90 * 86400  # 90 days ago

    return [
        ("baseline", {}, "exact replay — sanity check"),
        ("strip_csr", {"drop": ["__csr"]}, "remove __csr — is it server-validated?"),
        ("strip_dyn", {"drop": ["__dyn"]}, "remove __dyn — same question"),
        ("count_25", {"variable_overrides": {"count": 25}}, "raise count from 3 to 25 — throughput ceiling"),
        ("before_time", {"variable_overrides": {"beforeTime": yesterday_unix}},
         f"set beforeTime to {yesterday_unix} (~90 days ago) — server-side date filter"),
    ]


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def run_variant(name: str, headers: dict, body: str, dry_run: bool) -> dict:
    """Fire one variant and return summary dict."""
    if dry_run:
        return {
            "name": name,
            "dry_run": True,
            "body_size": len(body),
            "header_count": len(headers),
        }

    try:
        r = requests.post(TARGET_URL, data=body, headers=headers, timeout=30, allow_redirects=False)
    except Exception as e:
        return {"name": name, "error": f"request failed: {e}"}

    parsed = parse_response(r.text)
    return {
        "name": name,
        "status": r.status_code,
        "body_size": len(r.text),
        "posts": parsed["posts"],
        "end_cursor": parsed["end_cursor"][:30] + "…" if parsed["end_cursor"] and len(parsed["end_cursor"]) > 30 else parsed["end_cursor"],
        "errors": parsed["errors"][:3],  # first 3
        "n_errors": len(parsed["errors"]),
        "body_excerpt": r.text[:300],
        "redirect_to": r.headers.get("location"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", help="Path to a network_*.jsonl capture file.")
    ap.add_argument("--variant", help="Run only one variant by name (e.g. 'baseline').")
    ap.add_argument("--max-variants", type=int, default=None, help="Run at most N variants.")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be sent, don't fire.")
    args = ap.parse_args()

    p = Path(args.jsonl)
    if not p.exists():
        print(f"error: {p} does not exist", file=sys.stderr); sys.exit(1)

    print(f"Loading template from: {p}")
    template = find_template_request(p)
    template_req = template.get("request", {})
    template_form = parse_form(template_req.get("post_data", ""))
    template_headers = clean_headers(template_req.get("headers", {}))

    print(f"Template request:")
    print(f"  captured at:  {template.get('timestamp')}")
    print(f"  doc_id:       {template_form.get('doc_id')}")
    print(f"  fb_dtsg head: {template_form.get('fb_dtsg', '')[:24]}…")
    try:
        v = json.loads(template_form.get("variables", "{}"))
        print(f"  variables:    id={v.get('id')}, count={v.get('count')}, "
              f"cursor={(v.get('cursor') or '')[:30]}…")
    except json.JSONDecodeError:
        pass
    print()

    # Token freshness warning
    try:
        tcap = datetime.fromisoformat(template["timestamp"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - tcap).total_seconds() / 3600
        if age_hours > 18:
            print(f"⚠  Capture is {age_hours:.1f}h old. fb_dtsg rotates ~every 24h — baseline may fail.")
            print()
    except Exception:
        pass

    all_variants = variants_to_run(template_form)
    if args.variant:
        all_variants = [v for v in all_variants if v[0] == args.variant]
        if not all_variants:
            print(f"error: variant '{args.variant}' not found")
            sys.exit(1)
    if args.max_variants:
        all_variants = all_variants[: args.max_variants]

    if args.dry_run:
        print("(--dry-run: variants will not actually fire)\n")

    results = []
    for name, mods, desc in all_variants:
        print(f"{'='*70}\nVariant: {name}\n  {desc}\n{'='*70}")
        body = build_body(template_form, mods)
        r = run_variant(name, template_headers, body, args.dry_run)
        results.append(r)

        if args.dry_run:
            print(f"  body size: {r['body_size']} bytes, headers: {r['header_count']}")
        elif "error" in r:
            print(f"  ERROR: {r['error']}")
        else:
            print(f"  Status:        {r['status']}")
            print(f"  Body size:     {fmt_bytes(r['body_size'])}")
            print(f"  Posts parsed:  {r['posts']}")
            print(f"  end_cursor:    {r['end_cursor'] or '(none)'}")
            print(f"  GraphQL errors:{r['n_errors']}")
            if r["errors"]:
                for err in r["errors"]:
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    print(f"    - {msg[:200]}")
            if r["redirect_to"]:
                print(f"  REDIRECT TO:   {r['redirect_to']}")
            print(f"  Body excerpt:  {r['body_excerpt'][:200]}{'...' if len(r['body_excerpt']) > 200 else ''}")
        print()

    # Summary
    print(f"{'='*70}\nSUMMARY\n{'='*70}")
    print(f"  {'variant':<14} {'status':<8} {'posts':<6} {'errors':<7} note")
    print(f"  {'-'*14} {'-'*8} {'-'*6} {'-'*7} {'-'*40}")
    for r in results:
        if r.get("dry_run"):
            print(f"  {r['name']:<14} (dry-run)")
        elif "error" in r:
            print(f"  {r['name']:<14} {'ERR':<8} {'-':<6} {'-':<7} {r['error'][:40]}")
        else:
            note = ""
            if r["redirect_to"]:
                note = f"redirect → {r['redirect_to'][:50]}"
            elif r["n_errors"]:
                note = f"GraphQL error: {(r['errors'][0].get('message', '') if r['errors'] and isinstance(r['errors'][0], dict) else '')[:40]}"
            elif r["posts"] == 0 and r["status"] == 200:
                note = "200 but no posts — check body excerpt above"
            elif r["status"] == 200:
                note = "OK"
            print(f"  {r['name']:<14} {r['status']:<8} {r['posts']:<6} {r['n_errors']:<7} {note}")


if __name__ == "__main__":
    main()
