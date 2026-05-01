"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import ClassVar
import json

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
            "query_required": ["handle", "start_date", "end_date"],
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
                        "scroll_burst_every": 10,
                        "scroll_burst_size_range": (2, 5),
                        "pagination_sleep_mean": 2.5,
                        "pagination_sleep_std": 0.5,
                        "template_capture_timeout": 20.0,
                        # -1 disables the cap entirely; otherwise a positive
                        # int caps how many replays a single session fires.
                        "max_paginations": -1,
                        "post_nav_sleep_seconds": 3.0,
                        "request_timeout_ms": 30000,
                        "max_no_progress_streak": 5,
                        "operation_timeout_seconds": 900,
                    },
                },
                # "api": {...}  -- future: pure replay, no browser
            },
        },
        # e.g. for future endpoints
        # "GroupTimeline": {...},
        # "Search": {...},
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
    output side: they describe *what happened* (result string, posts, timing).
    The Worker layer attaches the canonical Query (which it already holds as
    the task) to compose a final ScrapingResult — so the Query is constructed
    exactly once, in the scraper layer, and never rebuilt downstream.
    """
    result: str  # 'success', 'failed to load', 'timeout', etc.
    posts: list[dict]
    time_started: datetime
    time_taken: timedelta


@dataclass
class ScrapingResult:
    """Result of a Facebook scraping operation"""
    query: Query
    result: str  # 'success', 'failed to load', 'timeout', etc.
    posts: list[dict]
    time_started: datetime
    time_taken: timedelta

    @classmethod
    def from_outcome(cls, query: Query, outcome: ScrapeOutcome) -> "ScrapingResult":
        """Compose a ScrapingResult from the upstream Query and a downstream
        ScrapeOutcome. Used by Worker to attach the canonical Query to what
        BrowserSession produced."""
        return cls(
            query=query,
            result=outcome.result,
            posts=outcome.posts,
            time_started=outcome.time_started,
            time_taken=outcome.time_taken,
        )

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary"""
        return {
            'query': self.query.to_dict(),
            'result': self.result,
            'posts': self.posts,
            'time_started': str(self.time_started),
            'time_taken': str(self.time_taken),
        }

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict())

    def save(self, path: str):
        """Save to JSON file"""
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)

    def add_post(self, post: dict):
        """Add a post to the results"""
        self.posts.append(post)
