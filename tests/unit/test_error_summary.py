"""_error_summary keeps request logs (and the cookies in them) out of result strings."""

from fbscrape.browser_session import _error_summary


def test_strips_playwright_call_log():
    e = Exception(
        "APIRequestContext.post: connect ENETUNREACH 2a03:2880::25de:443 - Local (:::0)\n"
        "Call log:\n  - → POST https://www.facebook.com/api/graphql/\n"
        "    - cookie: c_user=61588152462651; xs=46%3Asecret\n"
    )
    out = _error_summary(e)
    assert out == "APIRequestContext.post: connect ENETUNREACH 2a03:2880::25de:443 - Local (:::0)"
    assert "cookie" not in out and "c_user" not in out


def test_single_line_unchanged():
    assert _error_summary(TimeoutError("Page.goto: Timeout 30000ms exceeded")) == "Page.goto: Timeout 30000ms exceeded"
