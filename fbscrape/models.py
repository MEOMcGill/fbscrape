"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import ClassVar
import json

@dataclass
class JSONTrait:
    def dict(self):
        return asdict(self)

    def json(self):
        return json.dumps(self.dict(), default=str)

@dataclass
class Query(JSONTrait):
    """
    Represents a scraping task with endpoint-specific query validation.

    The query dict is validated based on the endpoint to ensure all required
    parameters are present before task execution.
    """

    # Maps endpoint names to required query fields
    ENDPOINT_REQUIRED_FIELDS: ClassVar[dict[str, list[str]]] = {
        "user_timeline": ["handle", "start_date", "end_date"],
        # Add more as implemented:
        # "search": ["query", "start_date", "end_date"],
        # "group_posts": ["group_id", "start_date", "end_date"],
    }

    endpoint: str
    query: dict
    params: dict
    start_date: datetime | None = None
    end_date: datetime | None = None

    def __post_init__(self):
        """Validate query fields based on endpoint."""
        self._validate_endpoint()
        self._validate_query_fields()

    def _validate_endpoint(self):
        """Check that endpoint is supported."""
        if self.endpoint not in self.ENDPOINT_REQUIRED_FIELDS:
            raise ValueError(
                f"Unsupported endpoint: '{self.endpoint}'. "
                f"Supported endpoints: {list(self.ENDPOINT_REQUIRED_FIELDS.keys())}"
            )

    def _validate_query_fields(self):
        """Check that all required fields for the endpoint are present in query."""
        required = self.ENDPOINT_REQUIRED_FIELDS[self.endpoint]
        missing = [field for field in required if field not in self.query]
        if missing:
            raise ValueError(
                f"Query for endpoint '{self.endpoint}' missing required fields: {missing}. "
                f"Required: {required}"
            )

    def to_dict(self):
        return {
            'endpoint': self.endpoint,
            'query': self.query,
            'params': self.params,
            'start-date': str(self.start_date) if self.start_date else None,
            'end-date': str(self.end_date) if self.end_date else None
        }

@dataclass
class ScrapingResult:
    """Result of a Facebook scraping operation"""
    query: Query
    result: str  # 'success', 'failed to load', 'timeout', etc.
    posts: list[dict]
    time_started: datetime
    time_taken: timedelta

    def to_dict(self) -> dict:
        """Convert to dictionary format"""
        return {
            'query': self.query.dict(),
            'result': self.result,
            'posts': self.posts,
            'time_started': str(self.time_started),
            'time_taken': str(self.time_taken)
        }

    def add_post(self, post: dict):
        self.posts.append(post)
