"""`BrowserSession.requests_sent` — the unit `Worker.requests_per_session` budgets.

Counts requests this session actually sent to Facebook: every scrape
navigation and every replay POST. Both have exactly one choke point
(`_hybrid_navigate` is the sole caller of `goto`, and `_hybrid_send_replay`
holds the only `page.request.post`), which is what makes two increments
sufficient for all 11 endpoints.

It deliberately does NOT reuse the interceptor's `graphql_request_count`:
replays go out through Playwright's APIRequestContext, which doesn't fire page
response events, so the interceptor never sees them — and `flush()` zeroes
that counter at the start of every scrape method, while this one has to
survive across tasks sharing a session.
"""

import asyncio

import pytest

from fbscrape.browser_session import BrowserSession


class _Acct:
    identifier = "a@example.com"
    display_name = "a@example.com"


def _bare_session():
    """A BrowserSession that never launched a browser (no initialize())."""
    return BrowserSession(account=_Acct(), pool=object())


def test_starts_at_zero():
    assert _bare_session().requests_sent == 0


def test_navigation_counts_one_request(monkeypatch):
    session = _bare_session()

    navigated = []

    async def _fake_goto(url, **kwargs):
        navigated.append(url)

    class _FakeKeyboard:
        async def press(self, key):
            pass

    class _FakePage:
        keyboard = _FakeKeyboard()

    async def _no_errors():
        return None

    monkeypatch.setattr(session, "goto", _fake_goto)
    monkeypatch.setattr(session, "check_error_conditions", _no_errors)
    session.page = _FakePage()

    error = asyncio.run(
        session._hybrid_navigate(
            target_url="https://www.facebook.com/someone/",
            post_nav_sleep_seconds=0,
            operation_timeout_seconds=5,
        )
    )

    assert error is None
    assert navigated == ["https://www.facebook.com/someone/"]
    assert session.requests_sent == 1


def test_failed_navigation_still_counts(monkeypatch):
    """The request went out; a later error doesn't un-send it."""
    session = _bare_session()

    async def _boom(url, **kwargs):
        raise RuntimeError("nav failed")

    monkeypatch.setattr(session, "goto", _boom)

    error = asyncio.run(
        session._hybrid_navigate(
            target_url="https://www.facebook.com/someone/",
            post_nav_sleep_seconds=0,
            operation_timeout_seconds=5,
        )
    )

    assert error is not None and error.startswith("navigation_error")
    assert session.requests_sent == 1


def test_replay_post_counts_one_request(monkeypatch):
    session = _bare_session()

    class _FakeRequest:
        async def post(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    class _FakePage:
        request = _FakeRequest()

    session.page = _FakePage()

    response, text, error = asyncio.run(
        session._hybrid_send_replay(
            handle="someone",
            body="{}",
            template_headers={},
            request_timeout_ms=1000,
            operation_timeout_seconds=5,
        )
    )

    # Terminal (non-raising) failure path — but the POST was still attempted.
    assert response is None and error.startswith("pagination_error")
    assert session.requests_sent == 1


def test_navigations_and_replays_accumulate(monkeypatch):
    """A ProfileAbout-shaped task (landing + 3 sub-tabs) vs a paginating one:
    the counter is what makes those comparable."""
    session = _bare_session()

    async def _fake_goto(url, **kwargs):
        pass

    class _FakeKeyboard:
        async def press(self, key):
            pass

    class _FakePage:
        keyboard = _FakeKeyboard()

    async def _no_errors():
        return None

    monkeypatch.setattr(session, "goto", _fake_goto)
    monkeypatch.setattr(session, "check_error_conditions", _no_errors)
    session.page = _FakePage()

    for _ in range(4):
        asyncio.run(
            session._hybrid_navigate(
                target_url="https://www.facebook.com/someone/about/",
                post_nav_sleep_seconds=0,
                operation_timeout_seconds=5,
            )
        )

    assert session.requests_sent == 4


def test_flush_does_not_reset_it():
    """Unlike graphql_request_count, this must survive the per-task flush()."""
    from fbscrape.response import ResponseInterceptor

    session = _bare_session()
    session.requests_sent = 7
    session.response_interceptor = ResponseInterceptor()
    session.response_interceptor.graphql_request_count = 7

    session.response_interceptor.flush()

    assert session.response_interceptor.graphql_request_count == 0
    assert session.requests_sent == 7, "session-scoped, not per-task"
