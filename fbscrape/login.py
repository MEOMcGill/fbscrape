"""
Login flows for Facebook accounts.

This module owns everything related to taking a freshly-opened
`BrowserSession` and reaching a logged-in state — form-fill flow,
viewer-based login detection, post-form URL classification, the obstacle
handlers we know about (cookie popups, the "Continue" interstitial), and
the manual breakpoint-driven flow for human-in-the-loop logins.

API surface (functions take a BrowserSession because the session owns the
playwright page/context, the AccountsPool, and the response interceptor):

    login(session)            -> production orchestrator: cookies → automatic
                                 → manual (non-headless only). Raises typed
                                 exceptions on terminal failure.
    login_with_cookies(session) -> cookie-injection + viewer probe.
    login_automatic(session)  -> form-fill flow (with Continue-interstitial fast
                                 path); used by `login()` when cookies fail.
    login_manual(session)     -> open facebook.com, breakpoint() for human.
    check_logged_in(session)  -> GraphQL-viewer-based login detection.

Helper contract: each `login_*` function returns True on confirmed success,
False on "this method is N/A or didn't authenticate" (caller should try the
next method), and raises a typed exception (CheckpointError, AccountDisabledError,
AutomationCheckpointError, TransientLoginError, FailedLoginError) on actual
account-state problems. These typed exceptions are caught upstream by
`Worker.execute_task`, which decides account rotation policy.

Internal helpers (popup dismissal, human-like typing, login-form detection,
post-form URL classification) live as module-level coroutines and take the
session explicitly. They were previously methods on BrowserSession.
"""
from __future__ import annotations

import asyncio
import bdb
import random
import re
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from playwright.async_api import (
    Locator,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
try:  # not re-exported from the public API on older playwright versions
    from playwright.async_api import TargetClosedError
except ImportError:
    from playwright._impl._errors import TargetClosedError

# Renderer/network nav-abort markers that warrant a transient rotate+retry
# rather than a batch-killing untyped escape. These surface as a *base*
# playwright Error (not Timeout/TargetClosed) from bare page.goto/page.reload
# in the login helpers — e.g. `Page.reload: NS_BINDING_ABORTED` from a wedged
# or proxy-flaky session (observed: one worker dead-churning task_failed every
# ~90s, never rotating, because the error was untyped). Gecko (Camoufox) uses
# the NS_* codes; net::ERR_ is the Chromium-fallback prefix.
_TRANSIENT_NAV_MARKERS = (
    "NS_BINDING_ABORTED",
    "NS_ERROR_ABORT",
    "NS_ERROR_NET_RESET",
    "NS_ERROR_NET_INTERRUPT",
    "NS_ERROR_NET_TIMEOUT",
    "NS_ERROR_CONNECTION_REFUSED",
    "NS_ERROR_PROXY_CONNECTION_REFUSED",
    "NS_ERROR_UNKNOWN_HOST",
    "NS_ERROR_UNKNOWN_PROXY_HOST",
    "net::ERR_",
)

from .exceptions import (
    FailedLoginError, CheckpointError, AccountDisabledError,
    AutomationCheckpointError, TransientLoginError,
)
from .logger import logger
from .response import ResponseInterceptor

if TYPE_CHECKING:
    from .browser_session import BrowserSession


# Max attempts of the form-fill + submit + verify inner block within a single
# login_automatic() call. Gives us one internal retry on transient playwright
# flakes before escalating to the worker via TransientLoginError.
LOGIN_FORM_MAX_ATTEMPTS = 2


# ==================== Public API ====================

async def login(session: "BrowserSession") -> None:
    """Production login orchestrator: cookies → automatic → manual (non-headless only).

    Returns None on success. Raises a typed exception on terminal failure;
    typed exceptions from the sub-functions (CheckpointError family,
    TransientLoginError, etc.) propagate straight to the worker.

    Helpers return False to signal "this method is N/A or didn't authenticate"
    — that's the trigger to fall through to the next method on the SAME account.

    Transient-nav chokepoint: the login helpers issue several bare
    `page.goto` / `page.reload` calls (e.g. `login_with_cookies`,
    `check_logged_in`) that aren't individually guarded. A renderer/network
    flake there raises a raw playwright `TimeoutError` / `TargetClosedError`,
    which is NOT one of the worker's typed-retry exceptions — so it would
    escape untyped through `gather()` and tear down the ENTIRE batch (one
    bad navigation killing every other handle). We reclassify those into
    `TransientLoginError` so the worker rotates to a fresh account + browser
    and retries (account stays `active=True`). Some nav flakes instead surface
    as a *base* playwright `Error` (e.g. `Page.reload: NS_BINDING_ABORTED` from
    a wedged/proxy-flaky session) — those are reclassified too, but ONLY when
    the message matches a known nav/network-abort marker (`_TRANSIENT_NAV_MARKERS`);
    any other base `Error` re-raises untouched. The catch is deliberately
    narrow: checkpoint / ban / disabled signals raise their own typed
    exceptions and pass straight through, never swallowed here.
    """
    try:
        if await login_with_cookies(session):
            return
        if await login_automatic(session):
            return
        if not session.headless:
            if await login_manual(session):
                return
        raise FailedLoginError(
            f"All login methods failed for {session.account.display_name}"
        )
    except (PlaywrightTimeoutError, TargetClosedError) as e:
        raise TransientLoginError(
            f"Login navigation flaked (transient) for "
            f"{session.account.display_name}: {e}"
        ) from e
    except PlaywrightError as e:
        # Base playwright Error from a nav flake (e.g. Page.reload:
        # NS_BINDING_ABORTED). Reclassify ONLY known nav/network-abort markers
        # to TransientLoginError so the worker rotates to a fresh account +
        # browser and retries (account stays active); any other playwright
        # Error re-raises untouched — no broad swallowing.
        msg = str(e)
        if any(marker in msg for marker in _TRANSIENT_NAV_MARKERS):
            raise TransientLoginError(
                f"Login navigation aborted (transient) for "
                f"{session.account.display_name}: {e}"
            ) from e
        raise


async def login_automatic(session: "BrowserSession") -> bool:
    """
    Execute Facebook form-fill login flow if needed.

    Transient errors (playwright flake, element-not-found, page timeout) get
    one internal retry with a page reload; if still failing, a
    `TransientLoginError` is raised so the worker can rotate to a different
    account WITHOUT marking the current one inactive.

    Returns:
        True if login successful or already logged in, False on known "can't
        login here" conditions (no form visible, viewer never resolved, URL
        never settled).

    Raises:
        CheckpointError / AccountDisabledError: Facebook redirected to a
            /checkpoint/ page (detector already wrote DB state).
        TransientLoginError: Both internal attempts hit unexpected errors.
    """
    logger.debug(f"login_automatic() for {session.account.display_name}")
    # Check if already logged in
    if await check_logged_in(session, timeout=10):
        logger.debug("Already logged in")
        await _on_login_success(session)
        return True

    # Decline cookies popup
    await _clear_pre_login_popups(session)

    # Continue-interstitial fast path: FB sometimes shows a "Continue → enter
    # password → Log in" re-auth screen when there's partial session state
    # (typically from cookies that almost authenticated). Clicking through it
    # is faster than a full form-fill. Only fires if the Continue button is
    # actually visible — defensive 3 s visibility check inside the helper.
    # Must run BEFORE clear_cookies, since the interstitial relies on the
    # partial session cookies being present.
    if await _handle_continue_interstitial(session):
        if await check_logged_in(session, timeout=10.0):
            await _on_login_success(session)
            logger.info(f"Login successful via Continue interstitial for {session.account.display_name}")
            return True
        logger.debug("Continue interstitial ran but login still not confirmed; falling through to form-fill")

    # Wipe any lingering cookies so FB serves the regular login form (bad
    # cookies can otherwise hide the form by making FB think we're partially
    # logged in). No-op when there are no cookies.
    await session._context.clear_cookies()

    # Check if login form is visible
    if not await _is_login_form_visible(session):
        logger.warning(f"Cannot login {session.account.display_name}: no login form and not logged in")
        return False

    logger.info(f"Logging in to Facebook as {session.account.display_name}")
    logger.debug("Login form is visible, proceeding with credentials")

    last_transient: Exception | None = None
    for attempt in range(1, LOGIN_FORM_MAX_ATTEMPTS + 1):
        logger.debug(
            f"Login attempt {attempt}/{LOGIN_FORM_MAX_ATTEMPTS} "
            f"for {session.account.display_name}"
        )
        try:
            # Fill username with human-like typing
            await _human_type(
                session.page.get_by_role('textbox', name='Email or mobile number'),
                session.account.identifier
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Fill password with human-like typing.
            # Use role-based textbox selector: `get_by_label("Password")` also matches
            # the "Show password" button (role=button) which shares the same label.
            await _human_type(
                session.page.get_by_role("textbox", name="Password"),
                session.account.password
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # Click login button
            if session.mobile:
                await session.page.get_by_role('button', name='Log in').click()
            else:
                await session.page.get_by_role('button', name='Log in').nth(0).click()

            logger.info("Login form submitted")
            await asyncio.sleep(5)

            # Classify the post-form URL. Raises CheckpointError / AccountDisabledError
            # on a checkpoint; returns True on a logged-in URL; False if URL never settled.
            url_ok = await _wait_for_log_in_outcome(session)
            if not url_ok:
                # URL stayed on /login or some intermediate page — most commonly a
                # slow/flaky network rather than a credential problem. Treat as
                # transient so the worker rotates without marking the account inactive.
                logger.warning(f"Login URL never settled for {session.account.display_name}")
                raise TransientLoginError(
                    f"Login URL never settled after form submit for {session.account.display_name}"
                )

            # Belt-and-suspenders: GraphQL-level confirmation
            if await check_logged_in(session, timeout=10.0):
                await _on_login_success(session)
                logger.info(f"Login successful for {session.account.display_name}")
                return True

            # Form submitted, URL settled on a logged-in variant, but viewer GraphQL
            # never came through — usually a GraphQL timing race or soft network issue
            # rather than a real credential failure. Treat as transient.
            logger.warning(f"Viewer never came through after form submit for {session.account.display_name}")
            raise TransientLoginError(
                f"Viewer never came through after form submit for {session.account.display_name}"
            )

        except FailedLoginError:
            # CheckpointError / AccountDisabledError — detector already wrote DB state.
            raise
        except Exception as e:
            last_transient = e
            logger.warning(
                f"Transient error on login attempt "
                f"{attempt}/{LOGIN_FORM_MAX_ATTEMPTS} "
                f"for {session.account.display_name}: {e}"
            )
            # Reset for next attempt — reload page and re-verify form is present
            if attempt < LOGIN_FORM_MAX_ATTEMPTS:
                try:
                    await session.page.goto(
                        "https://www.facebook.com", wait_until="domcontentloaded"
                    )
                    await _clear_pre_login_popups(session)
                    if not await _is_login_form_visible(session):
                        # Page landed on a non-form state (e.g., already logged in
                        # via partial submission, or a checkpoint); can't retry.
                        break
                except Exception as reset_err:
                    logger.warning(
                        f"Failed to reset page for retry "
                        f"({session.account.display_name}): {reset_err}"
                    )
                    break
                await asyncio.sleep(random.uniform(1.5, 3.0))

    # Exhausted internal retries — escalate to worker without marking account inactive.
    raise TransientLoginError(
        f"Login failed after {LOGIN_FORM_MAX_ATTEMPTS} transient attempts "
        f"for {session.account.display_name}: {last_transient}"
    )


async def login_with_cookies(session: "BrowserSession") -> bool:
    """Inject the account's persisted cookies and verify via URL classification
    + GraphQL viewer probe.

    Pipeline:
        1. If no cookies stored → return False.
        2. Inject cookies; on add_cookies exception → return False.
        3. Explicit goto(facebook.com) — add_cookies doesn't navigate.
        4. URL classification via `_wait_for_log_in_outcome`. Raises a typed
           exception on `/checkpoint/...` / `/two_step_verification/` URLs;
           returns True for "home"-shaped URLs (could-be-logged-in); returns
           False if URL never settled into anything recognized.
        5. If URL is home-shaped, confirm via `check_logged_in` (viewer-bearing
           GraphQL probe — authoritative for "really logged in").

    Returns:
        True if cookies authenticated us (URL is home + viewer fired); also runs
        `_on_login_success` (refreshes cookies in DB, marks active, etc.).
        False if no cookies / add_cookies failed / URL didn't settle / URL was
        home but viewer never fired (caller should fall through to form login).

    Raises:
        AccountDisabledError / CheckpointError / AutomationCheckpointError if
        FB redirected the navigation to a known failure URL.
    """
    if not session.account.cookies:
        logger.warning(f"No cookies stored for {session.account.display_name}")
        return False

    try:
        await session._context.add_cookies(session.account.cookies)
        logger.info(
            f"Injected {len(session.account.cookies)} cookies for {session.account.display_name}"
        )
    except Exception as e:
        logger.warning(f"Failed to inject cookies for {session.account.display_name}: {e}")
        return False

    # add_cookies doesn't navigate — we need to actually visit FB before any
    # URL classification or viewer probe makes sense.
    await session.page.goto("https://www.facebook.com", wait_until="domcontentloaded")

    # URL classification first — raises typed exception on checkpoint URLs.
    # Returns True for "home"-shaped URLs (necessary-but-not-sufficient for
    # being logged in); False if URL didn't settle into any known pattern.
    if not await _wait_for_log_in_outcome(session):
        logger.warning(
            f"Cookies for {session.account.display_name}: URL never settled into known pattern"
        )
        return False

    # URL is home-shaped. Confirm via viewer-bearing GraphQL probe.
    if not await check_logged_in(session, timeout=10.0):
        logger.warning(
            f"Cookies for {session.account.display_name}: home URL but viewer never fired"
        )
        return False

    await _on_login_success(session)
    return True

async def login_manual(session: "BrowserSession") -> bool:
    """Open facebook.com, drop into a pdb breakpoint so the human can complete
    login by hand (typically through the noVNC viewport when running in the
    container), then return.

    The human's word is taken at face value — no URL classification or viewer
    check after `c`. That's the point of manual: the operator drove it.

    Returns:
        True  if the user typed `c` (continue) at the prompt. Caller is
              responsible for `save_cookies()`.

    Raises:
        FailedLoginError if the user typed `q` (quit) or interrupted with
        Ctrl-C / Ctrl-D — no cookies should be persisted in that case.
    """
    page = session.page
    logger.info(f"login_manual() for {session.account.display_name}")
    await page.goto("https://www.facebook.com", wait_until="domcontentloaded")

    print("\n" + "=" * 60)
    print("MANUAL LOGIN")
    print(f"  Account: {session.account.identifier}")
    print("  Browser is open at facebook.com.")
    print("  Log in manually (use the noVNC viewport at localhost:6080 if you")
    print("  are running in the container).")
    print()
    print("  When done logging in, type 'c' + Enter at the (Pdb) prompt to")
    print("  save cookies.")
    print("  To abort without saving, type 'q' + Enter (or Ctrl-D).")
    print("=" * 60 + "\n")

    try:
        breakpoint()
    except (bdb.BdbQuit, KeyboardInterrupt) as e:
        logger.info("login_manual: aborted by user")
        raise FailedLoginError(
            f"Manual login aborted for {session.account.display_name}"
        ) from e
    return True


async def check_logged_in(session: "BrowserSession", timeout: float = 10.0) -> bool:
    """
    Check if logged in by navigating to facebook.com and waiting for a GraphQL
    response whose body carries a non-null `data.viewer` object. `viewer` is
    Facebook's authenticated-user context — queries referencing it only resolve
    when a live session exists; the Continue/login pages don't answer them.
    This is DOM-independent and, unlike post-bearing responses, doesn't require
    the home feed to render (which may not fire without a scroll).

    Args:
        timeout: Max seconds to wait for a viewer-bearing GraphQL response

    Returns:
        True if a viewer-bearing response was intercepted (logged in), False otherwise
    """
    logger.debug(f"check_logged_in() with timeout={timeout}s")
    # Create a temporary interceptor for this check
    temp_interceptor = ResponseInterceptor()
    temp_interceptor.setup_interception(session.page)

    try:
        await session.page.goto("https://www.facebook.com", wait_until="domcontentloaded")

        # Wait for a viewer-bearing GraphQL response (authenticated user context)
        elapsed = 0.0
        interval = 0.5
        while elapsed < timeout:
            if temp_interceptor.has_viewer_response():
                logger.info(
                    f"Logged in: viewer-bearing response intercepted "
                    f"({temp_interceptor.get_graphql_request_count()} GraphQL responses seen)"
                )
                return True

            await asyncio.sleep(interval)
            await session.page.reload(wait_until="domcontentloaded") # relaod - sometimes
            await asyncio.sleep(interval)
            elapsed += interval

        logger.warning(
            f"Not logged in: no viewer-bearing GraphQL response after {timeout}s "
            f"({temp_interceptor.get_graphql_request_count()} generic GraphQL responses seen)"
        )
        return False
    finally:
        temp_interceptor.stop_interception()


# ==================== Post-success bookkeeping ====================

async def _on_login_success(session: "BrowserSession") -> None:
    """Post-successful-login bookkeeping: persist cookies, mark account active,
    reset stale scroll counts, update last_used, and clear post-login popups."""
    # Save cookies after successful login
    await session.save_cookies()
    # Mark account as active and clear any previous error message
    await session.pool.set_active(session.account.identifier, True, None)
    # Reset scroll_count_overall_24h if last_used was over 24h ago
    if session.account.last_used:
        time_since_last_used = datetime.now(timezone.utc) - session.account.last_used.replace(tzinfo=timezone.utc)
        if time_since_last_used > timedelta(hours=24):
            await session.pool.reset_scroll_counts(session.account.identifier)
            logger.info(
                f"Reset scroll counts for {session.account.display_name} "
                f"(last used {time_since_last_used} ago)"
            )
    # Update last_used timestamp
    await session.pool.update_last_used(session.account.identifier)
    # Clear any post-login popups
    await _clear_post_login_popups(session)


# ==================== Obstacle handlers ====================

async def _handle_continue_interstitial(session: "BrowserSession") -> bool:
    """Facebook 'Continue' re-auth screen: click Continue, submit password, click Log in."""
    if not await _continue_to_login_is_visible(session):
        return False
    await _pass_continue_button(session)
    return True


async def _continue_to_login_is_visible(session: "BrowserSession") -> bool:
    try:
        await session.page.get_by_label("Continue", exact=False).wait_for(state="visible", timeout=3000)
        logger.debug("Continue for login is visible")
        return True
    except Exception as e:
        logger.debug(f"Continue for login is not visible: {e}")
        return False


async def _pass_continue_button(session: "BrowserSession") -> None:
    """Walk through Facebook's 'Continue → enter password → Log in' interstitial."""
    # click on the 'Continue' button if it's there
    try:
        await session.page.get_by_label("Continue", exact=False).click(timeout=10000)
        logger.debug("Clicked post-login 'Continue' button")
        await asyncio.sleep(2)
    except Exception as e:
        logger.debug(f"Failed to click post-login 'Continue' button: {e}")
        return

    # insert the password for the login
    try:
        await _human_type(
            session.page.get_by_role("textbox", name="Password"),
            session.account.password
        )
        logger.debug("Filled post-login 'Password' field")
        await asyncio.sleep(2)
    except Exception as e:
        logger.debug(f"Failed to fill post-login 'Password' field: {e}")
        return

    # press login button
    try:
        await session.page.get_by_role("button", name="Log in", exact=True).click(timeout=10000)
        logger.debug("Clicked post-login 'Log in' button")
        await asyncio.sleep(2)
    except Exception as e:
        logger.debug(f"Failed to click post-login 'Log in' button: {e}")
        return

    # now check if you've hit some issues logging in
    await _wait_for_log_in_outcome(session)


# ==================== Post-form URL classification ====================

async def _wait_for_log_in_outcome(session: "BrowserSession") -> bool:
    """Wait for the post-login-form URL to settle and classify the outcome.

    Outcomes table: (URL-path-suffix regex, outcome kind). Order matters —
    most-specific first (so /checkpoint/disabled/ wins over /checkpoint/,
    and the `/?home` / end-of-host / query-only patterns are kept narrow
    so they don't swallow /login/, /zuck, /recover/, etc.). Adding a new
    FB login flow is one row. The wait regex is the union of all rows;
    the dispatch iterates the same table in order and returns the first
    per-row pattern that matches.

    On a known terminal failure (`disabled`, `checkpoint`, `two_factor`)
    we persist `error_msg` + mark the account inactive *before* raising,
    so higher layers don't need a second DB write.
    """
    # "home" is a necessary-but-not-sufficient outcome — FB serves these URLs to
    # both logged-in users (real home feed) AND logged-out users (login form
    # rendered on /). Callers MUST follow up with a viewer-bearing GraphQL probe
    # (`check_logged_in`) before declaring success.
    outcomes: list[tuple[str, str]] = [
        (r"/checkpoint/disabled/",   "disabled"),
        (r"/checkpoint/",            "checkpoint"),
        (r"/two_step_verification/", "two_factor"),
        (r"/two_factor/",            "two_factor"),
        (r"/?home",                  "home"),  # /home, /home.php
        (r"/?$",                     "home"),  # bare root (host or host/)
        (r"/?\?",                    "home"),  # query-only (host?... / host/?...)
    ]
    host_re = r"https://(?:www|m|web|mbasic)\.facebook\.com"
    wait_re = re.compile(
        rf"^{host_re}(?:{'|'.join(f'(?:{p})' for p, _ in outcomes)})"
    )
    dispatch: list[tuple[re.Pattern, str]] = [
        (re.compile(rf"^{host_re}{p}"), kind) for p, kind in outcomes
    ]

    try:
        await session.page.wait_for_url(wait_re, timeout=5000)
    except Exception as e:
        logger.debug(
            f"No known login-outcome URL after 5s: {e} "
            f"(last url={session.page.url})"
        )
        return False

    url = session.page.url
    for pattern, kind in dispatch:
        if pattern.match(url):
            return await _dispatch_login_outcome(session, kind, url)

    # Unreachable as long as the wait regex and the dispatch list are
    # built from the same table — log and bail just in case.
    logger.warning(f"URL matched login-outcome wait regex but no handler: {url}")
    return False


async def _dispatch_login_outcome(session: "BrowserSession", kind: str, url: str) -> bool:
    """Route a classified login outcome to its handler.

    `home` returns True (URL is FB homepage — caller still owes a viewer-bearing
    GraphQL confirmation). Failure kinds raise the corresponding exception
    — all info needed by the worker is on the exception (`url` attr) and in
    the DB (`error_msg`).

    Disabled / generic-checkpoint / 2FA mark the account inactive here.
    `automation_checkpoint` is the exception: account stays active and the
    worker locks it 24h via `rotate_account(lock_until, error_msg)`.
    """
    if kind == "home":
        # URL is FB homepage — could be logged in OR logged out (login form).
        # Caller must follow up with a viewer-bearing GraphQL probe.
        return True

    # Refine the generic /checkpoint/ kind into automation_checkpoint when the
    # page body carries Facebook's bot-suspicion language. URL alone can't
    # distinguish — /checkpoint/<id>/ is shared with several challenge types.
    if kind == "checkpoint" and await _is_automation_suspected_checkpoint(session):
        msg = f"automation suspected ({url})"
        logger.warning(f"{session.account.display_name}: {msg}")
        raise AutomationCheckpointError(msg, url=url)

    msg_by_kind = {
        "disabled":   f"Account disabled by Facebook ({url})",
        "checkpoint": f"Checkpoint challenge — manual intervention required ({url})",
        "two_factor": f"2FA challenge — manual intervention required ({url})",
    }
    exc_by_kind = {
        "disabled":   AccountDisabledError,
        "checkpoint": CheckpointError,
        "two_factor": CheckpointError,
    }
    msg = msg_by_kind[kind]
    logger.warning(f"{session.account.display_name}: {msg}")
    await session.pool.set_active(session.account.identifier, False, msg)
    raise exc_by_kind[kind](msg, url=url)


async def _is_automation_suspected_checkpoint(session: "BrowserSession") -> bool:
    """Detect Facebook's 'We suspect automated behavior on your account'
    checkpoint variant by probing the page body. URL pattern alone is shared
    with ID-upload / SMS-code / etc. challenges, so only content disambiguates."""
    try:
        await session.page.get_by_text(
            "suspect automated behavior", exact=False
        ).first.wait_for(state="visible", timeout=2000)
        return True
    except Exception:
        return False


# ==================== Page helpers (popups, form detection, typing) ====================

async def _is_login_form_visible(session: "BrowserSession") -> bool:
    """Check if Facebook login form is visible"""
    try:
        await session.page.get_by_label("Email or phone number").or_(
            session.page.get_by_label("Password")
        ).or_(
            session.page.get_by_role("button", name="Log in", exact=True)
        ).first.wait_for(state="visible", timeout=5000)
        return True
    except Exception:
        return False


async def _decline_optional_cookies(session: "BrowserSession") -> bool:
    """Decline optional cookies popup if present"""
    try:
        await session.page.get_by_role('button', name='Decline optional cookies').nth(0).click(timeout=5000)
        logger.info("Declined optional cookies")
        return True
    except Exception:
        return False


async def _close_firefox_startup_overlay(session: "BrowserSession") -> None:
    """Close Firefox startup overlay if present"""
    try:
        await session.page.get_by_role('button', name='Close').nth(0).click(timeout=5000)
        logger.info("Closed Firefox startup overlay")
    except Exception:
        pass


async def _close_not_now_pop(session: "BrowserSession") -> None:
    """Close Not Now popup if present"""
    try:
        label = 'Not now' if session.mobile else 'Not Now'
        await session.page.get_by_role('button', name=label).nth(0).click(timeout=5000)
        logger.info("Closed Not Now popup")
    except Exception:
        pass


async def _clear_pre_login_popups(session: "BrowserSession") -> None:
    """Dismiss pre-login popup dialogs"""
    await _decline_optional_cookies(session)


async def _clear_post_login_popups(session: "BrowserSession") -> None:
    """Dismiss post-login popup dialogs"""
    await _close_firefox_startup_overlay(session)
    await _close_not_now_pop(session)


async def _human_type(locator: Locator, text: str, mean_delay: float = 0.1, std_dev: float = 0.03) -> None:
    """
    Type text with human-like timing using a normal distribution for delays.

    Args:
        locator: Playwright locator to type into
        text: Text to type
        mean_delay: Mean delay between keystrokes in seconds (default 100ms)
        std_dev: Standard deviation of delay in seconds (default 30ms)
    """
    await locator.click()
    for char in text:
        await locator.press(char)
        # Sample delay from normal distribution, clamp to avoid negative/extreme values
        delay = max(0.02, random.gauss(mean_delay, std_dev))
        await asyncio.sleep(delay)
