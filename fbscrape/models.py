"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import ClassVar
import json
import gzip
import os
import tempfile

from .logger import logger


@dataclass
class JSONTrait:
    """Mixin for JSON serialization of dataclasses"""

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary"""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict(), default=str)


@dataclass
class Query:
    """
    Represents a scraping task with endpoint-specific query validation.

    The query dict is validated based on the endpoint to ensure all required
    parameters are present before task execution.
    """

    # Single source of truth for endpoints, modes, and per-(endpoint, mode) params.
    # Adding an endpoint = one new top-level entry. Adding a mode to an endpoint =
    # one new entry under "modes". Defaults live here so saved ScrapingResults
    # record the actual values in effect (reproducibility).
    #
    # Schema:
    #   ENDPOINT_REGISTRY[endpoint] = {
    #       "query_required": [field, ...],          # required keys in `query`
    #       "modes": {
    #           mode_name: {
    #               "params": {param_name: default}, # `default = None` means required
    #           },
    #       },
    #   }
    ENDPOINT_REGISTRY: ClassVar[dict] = {
        "UserTimeline": {
            # `start_date` and `end_date` are *optional* (not in query_required).
            # When `end_date` is omitted, the CLI auto-fills today (FB's UI
            # always sends `beforeTime`, and we mirror that fingerprint at the
            # CLI layer). When `start_date` is omitted, no lower bound — the
            # client-side stop conditions (`OldestInBatchBelowStartDate`,
            # `ConsecutiveOutOfRange`) no-op via their existing `is None` guards.
            "query_required": ["handle"],
            "modes": {
                "manual": {
                    "params": {
                        "scroll_window_height_coefficient": 3.0,
                        "post_nav_sleep_seconds": 5.0,
                        "inter_scroll_sleep_range": (2.0, 4.5),
                        "breather_every_n_scrolls": 50,
                        "breather_duration_seconds": 30,
                        "max_no_new_posts_streak": 30,
                        "stall_timeout_seconds": 300,
                        "operation_timeout_seconds": 900,
                    },
                },
                "hybrid": {
                    "params": {
                        "pagination_count": 3,
                        "scroll_burst_every": 100,
                        "scroll_burst_size_range": (2, 5),
                        "pagination_sleep_mean": 2.5,
                        "pagination_sleep_std": 0.5,
                        "template_capture_timeout": 45.0,
                        # -1 disables the cap entirely; otherwise a positive
                        # int caps how many replays a single session fires.
                        "max_paginations": -1,
                        # -1 disables the cap; otherwise a positive int caps
                        # total accumulated posts. Checked at batch
                        # boundaries, so the actual return count can exceed
                        # this by up to `pagination_count - 1` posts.
                        "max_posts": -1,
                        # Resume support (parallels GroupTimeline; see that
                        # entry for the rationale). Sentinel defaults so the
                        # registry's `None = required` convention isn't
                        # triggered. The high-level scraper injects these
                        # only on leg 0 of its multi-leg cursor_reset loop;
                        # subsequent legs start fresh with adjusted end_date.
                        "initial_cursor": "",
                        "seen_post_ids_to_skip": [],
                        "post_nav_sleep_seconds": 3.0,
                        "request_timeout_ms": 30000,
                        "max_no_progress_streak": 5,
                        "operation_timeout_seconds": 900,
                    },
                },
                # "api": {...}  -- future: pure replay, no browser
            },
        },
        "Search": {
            "query_required": ["query_text"],
            "modes": {
                # Hybrid only — Search has no scroll-driven `manual` mode and
                # never will. Date bounds are enforced server-side via the
                # URL filter blob (see browser_session._build_search_url),
                # not via GraphQL `beforeTime` / `afterTime`.
                "hybrid": {
                    "params": {
                        # FB UI requests 5 search posts per page; mirror it.
                        "pagination_count": 5,
                        "scroll_burst_every": 50,
                        "scroll_burst_size_range": (2, 5),
                        "pagination_sleep_mean": 2.5,
                        "pagination_sleep_std": 0.5,
                        "template_capture_timeout": 45.0,
                        "max_paginations": -1,
                        # -1 disables the cap; positive int caps total
                        # accumulated posts. Batch-boundary enforced.
                        "max_posts": -1,
                        "post_nav_sleep_seconds": 3.0,
                        "request_timeout_ms": 30000,
                        "max_no_progress_streak": 5,
                        "operation_timeout_seconds": 900,
                    },
                },
            },
        },
        "PageTransparency": {
            # Caller supplies the numeric page_id; bootstrap navigation hits
            # https://www.facebook.com/<page_id>/ directly (FB redirects to
            # the canonical page URL), so no handle is required. `handle` is
            # still accepted as an optional query field — when present, it's
            # used for the navigation URL (better matches a real user typing
            # the vanity URL) and for output filenames.
            "query_required": ["page_id"],
            "modes": {
                # Hybrid only — single-shot replay, no pagination, no date
                # filter. The natural ProfileTransparencyDialogQuery only
                # fires from a UI click, so we don't wait for it; we capture
                # auth-bearing fields (fb_dtsg, lsd, __user, __csr, __dyn,
                # cookies) from any natural GraphQL POST that fires during
                # navigation and synthesize the transparency body.
                "hybrid": {
                    "params": {
                        "post_nav_sleep_seconds": 3.0,
                        "template_capture_timeout": 45.0,
                        "request_timeout_ms": 30000,
                        "operation_timeout_seconds": 120,
                    },
                },
            },
        },
        "ProfileAuthenticity": {
            # Caller supplies the numeric user_id; bootstrap navigation hits
            # https://www.facebook.com/<user_id>/ directly (FB redirects to
            # the canonical profile), so no handle is needed.
            "query_required": ["user_id"],
            "modes": {
                # Hybrid only — single-shot replay, no pagination, no date
                # filter. The natural ProfileCometDirectoryAuthenticityModalQuery
                # only fires from a UI click, so we don't wait for it; we
                # capture auth-bearing fields from any natural GraphQL POST
                # and synthesize the authenticity body.
                "hybrid": {
                    "params": {
                        "scale": 3,
                        "post_nav_sleep_seconds": 3.0,
                        "template_capture_timeout": 45.0,
                        "request_timeout_ms": 30000,
                        "operation_timeout_seconds": 120,
                    },
                },
            },
        },
        "PostDetail": {
            # Caller supplies the parent `handle` (vanity handle / numeric id
            # of the group, page, or user that owns the post — drives the
            # navigation URL) and `post_id` (numeric form OR pfbid-form; both
            # resolve via the permalink redirect). Unlike PageTransparency /
            # ProfileAuthenticity, there is NO GraphQL replay: FB server-renders
            # the post's Story into the permalink document's embedded JSON
            # (RelayPrefetchedStreamCache), so the scrape reads the document and
            # extracts the Story directly. Single-shot, no pagination.
            "query_required": ["handle", "post_id"],
            "modes": {
                "hybrid": {
                    "params": {
                        # Group posts live at /groups/<handle>/posts/<post_id>/;
                        # page / user posts at /<handle>/posts/<post_id>/. FB
                        # doesn't cross-resolve the two, so the caller declares
                        # which surface the post lives on.
                        "is_group": False,
                        "post_nav_sleep_seconds": 3.0,
                        # Max seconds to wait for the permalink document's
                        # server-rendered Story blob to be present after nav.
                        "document_wait_seconds": 4.0,
                        "operation_timeout_seconds": 120,
                    },
                },
            },
        },
        "ProfileInfo": {
            # Caller supplies the profile's vanity `handle` (or numeric id;
            # both resolve via /<handle>/). Like PostDetail, there is NO
            # GraphQL replay: FB server-renders the profile header (name,
            # follower count, cover photo, verified badge, intro-card
            # fields) into the document's embedded JSON
            # (`profile_header_renderer.user`), so the scrape reads the
            # document directly. Single-shot, no pagination.
            "query_required": ["handle"],
            "modes": {
                "hybrid": {
                    "params": {
                        "post_nav_sleep_seconds": 3.0,
                        # Max seconds to wait after navigation for the
                        # server-rendered profile header blob to settle
                        # before reading the document.
                        "document_wait_seconds": 4.0,
                        "operation_timeout_seconds": 120,
                    },
                },
            },
        },
        "ProfileAbout": {
            # Caller supplies the profile's vanity `handle` (or numeric id).
            # Like ProfileInfo, there is NO GraphQL replay — FB server-renders
            # everything into embedded JSON — but unlike ProfileInfo this is
            # NOT single-navigation: only the profile's header (name,
            # follower count, bio, category — same fields ProfileInfo
            # returns, folded into this record too since navigating to the
            # About landing page already renders them for free) plus a
            # directory of About sub-tab URLs come from one navigation
            # (`/<handle>/about/`); each requested sub-tab's actual fields
            # (contact info, address/hours, links, ...) only populate when
            # THAT specific sub-tab is navigated to directly — FB doesn't
            # server-render them all together. So this endpoint does one
            # landing navigation + one navigation per requested section.
            #
            # Section availability varies a lot by account: pages typically
            # expose contact_info/basic_info/links; personal profiles more
            # often expose personal_details/work/education instead. A
            # requested section absent from the discovered directory is
            # skipped, not an error.
            "query_required": ["handle"],
            "modes": {
                "hybrid": {
                    "params": {
                        # Sub-tab keys to fetch, matched against the
                        # directory discovered on the About landing page
                        # (`all_collections`). Default picks the 3
                        # empirically highest-value Page sections; override
                        # per call for personal-profile-shaped accounts
                        # (e.g. "directory_work", "directory_education").
                        "sections": (
                            "directory_contact_info",
                            "directory_basic_info",
                            "directory_links",
                        ),
                        "post_nav_sleep_seconds": 3.0,
                        "document_wait_seconds": 4.0,
                        "operation_timeout_seconds": 120,
                    },
                },
            },
        },
        "CommentsList": {
            # Caller supplies the parent post's `handle` (vanity handle of the
            # author / page that owns the post — needed for the navigation URL)
            # and `post_id` (numeric form OR pfbid-form; both resolve via
            # /<handle>/posts/<post_id>/). The base64-encoded `feedback:<id>`
            # variable the GraphQL replay needs is inherited from the natural
            # CommentsListComponentsPaginationQuery template captured during
            # the bootstrap scroll, so callers don't have to know it.
            "query_required": ["handle", "post_id"],
            "modes": {
                "hybrid": {
                    "params": {
                        # FB UI mirrors: -1 lets the server pick the page size
                        # (~10 comments per page empirically). Override to a
                        # positive integer to cap; rarely useful since the
                        # natural fingerprint is -1.
                        "comments_after_count": -1,
                        # `variables.feedLocation` on every replay. FB UI uses
                        # POST_PERMALINK_DIALOG when viewing a post's single-
                        # post permalink page (the surface our nav URL hits).
                        # Other observed values include "DEDICATED_PAGE" /
                        # "POST_PREVIEW"; leave at the default unless you've
                        # verified a different value via DevTools capture.
                        "feed_location": "POST_PERMALINK_DIALOG",
                        "scroll_burst_every": 50,
                        "scroll_burst_size_range": (2, 5),
                        "pagination_sleep_mean": 2.5,
                        "pagination_sleep_std": 0.5,
                        "template_capture_timeout": 45.0,
                        "max_paginations": -1,
                        # -1 disables the cap; positive int caps total
                        # accumulated comments. Batch-boundary enforced — the
                        # actual return count can exceed by up to one page.
                        "max_results": -1,
                        # Resume support. Sentinel defaults so the registry's
                        # `None` = required convention isn't triggered. Same
                        # role as GroupTimeline's resume seeds.
                        "initial_cursor": "",
                        "seen_comment_ids_to_skip": [],
                        "post_nav_sleep_seconds": 3.0,
                        "request_timeout_ms": 30000,
                        "max_no_progress_streak": 5,
                        "operation_timeout_seconds": 900,
                    },
                },
            },
        },
        "GroupTimeline": {
            # `handle` accepts either a vanity group handle (e.g. "albertaseparatism")
            # or the numeric group id — both forms resolve via `/groups/<handle>/`.
            # Date filtering is purely client-side: the GraphQL query carries no
            # beforeTime/afterTime variable, so termination relies on the parser-
            # extracted creation_time vs. start_date (Key Design Decision: hybrid
            # date-bounded stops). cursor_reset is terminal here (no server-side
            # date filter to advance for a resume leg).
            #
            # `start_date` and `end_date` are *optional* (not in query_required).
            # FB's group-feed UI sends no date filter at all (no `beforeTime` /
            # `afterTime` variable in GCFRSPQ), so both defaults mirror that
            # fingerprint: stay None when omitted. Client-side stop conditions
            # are None-safe via their existing guards.
            "query_required": ["handle"],
            "modes": {
                "hybrid": {
                    "params": {
                        # FB UI requests 3 group posts per page; mirror it.
                        "pagination_count": 3,
                        # Group feed sort, injected into every replay body's
                        # `variables.sortingSetting`. Empirically-validated values:
                        #   - "TOP_POSTS" — FB UI default; algorithmic ranking.
                        #     Older posts can appear at any edge position; the
                        #     `oldest_in_batch < start_date` stop is unreliable
                        #     here and is dropped from the default stop set in
                        #     favor of `ConsecutiveOutOfRange` (counts N posts
                        #     in a row outside the window). Lowest-fingerprint
                        #     choice and the empirically-validated safer option
                        #     for sustained scraping (CHRONOLOGICAL appears
                        #     correlated with FB suspending the account).
                        #   - "CHRONOLOGICAL" — stream-line tail is descending
                        #     by post creation_time. Closest to true creation-
                        #     time ordering BUT empirically associated with
                        #     bans — opt-in only. Caveat: the per-batch
                        #     *bootstrap* edge (always 1 post) is sometimes
                        #     out of order; the `OldestInBatchBelowStartDate`
                        #     stop condition is exempt on iter 1 (cursor_sent
                        #     is None) to absorb this.
                        #   - "RECENT_ACTIVITY" — sorts by most recent activity
                        #     (comment / reaction); not strictly post-creation
                        #     order, so a bumped old post can fire the
                        #     `oldest_in_batch < start_date` stop early. Treated
                        #     as non-chronological by `assemble_default_stop_conditions`.
                        # Override via `--sorting-setting` CLI flag / scraper kwarg.
                        "sorting_setting": "TOP_POSTS",
                        "scroll_burst_every": 50,
                        "scroll_burst_size_range": (2, 5),
                        "pagination_sleep_mean": 2.5,
                        "pagination_sleep_std": 0.5,
                        "template_capture_timeout": 45.0,
                        "max_paginations": -1,
                        # -1 disables the cap; positive int caps total
                        # accumulated posts. Batch-boundary enforced.
                        "max_posts": -1,
                        # Resume support. Sentinel defaults so the registry's
                        # `None` = required convention isn't triggered:
                        #   - "" → no initial cursor (loop starts from null)
                        #   - [] → no IDs to skip (interceptor dedup is fresh)
                        # When set (by the --continue CLI flag or by
                        # `FacebookScraper.group_timeline(resume_from=...)`),
                        # the loop starts with this cursor and seeds the
                        # interceptor's seen_post_ids set so bootstrap-edge
                        # highlights from prior runs aren't re-counted.
                        "initial_cursor": "",
                        "seen_post_ids_to_skip": [],
                        "post_nav_sleep_seconds": 3.0,
                        "request_timeout_ms": 30000,
                        "max_no_progress_streak": 30,
                        # N posts in a row outside [start_unix, end_unix] →
                        # bail with `consecutive_out_of_range`. Primary
                        # date-tail stop on non-chronological sorts (TOP_POSTS,
                        # RECENT_ACTIVITY) where `oldest_in_batch < start` is
                        # unreliable. Kept enabled on CHRONOLOGICAL too as
                        # belt-and-suspenders against the rare bootstrap-edge
                        # highlight that's old enough to mislead. -1 disables.
                        "max_consecutive_out_of_range": 20,
                        "operation_timeout_seconds": 900,
                    },
                },
            },
        },
    }

    # Format for date strings stored in `query` (e.g. query["start_date"]).
    DATE_FORMAT: ClassVar[str] = "%Y-%m-%d"
    # Keys in `query` that, if present, are validated as YYYY-MM-DD dates.
    # When both are set on the same Query, _validate_dates checks ordering.
    DATE_QUERY_KEYS: ClassVar[tuple[str, str]] = ("start_date", "end_date")

    endpoint: str
    mode: str
    query: dict
    params: dict

    def __post_init__(self):
        """Validate endpoint/mode/query/params and fill default params from registry."""
        self._validate_endpoint()
        self._validate_mode()
        self._validate_query_fields()
        self._validate_and_apply_param_defaults()
        self._validate_dates()

    def _validate_endpoint(self):
        if self.endpoint not in self.ENDPOINT_REGISTRY:
            raise ValueError(
                f"Unsupported endpoint: '{self.endpoint}'. "
                f"Supported endpoints: {list(self.ENDPOINT_REGISTRY.keys())}"
            )

    def _validate_mode(self):
        modes = self.ENDPOINT_REGISTRY[self.endpoint]["modes"]
        if self.mode not in modes:
            raise ValueError(
                f"Unsupported mode: '{self.mode}' for endpoint '{self.endpoint}'. "
                f"Supported modes: {list(modes.keys())}"
            )

    def _validate_query_fields(self):
        required = self.ENDPOINT_REGISTRY[self.endpoint]["query_required"]
        missing = [field for field in required if field not in self.query]
        if missing:
            raise ValueError(
                f"Query for endpoint '{self.endpoint}' missing required fields: {missing}. "
                f"Required: {required}"
            )

    def _validate_and_apply_param_defaults(self):
        """Reject unknown param keys, error on missing required (default=None), fill defaults."""
        allowed = self.ENDPOINT_REGISTRY[self.endpoint]["modes"][self.mode]["params"]
        unknown = [k for k in self.params if k not in allowed]
        if unknown:
            raise ValueError(
                f"Unknown params for ({self.endpoint!r}, {self.mode!r}): {unknown}. "
                f"Allowed: {list(allowed.keys())}"
            )
        missing_required = [k for k, default in allowed.items()
                            if default is None and k not in self.params]
        if missing_required:
            raise ValueError(
                f"Missing required params for ({self.endpoint!r}, {self.mode!r}): "
                f"{missing_required}"
            )
        for k, default in allowed.items():
            if k not in self.params:
                self.params[k] = default

    def _validate_dates(self):
        """Validate any date-shaped keys in `query` (the source of truth).

        Reads `start_date` / `end_date` (YYYY-MM-DD strings) from `self.query`
        if present, validates them, and writes back the clamped string if
        end_date was in the future. Endpoints that don't include date keys
        in `query_required` won't trigger any of this — date validation is
        opt-in by virtue of the endpoint asking for these fields.

        - start_date == end_date is allowed (single-day scrape).
        - start_date in the future → hard error.
        - end_date in the future → clamp to today and log a warning (lenient).
        """
        start_key, end_key = self.DATE_QUERY_KEYS
        start_str = self.query.get(start_key)
        end_str = self.query.get(end_key)

        def _parse(label: str, s) -> date | None:
            if s is None:
                return None
            if not isinstance(s, str):
                raise ValueError(
                    f"{label} must be a {self.DATE_FORMAT} string, got {type(s).__name__}"
                )
            try:
                return datetime.strptime(s, self.DATE_FORMAT).date()
            except ValueError:
                raise ValueError(
                    f"{label} must match {self.DATE_FORMAT}, got {s!r}"
                )

        start = _parse(start_key, start_str)
        end = _parse(end_key, end_str)
        today = datetime.now().date()

        if start and end and start > end:
            raise ValueError(f"{start_key} must be on or before {end_key}")
        if start and start > today:
            raise ValueError(f"{start_key} must be on or before today")
        if end and end > today:
            logger.warning(
                f"{end_key} {end} is in the future — clamping to today ({today})"
            )
            self.query[end_key] = today.strftime(self.DATE_FORMAT)



    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary"""
        return {
            'endpoint': self.endpoint,
            'mode': self.mode,
            'query': self.query,
            'params': self.params,
        }

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict())


@dataclass
class ScrapeOutcome:
    """Outcome of a single scrape, without the Query that triggered it.

    Returned by BrowserSession scrape methods, which are Query-agnostic on the
    output side: they describe *what happened* (result string, records, timing).
    The Worker layer attaches the canonical Query (which it already holds as
    the task) to compose a final ScrapingResult — so the Query is constructed
    exactly once, in the scraper layer, and never rebuilt downstream.

    `data` is `list[dict]` always (one element per scraped record). Single-
    record endpoints like PageTransparency populate a 1-element list; post-
    stream endpoints like UserTimeline / Search populate one element per post.

    `last_cursor` is the cursor that the next replay *would* have used had
    the loop continued — i.e. the latest `end_cursor` observed on the wire.
    `None` for single-shot endpoints, and `None` when a paginated loop
    exited via the "end_cursor null = end of feed" path. Used by the
    `--continue` resume mechanism. Per FB's own docs, cursors are
    ephemeral and may be invalidated server-side; treat as best-effort.
    """
    result: str  # 'success', 'failed to load', 'timeout', etc.
    data: list[dict]
    time_started: datetime
    time_taken: timedelta
    last_cursor: str | None = None
    # Write-on-parse spill (JSONL migration). When a paginated scrape streams
    # each post to a `.jsonl.gz` as it parses (instead of accumulating in RAM),
    # `data` is left empty and the records live in the file at `spill_path`;
    # `post_count` is the number written. `spill_path is None` => records are
    # inline in `data` (single-shot endpoints, manual mode), and `num_records`
    # falls back to `len(data)`.
    post_count: int | None = None
    spill_path: str | None = None

    @property
    def num_records(self) -> int:
        return self.post_count if self.post_count is not None else len(self.data)


@dataclass
class ScrapingResult:
    """Result of a Facebook scraping operation.

    `data` mirrors `ScrapeOutcome.data` — see that class's docstring for the
    list[dict] convention across single-record and post-stream endpoints.
    `last_cursor` mirrors `ScrapeOutcome.last_cursor` — see that class's
    docstring; serialized into saved JSON so `--continue` can pick it up.
    """
    query: Query
    result: str  # 'success', 'failed to load', 'timeout', etc.
    data: list[dict]
    time_started: datetime
    time_taken: timedelta
    last_cursor: str | None = None
    # See ScrapeOutcome — `spill_path`/`post_count` carry write-on-parse output
    # (JSONL migration); `data` is empty when a spill is present.
    post_count: int | None = None
    spill_path: str | None = None

    @property
    def num_records(self) -> int:
        return self.post_count if self.post_count is not None else len(self.data)

    def iter_posts(self):
        """Iterate the scraped records regardless of storage. Streams from the
        spill file when present (write-on-parse), else iterates inline `data`."""
        if self.spill_path is not None:
            from .jsonl_store import iter_posts
            yield from iter_posts(self.spill_path)
        else:
            yield from self.data

    @classmethod
    def from_outcome(cls, query: Query, outcome: ScrapeOutcome) -> "ScrapingResult":
        """Compose a ScrapingResult from the upstream Query and a downstream
        ScrapeOutcome. Used by Worker to attach the canonical Query to what
        BrowserSession produced."""
        return cls(
            query=query,
            result=outcome.result,
            data=outcome.data,
            time_started=outcome.time_started,
            time_taken=outcome.time_taken,
            last_cursor=outcome.last_cursor,
            post_count=outcome.post_count,
            spill_path=outcome.spill_path,
        )

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary"""
        return {
            'query': self.query.to_dict(),
            'result': self.result,
            'data': self.data,
            'time_started': str(self.time_started),
            'time_taken': str(self.time_taken),
            'last_cursor': self.last_cursor,
        }

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict())

    def save(self, path: str, compress: bool = True, append: bool = False) -> str:
        """Save as one-post-per-line JSONL. Returns the final path written.

        Each line is a self-contained envelope `{query, result, time_started,
        time_taken, last_cursor, data: <single record>}` (see `jsonl_store`).
        The records come from `iter_posts()` — the in-memory `data` list, or the
        write-on-parse spill when `spill_path` is set. The leg's terminal
        `result`/`time_taken` are stamped on the final line; mid-leg lines carry
        null. `query` (constant for the leg) rides every line.

        `append=True` appends a new gzip member to an existing file (`--continue`
        — no whole-file rewrite). A fresh write goes to a temp + `os.replace`
        (atomic; a crash leaves the prior file intact). `compress` toggles gzip.
        """
        from .jsonl_store import JsonlPostWriter
        if compress and not path.endswith('.gz'):
            path = path + '.gz'
        query_dict = self.query.to_dict()

        def _drain(writer: 'JsonlPostWriter') -> None:
            for post in self.iter_posts():
                writer.write_post(post, self.last_cursor)
            writer.finalize(self.result, self.time_taken, last_cursor=self.last_cursor)

        if append and os.path.exists(path):
            writer = JsonlPostWriter(path, query_dict, self.time_started,
                                     append=True, compress=compress)
            _drain(writer)
            return path

        # Fresh write: temp + os.replace (atomic on the same filesystem).
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        os.close(fd)
        try:
            writer = JsonlPostWriter(tmp, query_dict, self.time_started, compress=compress)
            _drain(writer)
            os.chmod(tmp, 0o644)  # mkstemp is 0600; match the prior default
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def add_record(self, record: dict):
        """Append a record to the results"""
        self.data.append(record)
