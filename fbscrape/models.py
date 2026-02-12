"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json

@dataclass
class JSONTrait:
    def dict(self):
        return asdict(self)

    def json(self):
        return json.dumps(self.dict(), default=str)

@dataclass
class Query(JSONTrait):
    endpoint: str
    query: dict
    params: dict
    start_date: datetime | None = None
    end_date: datetime | None = None

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
