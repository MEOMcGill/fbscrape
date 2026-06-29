"""Login orchestrator transient-nav classification (`login.login`).

The login helpers (`login_with_cookies`, `check_logged_in`) issue bare
`page.goto` / `page.reload` calls that aren't individually guarded. A
renderer/network flake there raises a raw playwright `TimeoutError` /
`TargetClosedError` — which is NOT one of the worker's typed-retry
exceptions, so before this fix it escaped untyped through `gather()` and
tore down the entire batch (one bad navigation killing every other handle;
observed in production 2026-06-02).

`login()` now wraps its orchestration in a narrow chokepoint that
reclassifies exactly those two playwright errors as `TransientLoginError`
(worker rotates + retries, account stays active). Everything else —
checkpoint/ban/disabled typed exceptions, the terminal `FailedLoginError`,
and normal success/return-False flow — must pass through untouched.

Asserted invariants:
- A `playwright TimeoutError` from ANY login helper → `TransientLoginError`.
- A `TargetClosedError` from ANY login helper → `TransientLoginError`.
- The chokepoint covers later helpers too (not just the first call).
- A `CheckpointError` is NOT swallowed/reclassified — it propagates as-is.
- The terminal `FailedLoginError` (all methods returned False) is NOT
  reclassified into `TransientLoginError`.
- A successful login returns None (no spurious raise).
"""


import pytest

from fbscrape import login as login_mod
from fbscrape.login import (
    login, PlaywrightTimeoutError, TargetClosedError, PlaywrightError,
)
from fbscrape.exceptions import (
    TransientLoginError,
    FailedLoginError,
    CheckpointError,
)


class _FakeAccount:
    display_name = "Test Account"
    identifier = "test@example.com"


class _FakeSession:
    """Minimal stand-in: login() only touches .headless and .account here."""
    headless = True  # skips login_manual

    def __init__(self):
        self.account = _FakeAccount()


def _async_raise(exc):
    async def _fn(_session):
        raise exc
    return _fn


def _async_return(value):
    async def _fn(_session):
        return value
    return _fn


async def test_goto_timeout_reclassified_as_transient(monkeypatch):
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded")),
    )
    with pytest.raises(TransientLoginError):
        await login(_FakeSession())


async def test_target_closed_reclassified_as_transient(monkeypatch):
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(TargetClosedError("Page.reload: Target page, context or browser has been closed")),
    )
    with pytest.raises(TransientLoginError):
        await login(_FakeSession())


async def test_chokepoint_covers_later_helpers(monkeypatch):
    # First method declines (returns False); the flake happens in the SECOND
    # method — the chokepoint must still catch it.
    monkeypatch.setattr(login_mod, "login_with_cookies", _async_return(False))
    monkeypatch.setattr(
        login_mod, "login_automatic",
        _async_raise(PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded")),
    )
    with pytest.raises(TransientLoginError):
        await login(_FakeSession())


async def test_checkpoint_error_not_swallowed(monkeypatch):
    # A checkpoint is a dangerous, account-specific signal — it must NOT be
    # reclassified as a benign transient. (TransientLoginError is a subclass
    # of FailedLoginError, NOT of CheckpointError, so if reclassification
    # leaked, pytest.raises(CheckpointError) would fail here.)
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(CheckpointError("checkpoint detected")),
    )
    with pytest.raises(CheckpointError):
        await login(_FakeSession())


async def test_terminal_failed_login_not_reclassified(monkeypatch):
    # All methods decline → login() raises FailedLoginError. The narrow catch
    # must not turn that terminal failure into a retryable TransientLoginError.
    monkeypatch.setattr(login_mod, "login_with_cookies", _async_return(False))
    monkeypatch.setattr(login_mod, "login_automatic", _async_return(False))
    with pytest.raises(FailedLoginError) as exc_info:
        await login(_FakeSession())
    assert not isinstance(exc_info.value, TransientLoginError)


async def test_successful_login_returns_none(monkeypatch):
    monkeypatch.setattr(login_mod, "login_with_cookies", _async_return(True))
    assert await login(_FakeSession()) is None


async def test_ns_binding_aborted_reclassified_as_transient(monkeypatch):
    # The dead-worker case: a base playwright Error from page.reload with a
    # nav-abort marker must become TransientLoginError (worker rotates+retries),
    # not escape untyped as task_failed.
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(PlaywrightError("Page.reload: NS_BINDING_ABORTED")),
    )
    with pytest.raises(TransientLoginError):
        await login(_FakeSession())


async def test_net_err_marker_reclassified_as_transient(monkeypatch):
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(PlaywrightError("Page.goto: net::ERR_ABORTED at https://...")),
    )
    with pytest.raises(TransientLoginError):
        await login(_FakeSession())


async def test_unrelated_playwright_error_not_reclassified(monkeypatch):
    # A base playwright Error WITHOUT a known nav-abort marker must propagate
    # untouched — we don't broadly swallow every playwright Error.
    monkeypatch.setattr(
        login_mod, "login_with_cookies",
        _async_raise(PlaywrightError("Page.evaluate: some unrelated failure")),
    )
    with pytest.raises(PlaywrightError) as exc_info:
        await login(_FakeSession())
    assert not isinstance(exc_info.value, TransientLoginError)
