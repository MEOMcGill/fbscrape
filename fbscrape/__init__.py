"""
Facebook scraping module - separated concerns for browser management,
authentication, response parsing, and scraping logic.
"""

from .browser import BrowserManager, PageController
from .session import FacebookAuth
from .response import ResponseInterceptor, FacebookGraphQLParser
from .scraper import FacebookScraper
from .models import ScrapingResult
from .utils import parse_facebook_date, internet_good, is_post_url, extract_post_id

__all__ = [
    'BrowserManager',
    'PageController',
    'FacebookAuth',
    'ResponseInterceptor',
    'FacebookGraphQLParser',
    'FacebookScraper',
    'ScrapingResult',
    'parse_facebook_date',
    'internet_good',
    'is_post_url',
    'extract_post_id',
]
