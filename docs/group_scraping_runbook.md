# Field runbook: scraping Facebook groups

How to run a group collection, and what to do in the event that something
interrupts it. Read [`endpoints/group_timeline.md`](endpoints/group_timeline.md)
first for the mechanics — what the endpoint is, what the flags mean. This
document is the *operational* layer: how to set a run up, what the recovery
paths are if you need them, and what evidence backs each default.

**A group scrape is expected to finish.** Most of what follows is contingency
documentation. The failure modes described here were real and common in a
May 2026 collection run over 49 political groups (~5.2 GB compressed) — and
that run is precisely why the tool now handles them itself:

| Landed since | What it removed |
|---|---|
| Chunk-path-aware cursor extraction | The "poison cursor" that made resumes fail outright (§6) |
| One-post-per-line JSONL + append-only resume | The out-of-memory failures on resume (§7) |
| Write-on-parse streaming | Losing an in-flight scrape's posts to a kill (§7) |
| Automatic cursor unstick | Hand-repairing a stalled resume (§5) |
| `TOP_POSTS` as the default sort | The account-suspension exposure of `CHRONOLOGICAL` (§2) |

So read §0–§3 to set a run up correctly. Read §4 onward when something actually
goes wrong — or when you want to understand what the tool is doing on your
behalf. The failure census in §9 is the *before* picture, kept because it
explains why the safeguards exist and what their symptoms look like.

---

## 0. Running a collection

The whole batch is one command. Targets come from a CSV whose `handle` column
holds a vanity handle or a numeric group id:

```bash
fbscrape scrape group-timeline \
    --input-file data/groups.csv \
    --output-dir data/group_posts \
    --start-date 2020-01-01 \
    --sorting-setting TOP_POSTS \
    --max-posts 10000 \
    --max-consecutive-out-of-range 20 \
    --max-sessions 2 --headless --wait-for-account
```

Every flag there is deliberate; §2 and §3 explain the sort and the two
termination bounds, which are the choices that matter most.

**Then check the result.** Not because you expect trouble, but because on a
large batch a handful of targets legitimately come back short — a group went
private, an account hit a 24-hour lock, a feed degraded partway:

```bash
python tmp/scrape_recap.py data/groups.csv data/group_posts
```

**If some targets are short,** re-run just those with `--continue` and each one
picks up from where it stopped rather than starting over:

```bash
fbscrape scrape group-timeline \
    --input-file data/groups_backlog.csv \
    --output-dir data/group_posts \
    --start-date 2020-01-01 --sorting-setting TOP_POSTS --max-posts 10000 \
    --continue --wait-for-account
```

That is the entire loop. In the rare case a resume can't make progress on its
own, §5 and §10 cover it.

---

## 1. Why groups are harder than pages

Five structural differences explain the defaults and the recovery paths below.
None of them apply to `UserTimeline`, so habits carried over from page scraping
can mislead here.

| # | Group feeds… | Consequence |
|---|---|---|
| 1 | have **no server-side date filter**. `GroupsCometFeedRegularStoriesPaginationQuery` (GCFRSPQ) carries no `beforeTime`/`afterTime` variable, unlike `UserTimeline`'s PCTFRQ. | `--start-date` / `--end-date` are *client-side advice only*. You cannot ask FB for a window; you paginate until a client-side stop condition fires. This also kills multi-leg `cursor_reset` recovery, which works by advancing `end_date` — so on groups a `cursor_reset` is terminal. |
| 2 | **don't fire GCFRSPQ on navigation.** | A bootstrap scroll is required before a request template can be captured (`_hybrid_bootstrap`). If it fails you get `template_capture_timeout` — 3 of our 49 group files ended this way. |
| 3 | are **not a clean chronological list.** FB injects a "highlight" post at the bootstrap edge of each batch, out of chronological position. | Naive "oldest post in this batch is older than start_date → stop" fires early and wrongly. Hence: the stop is exempted on iteration 1, the cursor-reset detector anchors on the **2nd-oldest** post, and the unstick heuristic skips the rank-1 post. |
| 4 | **can degrade under long pagination.** After a variable number of replays FB may start returning `field_exception` errors, or silently serve a degraded shape with a fresh cursor that jumps to newer posts (cursor reset). | Deep runs on very large groups are not guaranteed to complete in one invocation, so resumability is built in rather than bolted on. On a group of ordinary size this rarely comes up. |
| 5 | are **big.** A single active group produced 400–475 MB gzipped raw JSON. | The scrape path handles this by streaming, but anything *you* write that loads a whole file into memory (`flatten --concat`, a naive `json.load`) will still run out of memory. |

---

## 2. Sort: use `TOP_POSTS`, not `CHRONOLOGICAL`

`--sorting-setting CHRONOLOGICAL` gives you the closest thing to true creation
order and is the intuitive choice. We started there. It correlates with **account
suspensions** on this endpoint, and the runs themselves went badly:

| First batch, `CHRONOLOGICAL` (11 groups) | count |
|---|---|
| `graphql_error: field_exception` | 4 |
| `template_capture_timeout` | 3 |
| `rate_limit` (account locked 24 h) | 1 |
| `content not available` | 1 |
| `no_new_posts_streak` | 1 |
| `max_posts_reached` (clean) | 1 |

Re-run with the default `TOP_POSTS`, the same groups mostly terminated on a real
date-tail stop (`consecutive_out_of_range`) or ran to the requested depth.
`TOP_POSTS` is FB's own UI default, so it is also the lowest-fingerprint choice.

**What changes when you switch sort:** posts stop arriving monotonically, so the
chronological stop conditions are dropped (`assemble_default_stop_conditions`
only adds `OldestInBatchBelowStartDate` + `CursorReset` for
`GroupTimeline` + `CHRONOLOGICAL`). Termination then rests on
`--max-consecutive-out-of-range` (default 20) plus `--max-posts`. Don't leave
both unset on a non-chronological sort with no dates — nothing bounds the run
except `--max-paginations`.

---

## 3. Termination: bound every run explicitly

Because there is no server-side window, a group scrape ends only when *you* say
so. What we used, and what it means:

- `--max-posts N` — the workhorse. Worth a small value (`100`) on a first
  smoke run against a new batch, then the depth you actually want; ours ended at
  `10000`. Checked at batch boundaries, so the file can overshoot by up to
  `pagination_count - 1`.
- `--max-consecutive-out-of-range 20` — the date-tail stop under `TOP_POSTS`:
  bail after 20 posts in a row outside `[start_date, end_date]`. This is what
  most of our finished files terminated on (`consecutive_out_of_range` — 33 of
  49 files across the two batches). No-op if you pass no dates.
- `--max-no-progress-streak 30` — backstop for "FB is serving only posts we
  already have". On a resumed leg it doubles as the signal that the cursor needs
  a deeper anchor, which the tool then applies automatically — see §5.
- `--max-paginations` — hard safety cap; we left it off.

A capped run is not a truncated dataset: you re-enter with `--continue` and the
cap applies to the *new leg*. Set the cap to the depth you actually want; there
is no need to keep it artificially small unless you are deliberately spreading a
very large group across sessions.

---

## 4. Resuming and topping up: `--continue`

Two uses, one mechanism. The routine one is **incremental collection**: re-run a
target weeks later and pick up only what's new instead of re-scraping the whole
feed. The contingency one is **recovery**: if a run was interrupted — a lock, a
degraded feed, a killed process — the answer to "do I start over?" is no.

```bash
fbscrape scrape group-timeline <handle> --output-dir <dir> --continue
```

What `--continue` does, per target:

1. Finds the prior output for that stem (`<handle>_GroupTimeline_hybrid.jsonl.gz`,
   falling back to legacy `.json.gz` / `.json`). **Stems carry no dates** — one
   file per (handle, endpoint, mode) is a rolling archive across runs, which is
   what makes resume match after you change the date args.
2. Reads that file's **`last_cursor`** and starts pagination there instead of
   `cursor=null`.
3. Seeds the interceptor's dedup set with the prior file's `post_id`s, so the
   bootstrap edge can't re-add posts you already have.
4. Runs the leg, then **appends** the new posts to the same file as a new gzip
   member (never rewrites it — see §7).

Two things to know:

- **`--continue` and `--skip-existing` are mutually exclusive** by design (one
  drops targets that have output, the other resumes them). The CLI refuses both.
- If `last_cursor` is `null` (the prior scrape reached end-of-feed cleanly),
  resume is a no-op and a fresh scrape runs.

**Does splitting a scrape across legs cost you posts?** No — checked
explicitly (`tmp/continue_experiment/`): one `--max-posts 100` run vs. five
`--max-posts 20 --continue` legs on the same group, same day.

```
|A| = 102   |B| = 105   |A ∩ B| = 102   |A − B| = 0   Jaccard = 0.971
```

The three extras in the legged run were bootstrap-edge highlights and posts
created during the experiment; nothing was missed. **Verdict: equivalent.** So
resuming is safe whenever you need it, and so is deliberately splitting a very
large group across sessions.

---

## 5. If a resume can't make progress: re-anchor on a post's cursor

Mostly automatic now, and worth knowing about mainly so the log line makes sense
when you see it.

**The problem.** `last_cursor` is ephemeral server-side state, so a resume can
occasionally land on an anchor Facebook won't paginate from. Two symptoms:

| Symptom | Meaning |
|---|---|
| A resumed leg dies with `graphql_error: field_exception` at **pagination 1** | the saved cursor is invalid — FB rejects it outright. Historically this was the poison-cursor bug (§6), now fixed at the source; 4 of our 54 forensic dumps were this |
| A resumed leg returns `no_new_posts_streak` with ~0 new posts | the cursor is *valid* but anchored where FB only serves posts already in the dedup seed — a **dedup wall**. Handled automatically; see below |

**The fix.** Every post record in a saved scrape carries its own per-edge
`cursor`. Those cursors are also valid pagination anchors for the same
connection. So: throw away the file-level `last_cursor` and re-anchor on the
cursor of a post *deeper* in the file.

```bash
fbscrape unstick-cursor <file>...            # swap last_cursor to a deeper anchor
    --rank N          # anchor at the Nth chronologically-oldest cursored post (default 3)
    --only-if-stuck   # only touch files whose result == "no_new_posts_streak"
    --dry-run         # show the swap, write nothing
```

On a JSONL file this appends a status-only line (`data: null`) carrying the new
cursor, so the next `--continue` tail-read picks it up; no rewrite of a
400 MB file. On a legacy envelope it rewrites `last_cursor` in place.

**Why rank 3 and not rank 1** (`_find_unstick_cursor`, `cli.py:29`): rank 1 is
often the bootstrap-edge highlight outlier, i.e. not really the oldest post; and
roughly every third saved record has a null `cursor` (a fan-out artifact), so the
picker walks forward from rank 3 to the next cursored post. Raise `--rank` to
jump deeper if a swap still lands inside the dedup wall.

**This is the automatic path.** When a resumed leg returns
`no_new_posts_streak`, `cli._append_unstick_line` performs the rank-3 swap on the
merged file itself and logs `auto-unstuck cursor to rank #N` — the next leg
simply works. You should not normally need the command at all. Reach for it in
three cases: a legacy-format file, a batch repair across many files, and the
`field_exception`-on-iteration-1 case — where you must *omit* `--only-if-stuck`,
since it skips anything whose result isn't `no_new_posts_streak`.

**Do per-post cursors actually still work later?** Yes, verified
(`tmp/cursor_validity_experiment/`): five cursors taken from positions 0/20/40/60/80
of yesterday's file, each used to resume and pull the next 20 posts.

```
run   idx   result              new  matched  newer-than-window
run_a   0   max_posts_reached    21   20/20    0
run_b  20   max_posts_reached    21   20/20    0
run_c  40   max_posts_reached    21   19/20    0
run_d  61   max_posts_reached    21   20/20    0
run_e  80   max_posts_reached    21   20/20    0
                        totals:  99/100 (99%), zero contamination
```

Saved per-post cursors were still valid ~24 h later and FB served the same
sequence after them. That result is what licenses the whole unstick strategy —
and the ~24 h figure is also its expiry warning: don't expect a month-old cursor
to anchor.

---

## 6. The poison 90-char Reels cursor *(fixed — historical)*

A resolved bug, documented because you may still meet its fingerprint in files
saved before the fix. It silently poisoned 14 saved files in May 2026.

**Root cause** (KDD 21): FB's `@stream`/`@defer` responses are multi-chunk, and
each chunk has a `path`. The page-level pagination cursor lives in the
**shortest-path** chunk. When a batch contains a Reel, the Reels attachment's
deferred chunk — with its own `end_cursor`, at a deeper path — *arrives first*.
The old first-match extractor grabbed that one. Saved `last_cursor` was then a
90-character Reels sub-stream cursor (real page-level cursors are ~159–508
chars), and every `--continue` against those files blew up with
`field_exception` on iteration 1.

**Fixed forward** in `_hybrid_extract_end_cursor` (shortest-path chunk wins; a
falsy cursor there is the legitimate end-of-feed signal). New scrapes cannot
land in this state.

**Repairing already-saved files** was a one-off:
`fbscrape/tmp/unpoison_cursors.py` — scans a directory for `last_cursor` of
exactly 90 chars and swaps in the rank-3 per-edge cursor, mirroring
`_find_unstick_cursor` so the result matches what `unstick-cursor` would do.

> ⚠️ **If your checkout predates 2026-08-31, that script silently does nothing.**
> A bad global rename had left it reading `d.get("data-twitter")` instead of
> `d.get("data")` — so it reported every file as "poison cursor but no rank-3
> cursored post". Fixed (along with the same typo in
> `tmp/cursor_validity_experiment/{setup,analyze}.py`,
> `tmp/continue_experiment/compare.py`, `tmp/open_json_gz.py`, and 13
> `tmp/hybrid/` scripts, where it had also corrupted output paths to
> `data-twitter/hybrid/`). Verified against real records afterwards: a poisoned
> file swaps to a 159-char rank-3 per-edge cursor.

For anything other than the historical 90-char case, prefer the supported path:
`fbscrape unstick-cursor <files>` — and omit `--only-if-stuck` there, since a
poisoned file's `result` is a `graphql_error`, not `no_new_posts_streak`.

Diagnostic shortcut: `len(last_cursor) == 90` ⇒ poisoned. 159+ ⇒ a real
page-level cursor. (Per-edge post cursors in our files are 159 chars, and only
about two in three records carry one at all — 9 of 12 in a spot check — which is
why the rank picker walks forward to the next cursored post.)

---

## 7. Durability: write-on-parse, and why the format changed

Two properties that make an interruption a non-event rather than a lost run:

**Posts are written as they are parsed.** With a stream path set, each deduped
post goes straight to a JSONL file via `JsonlPostWriter` with autoflush. So if a
run is ever cut short — `Ctrl-C`, a wall-clock guard, a closed laptop — every
post parsed up to that moment is already durably on disk. In-memory accumulation
would have lost all of it.

**`--continue` appends; it never loads the prior file.** This is why the on-disk
format is one-post-per-line `.jsonl.gz` (KDD 24). The previous whole-file
envelope format is what these group scrapes broke: 300–400 MB gzip inflated to
8–15 GB of live Python objects during a resume merge, RSS hit **46 GiB** and the
process was killed (`CONSTRAINT_MEMCG`, 2026-06-03). Now a leg is an O(new-leg)
append of a new gzip member, and resume reads only the JSONL **tail**
(`read_resume_tail`, last ~150 lines → `last_cursor` + recent `post_id`s).

**Legacy files still exist.** The 2026-05 group dirs are whole-file `.json.gz`
envelopes. Consequences when you touch them:

- `--continue` migrates one to JSONL automatically on first resume (self-healing),
  or convert a corpus up front: `fbscrape utils convert-to-jsonl <dir>`.
- Reading them: use `jsonl_store.load_scrape_file` (handles both formats), or
  stream with ijson. Note that in an envelope the top-level `last_cursor` is
  written **after** the whole `data` array, so you cannot cheaply peek at it —
  you must stream the entire file. Another reason not to keep the old format.
- `flatten --concat` over files this size will run out of memory; we wrote a
  streaming flatten for exactly this (see §12).

---

## 8. Batch bookkeeping: backlog CSVs + `scrape_recap.py`

On a batch of any size, a few targets will come back short for reasons that have
nothing to do with the scraper — a group went private, an account was locked, a
feed degraded. Rather than track that by hand, the batch driver is a plain
list-difference loop over two CSVs:

- `Facebook_<batch>_groups.csv` — the full target list (`name,link,handle,public`).
  `--input-file` reads the `handle` column (vanity or numeric); optional
  `start_date` / `end_date` columns are honored, and then the matching CLI flag
  must *not* be set.
- `Facebook_<batch>_groups_backlog.csv` — same schema, only the handles still
  owed data. Re-run with `--continue` against this file. **The batch is done when
  the backlog CSV is header-only**, which for a healthy run is usually one pass
  later, not many.
- `tmp/scrape_recap.py <targets.csv> <output_dir>` — the regenerator's input:
  per handle, prints post count, result string, time taken, file mtime, and the
  covered date range; `MISSING` for handles with no file. Sorted worst-first.

Two flags worth setting on any unattended run:

- `--wait-for-account` — block (poll every 5 s) until an account frees up instead
  of raising `NoAccountError`. With 24 h rate-limit locks in play, this is what
  keeps a long batch alive.
- `--max-sessions 2` — we stayed low. Group scraping is the endpoint most
  associated with account loss; concurrency multiplies exposure.

---

## 9. Reading the forensics

When a scrape does trip one of the two structural failure modes, it dumps the
evidence rather than just logging a line — reading those dumps is how everything
above was worked out:

```
tmp/hybrid/cursor_reset/<label>/<UTC_ts>/{summary.json,window.jsonl}
tmp/hybrid/graphql_error/<label>/<UTC_ts>/{summary.json,window.jsonl}
```

`summary.json` gives `label`, `trigger_pagination`, the error, and window size;
`window.jsonl` is the rolling ~20-replay window of raw responses leading up to
the trip.

The census below is from the first batch — **before** the cursor-extraction fix,
JSONL resume, and automatic unstick landed. Treat it as the symptom catalogue,
not as a forecast for a run today. That batch left **54 dumps** across 3 days:

- **50 `graphql_error`** — all `field_exception`. 4 at pagination 1 (dead cursor
  on resume); the rest scattered from pagination 2 to 2150 (FB degrading a long
  run — median 82).
- **4 `cursor_reset`** — at paginations 12, 12, 151, 185.

Rules of thumb: `trigger_pagination == 1` → cursor problem, unstick it.
`trigger_pagination` large → FB degraded a long run, just run another leg. A
`cursor_reset` on a group is terminal for that leg (no date filter to advance)
but the partial data is preserved — resume normally.

`FB_NETWORK_CAPTURE_ALL=1` turns on full network capture if you need more.

---

## 10. Result-string triage

| `result` | On a group feed this means | Do |
|---|---|---|
| `consecutive_out_of_range` | walked past the date window under a non-chronological sort — **normal, clean finish** | nothing |
| `max_posts_reached` | hit your `--max-posts` cap | another `--continue` leg if you want more depth |
| `scraped until user-specified starting date was reached` | chronological date-tail stop | nothing |
| `no_new_posts_streak` | dedup wall — cursor anchored in already-collected territory | auto-unstick already appended a deeper cursor; run the next leg. If it repeats, `unstick-cursor --rank 6` (or higher) |
| `graphql_error: … field_exception` at pagination 1 | invalid / poisoned cursor | `fbscrape unstick-cursor <file>` (no `--only-if-stuck`), then resume |
| `graphql_error: … field_exception` deep in the run | FB degraded a long pagination run | resume; consider smaller `--max-posts` legs |
| `cursor_reset` | FB served a degraded shape with a jumped anchor; terminal on groups | resume with `--continue` |
| `rate_limit` | in-body code `1675004`; account locked 24 h, no retry burned | let the pool rotate; `--wait-for-account` |
| `template_capture_timeout` | GCFRSPQ never fired after the bootstrap scroll | retry the target; raise `--template-capture-timeout`; check the group still loads for that account |
| `group is private` | members-only group ("Only members can see…") | out of scope — we did not join groups; drop the target |
| `content not available` / `page not available` | group removed, renamed, or blocked for that account | verify by hand before assuming a scrape bug |

All of these preserve whatever was collected — there is no failure mode where a
leg silently discards posts. The top three rows are the ones you should expect
to see; the rest are contingencies, and the recoverable ones mostly clear
themselves on the next leg.

---

## 11. Caveats

- **`tmp/` is git-ignored scratch, not shipped code.** The experiment and repair
  scripts referenced here live only in a working checkout. They also carried a
  `data-twitter`-for-`data` typo until 2026-08-31 (§6) — if you copied one
  earlier, re-copy it.
- **The experiment scripts hardcode paths** (`tmp/unpoison_cursors.py` defaults to
  a local incident directory; `tmp/cursor_validity_experiment/setup.py` points at
  a baseline file under `tmp/continue_experiment/`). They are records of what was
  run, not general tools.
- **Cursors expire.** ~24 h validated. Old files are not reliably resumable —
  re-scrape rather than resume a months-old archive.
- **`--start-date` / `--end-date` on groups are client-side only.** A file's date
  coverage is an outcome, not a request. Always check the actual min/max
  `creation_time` (that's what `scrape_recap.py` prints).
- **Private groups were never solved.** No account of ours joined a group; a
  members-only feed returns `group is private` in ~5 s.
- The `CHRONOLOGICAL` output directory from the first batch is *abandoned* data —
  superseded by the `TOP_POSTS` re-run. Label such directories clearly; ours was
  a re-run away from being mistaken for the real corpus.

---

## 12. Where the evidence lives

| Path | What |
|---|---|
| `fbscrape/cli.py:29` `_find_unstick_cursor` | the rank-N deeper-cursor picker |
| `fbscrape/cli.py` `unstick-cursor` command | manual/batch cursor swap |
| `fbscrape/cli.py` `_append_unstick_line` | automatic unstick on `no_new_posts_streak` |
| `fbscrape/scraper.py` `_read_resume_state` / `_stream_resume_state` | JSONL tail-read + legacy ijson stream |
| `fbscrape/stop_conditions.py` | `CursorReset` (2nd-oldest anchor for groups), `ConsecutiveOutOfRange`, sort-aware assembly |
| `tests/unit/test_cli_unstick_cursor.py` | unstick behavior under test |
| `tmp/continue_experiment/` | `--continue` equivalence (Jaccard 0.97) — `run.sh`, `compare.py`, `run.log` |
| `tmp/cursor_validity_experiment/` | per-post cursor validity ~24 h (99/100) — `run.sh`, `setup.py`, `analyze.py`, `run.log` |
| `tmp/unpoison_cursors.py` | the 90-char poison-cursor repair one-off |
| `<collection repo>/tmp/hybrid/{cursor_reset,graphql_error}/` | the 54 forensic dumps |
| `<collection repo>/tmp/scrape_recap.py` | batch progress / backlog regenerator |
| `<collection repo>/data/group_posts*/` | the raw group scrapes (legacy envelope format) |
| `CLAUDE.md` KDDs 16, 19, 20, 21, 22, 24 | the design decisions behind all of the above |

**Related patterns from the page-side scrapes** (same class of problem, on
`UserTimeline` instead of `GroupTimeline`): re-scraping into a *fresh* output dir
with a one-day overlap and merging on `post_id` afterwards, when you don't want
to touch the canonical files; and an OOM-safe streaming flatten
(`iter_posts()` → per-post `json_normalize` → parquet shards every 250k rows) as
a replacement for `fbscrape flatten --concat` on multi-GB corpora.
