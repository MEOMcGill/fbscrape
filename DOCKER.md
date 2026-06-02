# Running `fbscrape` in Docker

This repo ships a ready-to-use container setup that runs the scraper with a real
(non-headless) Camoufox browser inside a **virtual X display** (`Xvfb`), exposed
over **noVNC** so you can watch — and interact with — the browser from your web
browser. Running the browser headful inside Xvfb is the recommended mode: it
keeps Camoufox's stealth fingerprint intact while still working on a headless
server with no physical display.

## What's in the box

| File | Role |
|---|---|
| [`Dockerfile`](Dockerfile) | Python 3.12 slim base + browser system libs + Xvfb/x11vnc/noVNC + the `fbscrape` package (editable install) + `camoufox fetch`. |
| [`docker-compose.yml`](docker-compose.yml) | One `scraper` service: builds the image, mounts state dirs, publishes noVNC on `6080`, sets `shm_size: 2gb`. |
| [`docker/entrypoint.sh`](docker/entrypoint.sh) | Boots `Xvfb` → `fluxbox` → `x11vnc` → `websockify` (noVNC), then `exec`s your command. |
| [`.dockerignore`](.dockerignore) | Keeps the build context small (excludes `db/`, `data/`, `auth/`, `tmp/`, venvs, etc.). |

Inside the container the package is installed as the `fbscrape` console script
(see `[project.scripts]` in `pyproject.toml`), so every CLI command from the
README works verbatim.

## Prerequisites

- Docker Engine + the Docker Compose plugin (`docker compose ...`).
- ~2 GB free RAM for the browser (`shm_size: 2gb` is set to avoid Chromium/Firefox
  `/dev/shm` exhaustion crashes).

## Persistent state (volumes)

`docker-compose.yml` bind-mounts four host directories into `/app` so all state
survives container rebuilds and restarts:

| Host path | Container path | Contains |
|---|---|---|
| `./db` | `/app/db` | `accounts.db` — the SQLite account pool. |
| `./data` | `/app/data` | Scrape output JSON / flattened datasets / downloaded media. |
| `./auth` | `/app/auth` | Saved per-account cookies / session state. |
| `./tmp` | `/app/tmp` | Forensic dumps (cursor-reset / graphql-error windows). |

> These dirs are also `.dockerignore`d, so they are **not** baked into the image —
> they only exist via the bind mounts at runtime.

### File ownership (UID/GID)

The image creates a non-root `scraper` user. To keep files written into the
mounted volumes owned by **you** on the host (instead of by an arbitrary
container UID), the build accepts `UID` / `GID` build args, wired through
compose. Build with your own ids:

```bash
UID=$(id -u) GID=$(id -g) docker compose build
```

(On Linux this matters; on Docker Desktop for macOS/Windows the VM handles
ownership translation and the defaults `1000:1000` are fine.)

## Build

```bash
# from the repo root
docker compose build
# or, to bake in your host UID/GID (recommended on Linux):
UID=$(id -u) GID=$(id -g) docker compose build
```

The build runs `python -m camoufox fetch` as the `scraper` user, so the stealth
Firefox binary is downloaded into the image at build time (not on first run).

## Start the container

```bash
docker compose up -d
```

The service stays alive (`tty: true`, `CMD ["bash"]`). On startup the entrypoint
prints the noVNC URL:

```
noVNC: http://localhost:6080/vnc.html (display :99, 1920x1080x24)
```

Open <http://localhost:6080/vnc.html> in your browser to watch the scraper's
browser window (useful for solving the occasional login checkpoint by hand).

## Run scraper commands

Exec into the running container and use the CLI exactly as documented in the
[README](README.md):

```bash
docker compose exec scraper bash
```

### 1. Add an account

```bash
fbscrape account add --email user@example.com --password 'secret123'
# or
fbscrape account add --phone +1234567890 --password 'secret123'

fbscrape account list -v
fbscrape account stats
```

The DB lives at `/app/db/accounts.db` → persisted to `./db/accounts.db` on the
host.

### 2. Scrape

```bash
# User timeline (hybrid is the default mode)
fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01

# Group timeline
fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01

# Comments on a post
fbscrape scrape comments-list zuck:10115311901107991 --max-results 200

# From an input file (mount it under ./data first)
fbscrape scrape user-timeline --input-file /app/data/targets.csv
```

Output JSON lands under `/app/data` (`./data` on the host).

### 3. Post-process

```bash
fbscrape flatten /app/data/zuck_UserTimeline_hybrid.json --format parquet
fbscrape download-media /app/data/zuck_UserTimeline_hybrid.json --include-thumbnails
```

### One-shot runs (no shell)

You can also run a single command without an interactive shell. The entrypoint
still boots the display first, then runs your command:

```bash
docker compose run --rm scraper \
  fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
```

## Headful (default) vs. headless

- **Headful inside Xvfb (default).** The browser runs against `DISPLAY=:99` and
  is viewable over noVNC. This is the lowest-fingerprint, recommended mode — just
  run the CLI commands as-is.
- **Headless.** You can still pass `--headless` to any `scrape` subcommand. It
  works, but headless Firefox is more detectable; prefer the Xvfb path for
  sustained scraping.

## Configuration knobs

Environment variables read by `docker/entrypoint.sh` (override in
`docker-compose.yml` under `environment:` or with `-e`):

| Var | Default | Meaning |
|---|---|---|
| `DISPLAY` | `:99` | X display the browser renders to. |
| `SCREEN_RES` | `1920x1080x24` (compose) / `1280x800x24` (image) | Virtual screen geometry. |
| `VNC_PORT` | `5900` | Internal x11vnc port. |
| `NOVNC_PORT` | `6080` | noVNC/websockify port (published to the host). |

To change the published noVNC port, edit the `ports:` mapping in
`docker-compose.yml` (e.g. `"8080:6080"`).

## Proxies

Per-account proxies are stored in the DB and applied automatically by the
browser session — no container-level network config needed:

```bash
fbscrape set user@example.com proxy_server http://proxy:8080
fbscrape set user@example.com proxy_username myuser
fbscrape set user@example.com proxy_password mypass
```

## Lifecycle

```bash
docker compose logs -f scraper     # tail entrypoint + scraper logs
docker compose restart scraper     # restart (stale X locks are auto-cleaned)
docker compose down                # stop & remove the container (volumes persist)
docker compose build --no-cache    # rebuild from scratch
```

State in `./db`, `./data`, `./auth`, and `./tmp` survives `down` / rebuilds
because it lives on the host via bind mounts.

## Troubleshooting

- **noVNC page won't load** — confirm `docker compose ps` shows the service up and
  port `6080` published; check `docker compose logs scraper` for the
  `noVNC: http://localhost:6080/...` line.
- **Browser crashes / renderer wedges under load** — ensure `shm_size: 2gb` is in
  effect (it is, in the provided compose file); low `/dev/shm` is the usual cause.
- **`Xvfb` won't claim the display after a hard restart** — the entrypoint removes
  stale `/tmp/.X99-lock` automatically; if you changed `DISPLAY`, the cleanup
  follows the new number.
- **Files in `./data` owned by root/`1000`** — rebuild with
  `UID=$(id -u) GID=$(id -g) docker compose build` (see *File ownership* above).
- **Login hits a checkpoint** — open the noVNC URL and complete the challenge
  manually in the live browser window; the saved cookies (in `./auth`) carry the
  session forward.
