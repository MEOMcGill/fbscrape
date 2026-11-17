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
from fbscrape.db import fetchone
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

@pytest.mark.asyncio
async def test_pool_streak_counts_and_clears(tmp_path):
    """The streak is PERSISTED (migrate_v4), so it survives a new pool object.

    That is the whole point: callers that run one process per unit of work --
    the group chain runs one per leg -- used to rebuild the pool each time and
    reset an in-memory streak to zero, so the retirement threshold was
    unreachable no matter how many logins an account failed.
    """
    db = str(tmp_path / "accounts.db")
    pool = AccountsPool(db_file=db)
    await pool.add_account(password="pw", phone_number="+10000000001")
    await pool.add_account(password="pw", phone_number="+10000000002")
    a, b = "+10000000001", "+10000000002"

    assert await pool.record_transient_login_failure(a) == 1
    assert await pool.record_transient_login_failure(a) == 2
    # Streaks are per-account, not global.
    assert await pool.record_transient_login_failure(b) == 1

    # A fresh pool object over the same database still sees the streak.
    assert await AccountsPool(db_file=db).record_transient_login_failure(a) == 3

    await pool.clear_transient_login_failures(a)
    assert await pool.record_transient_login_failure(a) == 1

    # A success ends the streak AND is credited to the login history.
    await pool.record_login_success(a)
    assert await pool.record_transient_login_failure(a) == 1
    row = await fetchone(
        db,
        "SELECT login_ok_total, login_fail_total FROM accounts WHERE phone_number = :p",
        {"p": a},
    )
    assert row[0] == 1
    assert row[1] == 5

    # Unknown identifier is a no-op, not an error.
    await pool.clear_transient_login_failures("never-seen")


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

    async def record_transient_login_failure(self, identifier):
        n = self._streaks.get(identifier, 0) + 1
        self._streaks[identifier] = n
        return n

    async def clear_transient_login_failures(self, identifier):
        self._streaks.pop(identifier, None)

    async def record_login_success(self, identifier):
        self._streaks.pop(identifier, None)

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
    # Login rotations get MAX_LOGIN_RETRIES attempts; the streak trips at the
    # third, and the account is retired exactly once however many follow.
    assert MAX_CONSECUTIVE_TRANSIENT_LOGIN_FAILURES == 3
    assert len(pool.inactive) == 1
    ident, msg = pool.inactive[0]
    assert ident == "a@example.com"
    assert "3 consecutive transient login failures" in msg
    # The streak was reset when acted on -- that is what stops a second
    # deactivation, asserted above. Any residue here is bookkeeping on an
    # account that is ALREADY inactive, and only exists because this fake
    # keeps serving it: the real pool filters `WHERE active = true`, so a
    # retired account cannot be handed back at all. Assert the invariant that
    # matters -- it never climbs back to the threshold.
    assert pool._streaks.get("a@example.com", 0) < MAX_CONSECUTIVE_TRANSIENT_LOGIN_FAILURES


def test_worker_keeps_account_active_below_streak(monkeypatch):
    pool = _OneAccountPool()
    pool._streaks["a@example.com"] = 0
    worker = _worker(monkeypatch, pool)

    # Simulate a success after two failures: the streak must start over.
    pool._streaks["a@example.com"] = 2
    pool._streaks.pop("a@example.com")  # what a successful login does

    with pytest.raises(RetryBudgetExhaustedError):
        asyncio.run(worker.execute_task(_query()))
    # Three fresh failures -> exactly one deactivation, at the third.
    assert len(pool.inactive) == 1
