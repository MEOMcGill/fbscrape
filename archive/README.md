# archive/

Files kept for provenance that are no longer part of how this repo works.

Nothing here is imported, read by the CLI, or copied into any build context.
Moving a file here is how we retire it without deleting it: the path stops
existing where someone might trip over it, but the file itself is still around.

**Contents are not tracked.** See the `archive/*` rule in `.gitignore` — an
archived account database carries real passwords and cookies. Only this README
is committed. Anything moved here should be listed below, so the record
outlives the files.

## There is no 64 GB of scraped data here

`dt-facebook-crawler`'s Dockerfile warned that a working fbscrape checkout
holds "64 GB" of scraped output under `data/`, as observed on the office worker
in 2026-09. **That is not true of this checkout, and there was nothing to
archive.** Measured 2026-11-17 on `meo-remote`:

| path | size |
|---|---|
| whole checkout | 647 MB |
| `tests/` | 13 MB |
| `tmp/` | 11 MB |
| **`data/`** | **7.3 MB** |
| `fbscrape/` | 1.7 MB |
| `db/` | 1.3 MB |

Nothing was moved here on account of that figure, because there was nothing to
move. It described a different machine, a different moment, or both, and it is
not reproducible here. The Dockerfile comment has been reworded to state the
hazard without the unverifiable number.

The hazard is still real and the vendoring excludes still matter: `data/` is
where fbscrape writes scraped output, so it grows without bound on a machine
that scrapes, and an unexcluded copy lands in the docker build context and then
goes to the daemon. That is an argument for `--exclude data` when vendoring,
not for archiving anything.

## Contents

Empty as of 2026-11-17.

## What deliberately does NOT belong here

- **`data/`** — live working output, not residue.
  `fbscrape.utils.get_home_dir_path()` resolves it from the package's own
  location on disk, so moving it breaks path resolution for every scrape that
  writes there. See the section above for why the "64 GB" that once pointed
  attention at it does not exist on this checkout.
- **`db/`** — the canonical live account pool, bind-mounted into
  `dt-facebook-crawler`'s edge worker at `/opt/fbscrape/db`. This checkout owns
  it; it is the most load-bearing directory here, not an archivable one.
