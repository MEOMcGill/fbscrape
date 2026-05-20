# Tests

Three-tier test suite organized by speed + dependency. Default `pytest` runs
only the fast tier; the slow tiers are opt-in via marker.

```
tests/
├── unit/         # no network, no browser — runs on every commit
├── integration/  # headless browser hitting real Facebook (needs an account)
├── e2e/          # full CLI subprocess invocations (needs an account)
└── fixtures/
    └── scraping_results/   # fresh captures the unit tests load
```

## One-time setup

Install dev deps and capture fixtures. The capture script scrapes `zuck`
(UserTimeline manual + hybrid, Search), one public group (GroupTimeline),
Meta's page (PageTransparency), and zuck's profile (ProfileAuthenticity)
into `tests/fixtures/scraping_results/`. Re-run any time fixtures look stale.

```bash
pip install -e '.[dev]'
python tests/_capture_fixtures.py                 # captures all 6 fixtures
python tests/_capture_fixtures.py --only user_timeline_hybrid   # just one
```

The capture needs an active account in `db/accounts.db` (see
`fbscrape account add` / `fbscrape login`). Unit tests skip individually
when their fixture file is missing, so a partial capture is OK.

## Running

| Command | Runs |
| --- | --- |
| `pytest` | unit tier only (default — fast, no network) |
| `pytest -m integration` | headless scrapes against real Facebook |
| `pytest -m e2e` | full CLI scrape → flatten / download-media |
| `pytest -m ''` | everything (empty marker selector overrides default) |
| `pytest tests/unit/test_query_registry.py` | one file |
| `pytest -k flatten` | every test with "flatten" in the name |

Integration and e2e tests **skip automatically** when no active account
is available in `db/accounts.db`, so CI without credentials silently ignores
them rather than failing.

## What each tier covers

**unit/** — pure functions, fast. Pins the user-facing API contract:
- `Query` / `ENDPOINT_REGISTRY` validation
- `ScrapingResult.save()` round-trip (plain + gzipped) and legacy `"posts"` key compatibility
- `FacebookGraphQLParser.flatten(record, endpoint)` for all three endpoints
- CLI `--input-file` parsing across csv/parquet/json/jsonl/yaml
- CLI `flatten` routing (file/dir/concat/--format all)

**integration/** — one test per supported `(endpoint, mode)`. All headless,
tight windows, real FB. Each asserts the scrape returns `result.result` in
the success set, `len(data) > 0`, and that every returned record flattens.

**e2e/** — CLI smoke. Runs `python -m fbscrape.cli` as a subprocess, scrapes
zuck for a one-month window, then flattens (test 1) or downloads media
(test 2). Confirms wire-format compatibility between the scrape output and
the post-processing commands.

## Adding tests when adding an endpoint

1. Add a new entry to `TARGETS` and `CAPTURERS` in `tests/_capture_fixtures.py`.
2. Re-run `python tests/_capture_fixtures.py --only <new_endpoint>`.
3. Add `tests/unit/test_flatten_<new_endpoint>.py` modeled on the existing
   single-shot ones (page_transparency / profile_authenticity).
4. Add `tests/integration/test_<new_endpoint>.py` mirroring the matching
   integration test.
5. Update `EXPECTED_KEYS` in `tests/unit/test_query_registry.py`
   (`test_endpoint_registry_top_level_keys_pinned`) — that test is
   intentionally a tripwire on adding/removing endpoints.
