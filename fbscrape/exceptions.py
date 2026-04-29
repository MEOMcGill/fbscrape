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
    """Failed to login to Facebook account (generic)"""
    pass


class CheckpointError(FailedLoginError):
    """Facebook redirected to /checkpoint/<id>/... — account needs manual
    resolution (ID photo, SMS code, etc.). Subclass of FailedLoginError so
    existing `except FailedLoginError` handlers still catch it."""

    def __init__(self, message: str, url: str | None = None):
        super().__init__(message)
        self.url = url


class AccountDisabledError(CheckpointError):
    """Facebook redirected to /checkpoint/disabled/ — account is permanently
    dead. Subclass of CheckpointError (which is itself a FailedLoginError)."""
    pass


class TransientLoginError(FailedLoginError):
    """An unexpected error during the login form flow that is *likely* transient
    (playwright element-not-found, page timeout, browser flake). Subclass of
    FailedLoginError so existing `except FailedLoginError` catches still work,
    but a dedicated worker clause can rotate to a different account WITHOUT
    marking the current one inactive."""
    pass


class AccountBannedError(FacebookScraperError):
    """Account has been banned or suspended"""
    pass


class RateLimitError(FacebookScraperError):
    """Hit rate limit"""
    pass