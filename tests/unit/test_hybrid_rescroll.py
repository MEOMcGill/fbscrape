"""`_hybrid_wait_for_template` re-scroll behavior.

A single bootstrap scroll often fails to provoke the feed-refetch query under
load (page not rendered yet, or one viewport isn't past FB's SSR posts), and the
wait used to just dead-poll. With `rescroll=True` the wait keeps scrolling
(scrollBy is relative → progressively deeper) until the query fires or it times
out. Single-shot endpoints leave `rescroll=False` and must not scroll.
"""
from __future__ import annotations

import pytest

from fbscrape.browser_session import BrowserSession


class _FakeInterceptor:
    def __init__(self):
        self.latest_pctfrq_request = None
        self.network_capture = []


class _FakeSession:
    """Minimal stand-in exposing just what `_hybrid_wait_for_template` touches."""
    def __init__(self, provoke_after_scrolls: int):
        self.response_interceptor = _FakeInterceptor()
        self.scroll_calls = 0
        self._provoke_after = provoke_after_scrolls
        self.endpoint = "UserTimeline"

    async def scroll(self, window_height_coefficient: float = 3):
        self.scroll_calls += 1
        # Simulate FB finally firing the PCTFRQ once we've scrolled deep enough.
        if self.scroll_calls >= self._provoke_after:
            self.response_interceptor.latest_pctfrq_request = {
                "post_data": "x", "headers": {},
            }

    def _hybrid_parse_form_data(self, _post_data):
        return {}


async def test_rescroll_keeps_scrolling_until_template_appears():
    sess = _FakeSession(provoke_after_scrolls=2)
    tpl = await BrowserSession._hybrid_wait_for_template(
        sess, timeout_seconds=3.0, rescroll=True, rescroll_every_seconds=0.5,
    )
    assert tpl is not None                 # template eventually captured
    assert sess.scroll_calls >= 2          # it re-scrolled (didn't dead-poll)


async def test_no_rescroll_when_disabled():
    sess = _FakeSession(provoke_after_scrolls=999)  # never provokes
    tpl = await BrowserSession._hybrid_wait_for_template(
        sess, timeout_seconds=1.0, rescroll=False, rescroll_every_seconds=0.5,
    )
    assert tpl is None
    assert sess.scroll_calls == 0          # single-shot path never scrolls


async def test_returns_immediately_if_template_already_present():
    sess = _FakeSession(provoke_after_scrolls=1)
    sess.response_interceptor.latest_pctfrq_request = {"post_data": "y", "headers": {}}
    tpl = await BrowserSession._hybrid_wait_for_template(
        sess, timeout_seconds=3.0, rescroll=True, rescroll_every_seconds=0.5,
    )
    assert tpl == {"post_data": "y", "headers": {}}
    assert sess.scroll_calls == 0          # no need to scroll — it was already there


async def test_rescroll_failure_does_not_abort_capture(monkeypatch):
    """A scroll hiccup mid-wait must be swallowed, not propagated."""
    sess = _FakeSession(provoke_after_scrolls=999)

    calls = {"n": 0}
    async def _boom(window_height_coefficient: float = 3):
        calls["n"] += 1
        raise RuntimeError("transient scroll error")
    sess.scroll = _boom

    # Should simply time out (return None), not raise.
    tpl = await BrowserSession._hybrid_wait_for_template(
        sess, timeout_seconds=1.5, rescroll=True, rescroll_every_seconds=0.5,
    )
    assert tpl is None
    assert calls["n"] >= 1                  # it did attempt to scroll
