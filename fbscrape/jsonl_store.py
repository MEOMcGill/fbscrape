"""One-post-per-line JSONL store for scrape outputs.

The scrape format is a gzipped JSONL file (`<stem>.jsonl.gz`) where **each line
is a self-contained envelope** carrying the leg metadata plus a *single* post:

    {"query":{…},"result":null,"time_started":"…","time_taken":null,"last_cursor":"CUR","data":{…Story…}}

This replaces the monolithic `{… "data": [all posts] …}` envelope. The win:
- **write-on-parse** — the scraper writes each post the instant it's parsed and
  never accumulates the leg in RAM (memory O(1) in posts);
- **`--continue` = append** — a resume leg appends its lines (a new gzip member),
  no whole-file rewrite;
- **resume = tail-read** — the last line carries `last_cursor`, and the last N
  lines carry the recent `post_id`s for cross-leg dedup; recovered by
  decompress-and-scan-the-tail (no separate sidecar).

Per-line metadata semantics:
- `query` / `time_started` are constant for a leg and ride every line.
- `last_cursor` is the page cursor of the batch that post arrived in; the **last
  line's** is the resume cursor.
- `result` / `time_taken` are only known when a leg ends, so mid-leg lines carry
  `null`; the leg's **final line** carries the terminal values. The last line of
  the file is authoritative for leg status. (A zero-post leg writes a single
  status-only line with `data: null`.)

`JsonlPostWriter` keeps a 1-post buffer so it can stamp the terminal
`result`/`time_taken` onto the final post's line without holding the whole leg.
"""

import decimal
import gzip
import json
from collections import deque


def _json_default(o):
    """json fallback for Decimal (ijson yields Decimal; stdlib json can't
    serialize it). int for integral values, else float — matching json.load."""
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _post_id(rec) -> str | None:
    """`node.post_id` -> top-level `post_id` precedence (matches the rest of the
    codebase, incl. scraper._stream_resume_state)."""
    if not isinstance(rec, dict):
        return None
    node = rec.get("node") or {}
    return (node.get("post_id") if isinstance(node, dict) else None) or rec.get("post_id")


def _is_gzip(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return path.endswith(".gz")


def _open_text(path: str, mode: str = "rt"):
    """Open .gz transparently (sniffed by magic on read; by extension on write)."""
    if "r" in mode:
        return gzip.open(path, mode) if _is_gzip(path) else open(path, mode)
    return gzip.open(path, mode) if path.endswith(".gz") else open(path, mode)


class JsonlPostWriter:
    """Append-one-post-per-line writer. Buffers a single post so the final line
    can carry the terminal `result`/`time_taken`.

    Usage:
        w = JsonlPostWriter(path, query_dict, time_started, append=False)
        for post, cursor in ...:
            w.write_post(post, cursor)
        w.finalize(result="success", time_taken=td, last_cursor=final_cur)

    `append=True` opens the gzip in append mode (a new member appended to an
    existing file) for `--continue`. Readers decompress all members transparently.
    """

    def __init__(self, path: str, query: dict, time_started,
                 append: bool = False, compress: bool = True,
                 autoflush: bool = False):
        self.path = path
        self.query = query
        self.time_started = str(time_started) if time_started is not None else None
        mode = "at" if append else "wt"
        # autoflush: for streaming (write-on-parse) scrapes, line-buffer and flush
        # each emitted line to the OS so a mid-scrape cancellation or kill leaves
        # every already-parsed post on disk — otherwise the process's userspace
        # buffer (up to a page) is lost on abnormal exit. Uncompressed only; gzip
        # buffers internally, so streaming callers pass compress=False.
        self._autoflush = autoflush
        if compress:
            self._fh = gzip.open(path, mode)
        else:
            self._fh = open(path, mode, buffering=(1 if autoflush else -1))
        self._buffered = None  # (post, cursor) awaiting flush
        self._finalized = False
        self.count = 0  # posts handed to write_post()

    def _emit(self, post, cursor, result, time_taken):
        line = {
            "query": self.query,
            "result": result,
            "time_started": self.time_started,
            "time_taken": (str(time_taken) if time_taken is not None else None),
            "last_cursor": cursor,
            "data": post,
        }
        self._fh.write(json.dumps(line, default=_json_default))
        self._fh.write("\n")
        if self._autoflush:
            self._fh.flush()

    def write_post(self, post: dict, last_cursor: str | None = None) -> None:
        # Flush the previously-buffered post as a non-final (result=null) line.
        if self._buffered is not None:
            p, c = self._buffered
            self._emit(p, c, None, None)
        self._buffered = (post, last_cursor)
        self.count += 1

    def finalize(self, result: str, time_taken, last_cursor: str | None = None) -> None:
        """Flush the buffered (final) post stamped with the terminal status. A
        zero-post leg writes a single status-only line (`data: null`). Idempotent."""
        if self._finalized:
            return
        if self._buffered is not None:
            p, c = self._buffered
            self._emit(p, last_cursor if last_cursor is not None else c, result, time_taken)
            self._buffered = None
        elif self.count == 0:
            self._emit(None, last_cursor, result, time_taken)
        self._finalized = True
        self._fh.close()

    def close(self) -> None:
        """Close without a terminal stamp (e.g. on error). Flushes a buffered
        post as a non-final line so it isn't lost."""
        if self._finalized:
            return
        if self._buffered is not None:
            p, c = self._buffered
            self._emit(p, c, None, None)
            self._buffered = None
        self._finalized = True
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # If finalize() wasn't called (e.g. exception), close without losing the
        # buffered post. A real finalize() should be called on the happy path.
        self.close()
        return False


def looks_like_jsonl(path: str) -> bool:
    """True if `path` is one-post-per-line JSONL (vs. a legacy whole-file
    envelope).

    A JSONL line is a complete JSON object whose `data` is a *single* record
    (dict, or null on a status-only line). A whole-file envelope — pretty-printed
    (first line `{`, doesn't parse alone) or compact (whole object on one line) —
    carries a *list* under `data` (or the legacy `posts` key). So: first line
    must parse as a dict AND neither `data` nor `posts` is a list.
    """
    try:
        with _open_text(path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    return False
                return (
                    isinstance(obj, dict)
                    and not isinstance(obj.get("data"), list)
                    and not isinstance(obj.get("posts"), list)
                )
    except OSError:
        return False
    return False


def iter_post_lines(path: str):
    """Yield each line's parsed envelope dict (`{query, result, …, data}`).
    Skips blank and malformed lines (robust to a partial trailing line from an
    interrupted append)."""
    with _open_text(path, "rt") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # tolerate a partial/corrupt trailing line


def iter_posts(path: str):
    """Yield each post (`data`) from a JSONL file, skipping status-only
    (`data: null`) lines. The primary reader primitive for flatten/download."""
    for obj in iter_post_lines(path):
        post = obj.get("data")
        if post is not None:
            yield post


def load_records(path: str) -> list[dict]:
    """Dual-format loader: return the post records from a scrape file whether
    it's one-post-per-line JSONL (current) or a legacy whole-file envelope
    (`{… "data": [...] …}` / legacy `"posts"`). Materializes the list — for
    offline post-processing (flatten / download-media), matching prior behavior.
    """
    if looks_like_jsonl(path):
        return list(iter_posts(path))
    import json as _json
    with _open_text(path, "rt") as f:
        doc = _json.load(f)
    return doc.get("data") or doc.get("posts") or []


def load_scrape_file(path: str) -> tuple[dict | None, list[dict]]:
    """Dual-format loader returning `(query, records)` in a single pass. Handles
    one-post-per-line JSONL (current) and the legacy whole-file envelope. The
    `query` comes from the first JSONL line / the envelope's top-level `query`;
    `records` are the posts (status-only `data: null` lines skipped).
    Materializes `records` — for offline post-processing (flatten / download)."""
    if looks_like_jsonl(path):
        query = None
        records: list[dict] = []
        for obj in iter_post_lines(path):
            if query is None:
                query = obj.get("query")
            post = obj.get("data")
            if post is not None:
                records.append(post)
        return query, records
    import json as _json
    with _open_text(path, "rt") as f:
        doc = _json.load(f)
    return doc.get("query"), (doc.get("data") or doc.get("posts") or [])


def read_meta(path: str) -> dict | None:
    """Return the last line's envelope (authoritative leg metadata: `result`,
    `last_cursor`, `query`, timing) — or None if the file has no parseable line.
    Scans the whole file (cheap relative to parsing every post into objects)."""
    last = None
    for obj in iter_post_lines(path):
        last = obj
    return last


def read_resume_tail(path: str, n_ids: int = 150) -> tuple[str, list[str]]:
    """Recover `(last_cursor, recent_post_ids)` for `--continue` by scanning the
    JSONL tail. Decompression-bound, not parse-bound: keeps only the last
    `n_ids` lines (a bounded rolling window) and parses just those.

    `last_cursor` is the last line's cursor; `recent_post_ids` is the last
    `n_ids` post ids (collection order) for cross-leg dedup seeding.
    """
    tail: deque[str] = deque(maxlen=n_ids + 2)
    buf = ""
    with _open_text(path, "rt") as f:
        while True:
            chunk = f.read(1 << 20)  # 1 MiB of decompressed text
            if not chunk:
                break
            buf += chunk
            parts = buf.split("\n")
            buf = parts.pop()  # trailing partial line (no newline yet)
            for line in parts:
                if line.strip():
                    tail.append(line)
        if buf.strip():
            tail.append(buf)

    last_cursor = ""
    post_ids: list[str] = []
    for line in tail:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        lc = obj.get("last_cursor")
        if lc:
            last_cursor = lc  # ends as the last non-null cursor (the resume point)
        pid = _post_id(obj.get("data"))
        if pid:
            post_ids.append(pid)
    return last_cursor, post_ids[-n_ids:]


def convert_envelope_to_jsonl(src_path: str, dest_path: str) -> tuple[int, str | None]:
    """Convert a legacy whole-file envelope (`{… "data": [posts] …}`) into a
    one-post-per-line JSONL file. Streams the `data` array via ijson so the
    source is never materialized. Returns `(n_posts, endpoint)`.

    The envelope's leg-level `result`/`time_taken` are stamped on the final
    post's line; intermediate lines carry `null` (matching a native leg).
    """
    import ijson

    # Pull the small scalar head (query, result, timing, last_cursor) first.
    meta = _read_envelope_head(src_path)
    writer = JsonlPostWriter(
        dest_path, meta.get("query") or {}, meta.get("time_started"), append=False,
    )
    endpoint = (meta.get("query") or {}).get("endpoint")
    data_key = _detect_envelope_data_key(src_path)
    last_cursor = meta.get("last_cursor")
    n = 0
    with _open_bytes(src_path) as fh:
        for rec in ijson.items(fh, f"{data_key}.item", use_float=True):
            writer.write_post(rec, last_cursor)
            n += 1
    writer.finalize(meta.get("result"), meta.get("time_taken"), last_cursor=last_cursor)
    return n, endpoint


def _open_bytes(path: str):
    return gzip.open(path, "rb") if _is_gzip(path) else open(path, "rb")


def _detect_envelope_data_key(path: str) -> str:
    """Return 'data' or 'posts' (legacy) — whichever array the envelope carries."""
    import ijson
    with _open_bytes(path) as fh:
        for prefix, event, _v in ijson.parse(fh):
            if event == "start_array" and prefix in ("data", "posts"):
                return prefix
    return "data"


def _read_envelope_head(path: str) -> dict:
    """Pull the scalar envelope fields (query, result, time_started, time_taken,
    last_cursor) without materializing the `data` array.

    `result` precedes `data` but `time_started`/`time_taken`/`last_cursor` trail
    it, so we can't early-stop at the array — we parse to EOF dispatching only on
    the (top-level, exact-name) scalar prefixes and ignore every `data.item.*`
    event. One-time/offline converter use, so the extra decompression is fine."""
    import ijson
    head: dict = {}
    with _open_bytes(path) as fh:
        # `query` is a small nested object near the top — grab it whole (early-stops).
        try:
            head["query"] = next(ijson.items(fh, "query"))
        except StopIteration:
            head["query"] = {}
    want = {"result", "time_started", "time_taken", "last_cursor"}
    with _open_bytes(path) as fh:
        for prefix, event, value in ijson.parse(fh):
            if prefix in want and event in ("string", "number", "null", "boolean"):
                head[prefix] = value
    return head
