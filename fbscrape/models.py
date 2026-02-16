"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import ClassVar
import json


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

    # Maps endpoint names to required query fields
    ENDPOINT_REQUIRED_FIELDS: ClassVar[dict[str, list[str]]] = {
        "UserTimeline": ["handle", "start_date", "end_date"],
        # Add more as implemented:
        # "Search": ["query", "start_date", "end_date"],
        # "GroupTimeline": ["group_id", "start_date", "end_date"],
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

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dictionary"""
        return {
            'endpoint': self.endpoint,
            'query': self.query,
            'params': self.params,
            'start_date': str(self.start_date) if self.start_date else None,
            'end_date': str(self.end_date) if self.end_date else None,
        }

    def to_json(self) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict())


@dataclass
class ScrapingResult:
    """Result of a Facebook scraping operation"""
    query: Query
    result: str  # 'success', 'failed to load', 'timeout', etc.
    posts: list[dict]
    time_started: datetime
    time_taken: timedelta

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
