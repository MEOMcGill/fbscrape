"""
Probe whether Camoufox's headless modes are detectable by anti-bot fingerprinters.

Visits two sites in the same browser session:
  - creepjs (https://abrahamjuliot.github.io/creepjs/) — async fingerprinter,
    settles over ~15s, exposes a "trust score" + a list of "lies".
  - bot.sannysoft.com — synchronous table of pass/fail rows for classic
    headless / webdriver / chrome-runtime / permissions checks.

Usage:
    python tmp/test_creepjs_headless.py --headless virtual
    python tmp/test_creepjs_headless.py --headless false
    python tmp/test_creepjs_headless.py --headless true

Outputs (in tmp/creepjs_results/<mode>_<ts>/<probe_name>/):
    page.html          - rendered DOM after the probe finishes
    page.png           - full-page screenshot
    summary.json       - JS-evaluated extracts (probe-specific)

Run all three modes and diff the summary.json files to see what each probe
picks up. The HTML dumps are the source of truth; the screenshots and
summaries are conveniences.
"""

import argparse
import asyncio
import json
import os
from datetime import datetime

from camoufox.async_api import AsyncCamoufox

from fbscrape.utils import get_home_dir_path, get_device_os
from fbscrape.logger import logger


# Per-probe config. `settle_selector` is what we wait for before dumping —
# something the probe creates only after its detectors have run. `extract_js`
# is a JS expression returning a probe-specific summary object; the wrapper
# adds shared `navigator.*` fields.
PROBES = [
    {
        "name": "creepjs",
        "url": "https://abrahamjuliot.github.io/creepjs/",
        # creepjs builds its UI piece by piece; wait for one of these late-
        # rendering containers, then add a grace sleep so tail-end detectors
        # finish.
        "settle_selector": ".headless-rating, .like-headless-rating, .stealth-rating",
        "settle_timeout_ms": 30_000,
        "grace_sleep_seconds": 5,
        "extract_js": """
            () => {
              const firstLine = (sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                // creepjs ratings nest big CSS blocks; grab only the first line.
                return el.textContent.trim().split('\\n')[0].trim();
              };
              const allText = (sel) => Array.from(document.querySelectorAll(sel)).map(e => e.textContent.trim());
              const body = (document.body && document.body.innerText || '');
              // Pull the headless block out of body text. It looks like:
              //   Headlesscd81633b
              //   chromium: false
              //   6% like headless: 7bca6f28
              //   0% headless: 52defe05
              //   0% stealth: 0c019315
              //   ...
              const m = body.match(/Headless[a-f0-9]{8}\\s*\\n([\\s\\S]{0,400})/);
              const headless_section = m ? m[1].trim() : null;
              const ratings = {
                headless: firstLine('.headless-rating'),
                like_headless: firstLine('.like-headless-rating'),
                stealth: firstLine('.stealth-rating'),
              };
              const fp_match = body.match(/FP ID:\\s*([a-f0-9]+)/);
              return {
                fingerprint_id: fp_match ? fp_match[1] : null,
                ratings: ratings,
                headless_section: headless_section,
                lies: allText('.lies li, .lies-list li').slice(0, 50),
                body_text_first_3k: body.slice(0, 3000),
              };
            }
        """,
    },
    {
        "name": "sannysoft",
        "url": "https://bot.sannysoft.com/",
        # sannysoft renders synchronously; the result table is in the DOM by
        # `domcontentloaded`. A short wait is enough for the JS-driven row
        # classes (passed/failed/warn) to apply.
        "settle_selector": "#fp-table, #fp2-table, table",
        "settle_timeout_ms": 15_000,
        "grace_sleep_seconds": 15,
        # sannysoft renders multiple plain <table> elements; rows look like
        # `<tr><td>Name</td><td class="passed|failed result">value</td></tr>`.
        # No table ids - just walk every <tr> with two <td>s.
        "extract_js": """
            () => {
              const rows = Array.from(document.querySelectorAll('tr')).map(tr => {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 2) return null;
                const cls = tds[1].className.trim();
                let status = 'unknown';
                if (cls.includes('failed')) status = 'failed';
                else if (cls.includes('warn')) status = 'warn';
                else if (cls.includes('passed')) status = 'passed';
                return {
                  name: tds[0].textContent.trim().replace(/\\s+/g, ' '),
                  value: tds[1].textContent.trim().replace(/\\s+/g, ' '),
                  status,
                  raw_class: cls,
                };
              }).filter(Boolean);
              return {
                rows,
                counts: {
                  passed: rows.filter(r => r.status === 'passed').length,
                  failed: rows.filter(r => r.status === 'failed').length,
                  warn: rows.filter(r => r.status === 'warn').length,
                  unknown: rows.filter(r => r.status === 'unknown').length,
                },
                body_text_first_4k: (document.body && document.body.innerText || '').slice(0, 4000),
              };
            }
        """,
    },
]


def _parse_headless(value: str):
    """Map --headless string to the value Camoufox expects."""
    v = value.lower()
    if v in ("false", "0", "no"):
        return False
    if v in ("true", "1", "yes"):
        return True
    if v == "virtual":
        return "virtual"
    raise argparse.ArgumentTypeError(f"invalid headless value: {value!r}")


def _navigator_summary_js() -> str:
    """Shared navigator/window/screen extract, wrapped around each probe's extract."""
    return """
        () => ({
          page_title: document.title,
          url: location.href,
          user_agent: navigator.userAgent,
          webdriver: navigator.webdriver,
          languages: navigator.languages,
          platform: navigator.platform,
          hardware_concurrency: navigator.hardwareConcurrency,
          device_memory: navigator.deviceMemory ?? null,
          viewport: { width: window.innerWidth, height: window.innerHeight },
          screen: { width: screen.width, height: screen.height, colorDepth: screen.colorDepth },
        })
    """


async def _run_probe(page, probe: dict, out_dir: str):
    probe_dir = os.path.join(out_dir, probe["name"])
    os.makedirs(probe_dir, exist_ok=True)
    logger.info(f"[{probe['name']}] navigating to {probe['url']}")
    await page.goto(probe["url"], wait_until="domcontentloaded")

    try:
        await page.wait_for_selector(
            probe["settle_selector"], timeout=probe["settle_timeout_ms"]
        )
        logger.info(f"[{probe['name']}] settle selector found, sleeping for grace period")
    except Exception as e:
        logger.warning(
            f"[{probe['name']}] settle selector not found ({e}); falling back to fixed wait"
        )
    await asyncio.sleep(probe["grace_sleep_seconds"])

    # HTML dump.
    html = await page.content()
    html_path = os.path.join(probe_dir, "page.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info(f"[{probe['name']}] saved HTML ({len(html)} chars) -> {html_path}")

    # Screenshot.
    png_path = os.path.join(probe_dir, "page.png")
    await page.screenshot(path=png_path, full_page=True)
    logger.info(f"[{probe['name']}] saved screenshot -> {png_path}")

    # Structured summary (shared navigator + probe-specific extract).
    nav_summary = await page.evaluate(_navigator_summary_js())
    probe_summary = await page.evaluate(probe["extract_js"])
    summary = {**nav_summary, probe["name"]: probe_summary}

    summary_path = os.path.join(probe_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    logger.info(f"[{probe['name']}] saved summary -> {summary_path}")

    # One-line breadcrumb so each probe shows up in the run log.
    if probe["name"] == "creepjs":
        ratings = probe_summary.get("ratings") or {}
        logger.info(
            f"[creepjs] headless={ratings.get('headless')!r} "
            f"like_headless={ratings.get('like_headless')!r} "
            f"stealth={ratings.get('stealth')!r} "
            f"webdriver={nav_summary.get('webdriver')!r}"
        )
    elif probe["name"] == "sannysoft":
        rows = probe_summary.get("rows") or []
        counts = probe_summary.get("counts") or {}
        failed = [r["name"] for r in rows if r.get("status") == "failed"]
        warn = [r["name"] for r in rows if r.get("status") == "warn"]
        logger.info(
            f"[sannysoft] passed={counts.get('passed')} failed={counts.get('failed')} "
            f"warn={counts.get('warn')} failed_names={failed[:8]} warn_names={warn[:8]}"
        )


async def main(headless_arg: str, out_root: str):
    headless = _parse_headless(headless_arg)
    current_os = get_device_os()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = headless_arg.lower()
    out_dir = os.path.join(out_root, f"{label}_{ts}")
    os.makedirs(out_dir, exist_ok=True)
    logger.info(f"Output directory: {out_dir}")
    logger.info(f"Camoufox: headless={headless!r}, os={current_os}")

    async with AsyncCamoufox(headless=headless, os=current_os) as browser:
        context = await browser.new_context()
        page = await context.new_page()
        # Same workaround for camoufox issue #473 used in create_account.py.
        await page.set_extra_http_headers({"Accept-Encoding": "gzip, deflate"})

        for probe in PROBES:
            try:
                await _run_probe(page, probe, out_dir)
            except Exception as e:
                logger.error(f"[{probe['name']}] probe failed: {e}")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Probe Camoufox headless detection via creepjs + sannysoft")
    parser.add_argument(
        "--headless",
        "-H",
        default="virtual",
        help="Camoufox headless mode: false, true, or virtual (default: virtual)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=os.path.join(get_home_dir_path(), "tmp", "creepjs_results"),
        help="Root directory for results (a per-run subdir is created)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.headless, args.output_dir))
