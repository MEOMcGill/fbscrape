"""
Data models for Facebook scraping results
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ScrapingResult:
    """Result of a Facebook scraping operation"""
    result: str  # 'success', 'failed to load', 'timeout', etc.
    posts: list[dict]
    users: list[dict]
    time_started: datetime
    time_taken: timedelta

    def to_dict(self) -> dict:
        """Convert to dictionary format for backward compatibility"""
        return {
            'result': self.result,
            'posts': self.posts,
            'users': self.users,
            'time-started': str(self.time_started),
            'time-taken': str(self.time_taken)
        }
