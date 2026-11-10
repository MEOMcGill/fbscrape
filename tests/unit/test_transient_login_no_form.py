"""An unclassifiable login page is transient, not a dead account.

Observed in production 2026-11-09/10: under load, the cookie probe's URL wait
timed out on accounts whose cookies were valid, `login_automatic` then found
neither a logged-in page nor a login form (the page hadn't loaded) and returned
False, and `login()` turned that into "All login methods failed" ->
`mark_inactive`. Nine healthy accounts were retired in ~20 hours; every one
logged straight back in with its stored cookies.

Asserted invariants:
- `login_automatic` raises `TransientLoginError` (not `return False`) when the
  page is neither logged in nor showing a login form.
- Headless `login()` propagates that `TransientLoginError` (worker rotates,
  account stays active).
- Non-headless `login()` falls back to manual login instead.
- The worker keeps an account active on its first transient login failures
  and marks it inactive only after MAX_CONSECUTIVE_TRANSIENT_LOGIN_FAILURES in
  a row; a success in between resets the streak.
"""

import asyncio

import pytest

import fbscrape.worker as worker_mod
from fbscrape import login as login_mod
from fbscrape.accounts_pool import AccountsPool
from fbscrape.exceptions import RetryBudgetExhaustedError, TransientLoginError
from fbscrape.models import Query
from fbscrape.worker import MAX_CONSECUTIVE_TRANSIENT_LOGIN_FAILURES, Worker


class _Acct:
    def __init__(self, ident="a@example.com"):
        self.identifier = ident
        self.display_name = ident


async def _false(*_a, **_k):
    return False


async def _true(*_a, **_k):
    return True


async def _noop(*_a, **_k):
    return None


# ==================== login_automatic / login ====================

class _Ctx:
    clear_cookies = staticmethod(_noop)


class _LoginSession:
    def __init__(self, headless=True):
        self.headless = headless
        self.account = _Acct()
        self._context = _Ctx()


def _no_form_page(monkeypatch):
    monkeypatch.setattr(login_mod, "check_logged_in", _false)
    monkeypatch.setattr(login_mod, "_clear_pre_login_popups", _noop)
    monkeypatch.setattr(login_mod, "_handle_continue_interstitial", _false)
    monkeypatch.setattr(login_mod, "_is_login_form_visible", _false)


def test_login_automatic_no_form_is_transient(monkeypatch):
    _no_form_page(monkeypatch)
    with pytest.raises(TransientLoginError):
        asyncio.run(login_mod.login_automatic(_LoginSession()))


def test_headless_login_propagates_transient(monkeypatch):
    _no_form_page(monkeypatch)
    monkeypatch.setattr(login_mod, "login_with_cookies", _false)
    with pytest.raises(TransientLoginError):
        asyncio.run(login_mod.login(_LoginSession(headless=True)))


def test_non_headless_login_falls_back_to_manual(monkeypatch):
    _no_form_page(monkeypatch)
    monkeypatch.setattr(login_mod, "login_with_cookies", _false)
    called = []

    async def _manual(_session):
        called.append(True)
        return True

    monkeypatch.setattr(login_mod, "login_manual", _manual)
    asyncio.run(login_mod.login(_LoginSession(headless=False)))
    assert called == [True]


# ==================== AccountsPool streak counter ====================

def test_pool_streak_counts_and_clears():
    pool = AccountsPool(db_file=":memory:")
    assert pool.record_transient_login_failure("a") == 1
    assert pool.record_transient_login_failure("a") == 2
    assert pool.record_transient_login_failure("b") == 1
    pool.clear_transient_login_failures("a")
    assert pool.record_transient_login_failure("a") == 1
    pool.clear_transient_login_failures("never-seen")  # no-op, no KeyError


# ==================== Worker policy ====================

class _OneAccountPool:
    """Always hands back the same account, so every retry hits it again."""

    def __init__(self):
        self.acct = _Acct()
        self.inactive = []
        self._streaks = {}

    async def get_available(self, order_by=None):
        return self.acct

    async def get_available_or_wait(self, order_by=None):
        return self.acct

    async def release_account(self, identifier):
        pass

    async def lock_until(self, identifier, until, error_msg=None):
        pass

    async def mark_inactive(self, identifier, error_msg):
        self.inactive.append((identifier, error_msg))

    record_transient_login_failure = AccountsPool.record_transient_login_failure
    clear_transient_login_failures = AccountsPool.clear_transient_login_failures

    @property
    def _transient_login_failures(self):
        return self._streaks


def _always_transient_session():
    class _S:
        def __init__(self, account=None, **kwargs):
            self.account = account
            self.scrolls_recorded = 0
            self.requests_sent = 0

        async def __aenter__(self):
            raise TransientLoginError("No login form and not logged in")

        async def __aexit__(self, *exc):
            return False

    return _S


def _query():
    return Query(endpoint="UserTimeline", mode="hybrid", query={"handle": "h"}, params={})


def _worker(monkeypatch, pool):
    monkeypatch.setattr(worker_mod, "BrowserSession", _always_transient_session())
    w = Worker(id="w0", pool=pool)
    w.current_account = pool.acct
    return w


def test_worker_marks_inactive_only_after_streak(monkeypatch):
    pool = _OneAccountPool()
    worker = _worker(monkeypatch, pool)
    with pytest.raises(RetryBudgetExhaustedError):
        asyncio.run(worker.execute_task(_query()))
    # Worker retries 3x per task; all three hit the same account.
    assert MAX_CONSECUTIVE_TRANSIENT_LOGIN_FAILURES == 3
    assert len(pool.inactive) == 1
    ident, msg = pool.inactive[0]
    assert ident == "a@example.com"
    assert "3 consecutive transient login failures" in msg
    # Streak is reset once acted on.
    assert pool._streaks == {}


def test_worker_keeps_account_active_below_streak(monkeypatch):
    pool = _OneAccountPool()
    pool._streaks["a@example.com"] = 0
    worker = _worker(monkeypatch, pool)

    # Simulate a success after two failures: the streak must start over.
    pool.record_transient_login_failure("a@example.com")
    pool.record_transient_login_failure("a@example.com")
    pool.clear_transient_login_failures("a@example.com")  # what _on_login_success does

    with pytest.raises(RetryBudgetExhaustedError):
        asyncio.run(worker.execute_task(_query()))
    # Three fresh failures -> exactly one deactivation, at the third.
    assert len(pool.inactive) == 1
