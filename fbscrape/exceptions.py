"""
Custom exceptions for fbscrape
"""


class FacebookScraperError(Exception):
    """Base exception for fbscrape"""
    pass


class NoAccountError(FacebookScraperError):
    """No accounts available in pool"""
    pass


class FailedLoginError(FacebookScraperError):
    """Failed to login to Facebook account"""
    pass


class AccountBannedError(FacebookScraperError):
    """Account has been banned or suspended"""
    pass


class RateLimitError(FacebookScraperError):
    """Hit rate limit"""
    pass