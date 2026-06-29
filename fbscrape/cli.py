"""
CLI for managing Facebook scraper accounts
"""

import asyncio
import click
import json
import os
from datetime import datetime, timezone
from tabulate import tabulate
import gzip


def _open_scrape_input(path: str):
    """Open a saved scrape result for reading as text.

    Sniffs the first two bytes — gzip magic (`1f 8b`) wins regardless of
    file extension, so a misnamed `.json` file containing gzip bytes
    (saved before ScrapingResult.save's auto-`.gz`-append landed) still
    decodes correctly. Falls back to plain text open otherwise.
    """
    with open(path, 'rb') as f:
        magic = f.read(2)
    if magic == b'\x1f\x8b':
        return gzip.open(path, 'rt')
    return open(path, 'rt')


def _find_unstick_cursor(
    data: list[dict],
    endpoint: str,
    rank: int = 3,
) -> tuple[str, dict] | None:
    """Pick the per-edge cursor of the rank-th chronologically-oldest post
    with a non-null `cursor` field in `data`.

    Used to escape a `no_new_posts_streak` deadlock when the saved
    `last_cursor` anchors at a position where FB serves only already-
    collected posts. Swapping `last_cursor` to a deeper anchor jumps the
    next resume past the dedup wall and into uncovered territory.

    The default `rank=3` skips the very-oldest post (which is often a
    bootstrap-edge highlight outlier — FB injects one chronologically
    out-of-position anchor post per batch). If the rank-th post lacks a
    cursor (every 3rd post in our saved files does — also a bootstrap-
    edge artifact in the parser's fan-out), walks forward to the next
    cursored post.

    Returns `(cursor, diagnostic)` where diagnostic is a small dict for
    logging — or None when the file has too few timestamped/cursored
    posts to satisfy `rank`.
    """
    # Local import to avoid putting fbscrape.response in cli.py's top-level imports.
    from .response import FacebookGraphQLParser
    parser = FacebookGraphQLParser()

    items: list[tuple[int, int, str | None]] = []  # (created_at, idx, cursor)
    for i, rec in enumerate(data):
        try:
            flat = parser.flatten(rec, endpoint=endpoint)
        except Exception:
            continue
        ct = flat.get('created_at') if flat else None
        cursor = rec.get('cursor')
        if isinstance(ct, (int, float)):
            items.append((int(ct), i, cursor))
    items.sort(key=lambda x: x[0])  # oldest first

    if len(items) < rank:
        return None
    # From the rank-th oldest, walk forward to the first cursored post. The
    # rank-1 skip dodges the bootstrap-edge highlight outlier.
    for cur_rank, (ct, idx, cursor) in enumerate(items[rank - 1:], start=rank):
        if cursor:
            return (cursor, {
                "chosen_rank": cur_rank,
                "chosen_idx": idx,
                "chosen_created_at": ct,
            })
    return None

from .accounts_pool import AccountsPool
from .utils import gather, get_home_dir_path, utc


def get_default_db():
    return os.path.join(get_home_dir_path(), "db", "accounts.db")


def _format_locks(locks: dict) -> str:
    """Render an Account.locks dict as 'queue:<timestamp> (<relative>)' entries."""
    if not locks:
        return '-'
    now = utc.now()
    parts = []
    for queue, expiry in locks.items():
        delta = (expiry - now).total_seconds()
        ts = str(expiry)[:19]
        abs_delta = abs(delta)
        if abs_delta < 60:
            rel = f"{int(abs_delta)}s"
        elif abs_delta < 3600:
            rel = f"{int(abs_delta / 60)}m"
        else:
            hours = int(abs_delta // 3600)
            mins = int((abs_delta % 3600) // 60)
            rel = f"{hours}h{mins}m"
        suffix = f"in {rel}" if delta > 0 else f"{rel} ago"
        parts.append(f"{queue}:{ts} ({suffix})")
    return ', '.join(parts)


def run_async(coro):
    """Helper to run async functions from sync CLI commands"""
    return asyncio.run(coro)


@click.group()
@click.option('--db', default=None, help='Path to accounts database')
@click.pass_context
def cli(ctx, db):
    """Facebook Scraper CLI - Manage accounts and scraping"""
    ctx.ensure_object(dict)
    ctx.obj['db'] = db or get_default_db()


@cli.group()
def account():
    """Manage scraper accounts"""
    pass


# ============== Account Management ==============

@account.command()
@click.option('--email', default=None, help='Account email')
@click.option('--phone', default=None, help='Account phone number')
@click.option('--password', required=True, help='Account password')
@click.option('--username', default=None, help='Facebook username')
@click.option('--email-password', default=None, help='Email account password')
@click.option('--proxy', default=None, help='Proxy server URL')
@click.option('--proxy-user', default=None, help='Proxy username')
@click.option('--proxy-pass', default=None, help='Proxy password')
@click.option('--cookies', default=None, help='Cookies (JSON string or file path)')
@click.pass_context
def add(ctx, email, phone, password, username, email_password, proxy, proxy_user, proxy_pass, cookies):
    """Add a new account"""
    if not email and not phone:
        raise click.UsageError("Must provide either --email or --phone")

    async def _add():
        pool = AccountsPool(ctx.obj['db'])

        # Load cookies from file if path provided
        cookie_data = cookies
        if cookies and os.path.exists(cookies):
            with open(cookies) as f:
                cookie_data = f.read()

        await pool.add_account(
            password=password,
            email=email,
            phone_number=phone,
            username=username,
            email_password=email_password,
            proxy_server=proxy,
            proxy_username=proxy_user,
            proxy_password=proxy_pass,
            cookies=cookie_data,
        )
        identifier = email or phone
        click.echo(f"Added account: {identifier}")

    run_async(_add())


@account.command(name='add-from-file')
@click.argument('filepath')
@click.option('--format', 'fmt', default='email:password',
              help='Line format (e.g., "email:password" or "phone:password:email_password")')
@click.pass_context
def add_from_file(ctx, filepath, fmt):
    """Add accounts from a file (one per line)"""
    if not os.path.exists(filepath):
        raise click.UsageError(f"File not found: {filepath}")

    fields = fmt.split(':')

    async def _add():
        pool = AccountsPool(ctx.obj['db'])
        added = 0
        failed = 0

        with open(filepath) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                parts = line.split(':')
                if len(parts) < len(fields):
                    click.echo(f"Line {line_num}: Not enough fields, skipping")
                    failed += 1
                    continue

                # Build kwargs from format
                kwargs = {}
                for i, field in enumerate(fields):
                    value = parts[i] if i < len(parts) else None
                    if field == 'email':
                        kwargs['email'] = value
                    elif field == 'phone':
                        kwargs['phone_number'] = value
                    elif field == 'password':
                        kwargs['password'] = value
                    elif field == 'email_password':
                        kwargs['email_password'] = value
                    elif field == 'username':
                        kwargs['username'] = value

                if 'password' not in kwargs:
                    click.echo(f"Line {line_num}: No password field, skipping")
                    failed += 1
                    continue

                if 'email' not in kwargs and 'phone_number' not in kwargs:
                    click.echo(f"Line {line_num}: No email or phone, skipping")
                    failed += 1
                    continue

                try:
                    await pool.add_account(**kwargs)
                    added += 1
                except Exception as e:
                    click.echo(f"Line {line_num}: Error - {e}")
                    failed += 1

        click.echo(f"Added {added} accounts, {failed} failed")

    run_async(_add())


@account.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'delete_all', is_flag=True, help='Delete all accounts')
@click.option('--inactive', is_flag=True, help='Delete only inactive accounts')
@click.pass_context
def delete(ctx, identifier, delete_all, inactive):
    """Delete account(s) by email or phone"""
    if not identifier and not delete_all and not inactive:
        raise click.UsageError("Provide identifier(s) or use --all/--inactive")

    async def _delete():
        pool = AccountsPool(ctx.obj['db'])

        if delete_all:
            accounts = await pool.get(None)
            if not accounts:
                click.echo("No accounts to delete")
                return
            if not click.confirm(f"Delete ALL {len(accounts)} accounts?"):
                return
            await pool.delete_account([a.identifier for a in accounts])
            click.echo(f"Deleted {len(accounts)} accounts")

        elif inactive:
            accounts = await pool.get_inactive_accounts()
            if not accounts:
                click.echo("No inactive accounts to delete")
                return
            if not click.confirm(f"Delete {len(accounts)} inactive accounts?"):
                return
            await pool.delete_account([a.identifier for a in accounts])
            click.echo(f"Deleted {len(accounts)} inactive accounts")

        else:
            await pool.delete_account(list(identifier))
            click.echo(f"Deleted {len(identifier)} account(s)")

    run_async(_delete())


@account.command(name='list')
@click.option('--active', is_flag=True, help='Show only active accounts')
@click.option('--inactive', is_flag=True, help='Show only inactive accounts')
@click.option('--verbose', '-v', is_flag=True, help='Show all fields')
@click.pass_context
def list_accounts(ctx, active, inactive, verbose):
    """List all accounts"""
    async def _list():
        pool = AccountsPool(ctx.obj['db'])

        if active:
            accounts = await pool.get_active_accounts()
        elif inactive:
            accounts = await pool.get_inactive_accounts()
        else:
            accounts = await pool.get(None)

        if not accounts:
            click.echo("No accounts found")
            return

        if verbose:
            headers = ['Identifier', 'Password', 'Username', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)', 'Locks', 'Error', 'Proxy']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
                    a.password or '-',
                    a.username or '-',
                    'Y' if a.active else 'N',
                    'Y' if a.in_use else 'N',
                    str(a.last_used)[:19] if a.last_used else '-',
                    a.scroll_count_overall_24h,
                    _format_locks(a.locks),
                    (a.error_msg[:30] + '...') if a.error_msg and len(a.error_msg) > 30 else (a.error_msg or '-'),
                    a.proxy_server or '-',
                ])
        else:
            headers = ['Identifier', 'Password', 'Username', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)', 'Locks']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
                    a.password or '-',
                    a.username or '-',
                    'Y' if a.active else 'N',
                    'Y' if a.in_use else 'N',
                    str(a.last_used)[:19] if a.last_used else '-',
                    a.scroll_count_overall_24h,
                    _format_locks(a.locks),
                ])

        click.echo(tabulate(rows, headers=headers, tablefmt='simple'))
        click.echo(f"\nTotal: {len(accounts)} accounts")

    run_async(_list())


@account.command()
@click.argument('identifier')
@click.pass_context
def info(ctx, identifier):
    """Show detailed info for an account"""
    async def _info():
        pool = AccountsPool(ctx.obj['db'])
        try:
            account = await pool.get(identifier)
        except ValueError:
            click.echo(f"Account not found: {identifier}")
            return

        click.echo(f"Account: {account.identifier}")
        click.echo(f"  Username:      {account.username or '-'}")
        click.echo(f"  Email:         {account.email or '-'}")
        click.echo(f"  Phone:         {account.phone_number or '-'}")
        click.echo(f"  Active:        {account.active}")
        click.echo(f"  In Use:        {account.in_use}")
        click.echo(f"  Last Used:     {account.last_used or '-'}")
        click.echo(f"  Fingerprints:  {', '.join(sorted(account.fingerprints)) or '-'}")
        click.echo(f"  Proxy:         {account.proxy_server or '-'}")
        click.echo(f"  Cookies:       {len(account.cookies)} stored")
        click.echo(f"  Scrolls (24h): {account.scroll_count_overall_24h}")
        click.echo(f"  Scrolls/endpoint: {account.scroll_count_per_endpoint_total or '-'}")
        click.echo(f"  Locks:         {_format_locks(account.locks)}")
        click.echo(f"  Error:         {account.error_msg or '-'}")

    run_async(_info())


@account.command()
@click.pass_context
def stats(ctx):
    """Show account pool statistics"""
    async def _stats():
        pool = AccountsPool(ctx.obj['db'])
        s = await pool.stats()

        if not s:
            click.echo("No statistics available")
            return

        click.echo("Account Pool Statistics")
        click.echo("-" * 30)
        click.echo(f"  Total:    {s.get('total', 0)}")
        click.echo(f"  Active:   {s.get('active', 0)}")
        click.echo(f"  Inactive: {s.get('inactive', 0)}")
        click.echo(f"  In Use:   {s.get('in_use', 0)}")

        # Show locked counts per queue
        for key, value in s.items():
            if key.startswith('locked_'):
                queue = key.replace('locked_', '')
                click.echo(f"  Locked ({queue}): {value}")

    run_async(_stats())


# ============== Account Status ==============

@account.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'set_all', is_flag=True, help='Set all accounts')
@click.pass_context
def activate(ctx, identifier, set_all):
    """Mark account(s) as active"""
    if not identifier and not set_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _activate():
        pool = AccountsPool(ctx.obj['db'])
        target = None if set_all else list(identifier)
        await pool.set_active(target, True)
        click.echo(f"Activated {'all accounts' if set_all else len(identifier)}")

    run_async(_activate())


@account.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'set_all', is_flag=True, help='Set all accounts')
@click.option('--error', default=None, help='Error message to set')
@click.pass_context
def deactivate(ctx, identifier, set_all, error):
    """Mark account(s) as inactive"""
    if not identifier and not set_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _deactivate():
        pool = AccountsPool(ctx.obj['db'])
        target = None if set_all else list(identifier)
        await pool.set_active(target, False, error)
        click.echo(f"Deactivated {'all accounts' if set_all else len(identifier)}")

    run_async(_deactivate())


@account.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'reset_all', is_flag=True, help='Reset all accounts')
@click.pass_context
def unlock(ctx, identifier, reset_all):
    """Remove locks from account(s)"""
    if not identifier and not reset_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _unlock():
        pool = AccountsPool(ctx.obj['db'])
        target = None if reset_all else list(identifier)
        await pool.reset_locks(target)
        click.echo(f"Unlocked {'all accounts' if reset_all else len(identifier)}")

    run_async(_unlock())


@account.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'release_all', is_flag=True, help='Release all accounts')
@click.pass_context
def release(ctx, identifier, release_all):
    """Release account(s) from use (set in_use=false)"""
    if not identifier and not release_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _release():
        pool = AccountsPool(ctx.obj['db'])
        target = None if release_all else list(identifier)
        await pool.release_account(target)
        click.echo(f"Released {'all accounts' if release_all else len(identifier)}")

    run_async(_release())


# ============== Field Management ==============

@account.command(name='set')
@click.argument('identifier')
@click.argument('field')
@click.argument('value')
@click.pass_context
def set_field(ctx, identifier, field, value):
    """Set a field value for an account.

    \b
    IDENTIFIER: Account email or phone number
    FIELD: Field name to update
    VALUE: New value (use 'null' for None)

    \b
    Updatable fields:
      password, email, username, email_password, phone_number,
      active, proxy_server, proxy_username, proxy_password,
      error_msg, twofa_id

    \b
    Examples:
      fbscrape account set user@example.com username myusername
      fbscrape account set user@example.com active true
      fbscrape account set user@example.com proxy_server http://proxy:8080
      fbscrape account set user@example.com error_msg null
    """
    async def _set():
        pool = AccountsPool(ctx.obj['db'])

        # Handle special values
        parsed_value = value
        if value.lower() == 'null' or value.lower() == 'none':
            parsed_value = None
        elif value.lower() in ('true', 'false'):
            parsed_value = value.lower() == 'true'

        try:
            await pool.update_field(identifier, field, parsed_value)
            display_value = parsed_value if parsed_value is not None else 'null'
            click.echo(f"Updated {identifier}: {field} = {display_value}")
        except ValueError as e:
            raise click.UsageError(str(e))

    run_async(_set())


@account.command(name='fields')
def list_fields():
    """List all updatable fields for the 'set' command"""
    fields = sorted(AccountsPool._updatable_fields)
    click.echo("Updatable fields for 'fbscrape account set':")
    click.echo("-" * 35)
    for f in fields:
        click.echo(f"  {f}")


# ============== Scroll Management ==============

@account.command(name='reset-scrolls')
@click.argument('identifier', nargs=-1)
@click.option('--all', 'reset_all', is_flag=True, help='Reset all accounts')
@click.option('--endpoint', default=None, help='Reset only specific endpoint')
@click.pass_context
def reset_scrolls(ctx, identifier, reset_all, endpoint):
    """Reset scroll counts for account(s)"""
    if not identifier and not reset_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _reset():
        pool = AccountsPool(ctx.obj['db'])

        if reset_all:
            await pool.reset_scroll_counts(None, endpoint)
            click.echo(f"Reset scroll counts for all accounts" + (f" (endpoint: {endpoint})" if endpoint else ""))
        else:
            for ident in identifier:
                await pool.reset_scroll_counts(ident, endpoint)
            click.echo(f"Reset scroll counts for {len(identifier)} account(s)")

    run_async(_reset())


# ============== Cookie Management ==============

@account.command(name='set-cookies')
@click.argument('identifier')
@click.argument('cookies_file')
@click.pass_context
def set_cookies(ctx, identifier, cookies_file):
    """Set cookies for an account from a file"""
    if not os.path.exists(cookies_file):
        raise click.UsageError(f"File not found: {cookies_file}")

    async def _set():
        pool = AccountsPool(ctx.obj['db'])

        with open(cookies_file) as f:
            cookies = f.read()

        await pool.update_cookies(identifier, cookies)
        click.echo(f"Updated cookies for {identifier}")

    run_async(_set())


@account.command(name='export-cookies')
@click.argument('identifier')
@click.argument('output_file')
@click.pass_context
def export_cookies(ctx, identifier, output_file):
    """Export cookies for an account to a file"""
    import json

    async def _export():
        pool = AccountsPool(ctx.obj['db'])
        try:
            account = await pool.get(identifier)
        except ValueError:
            click.echo(f"Account not found: {identifier}")
            return

        with open(output_file, 'w') as f:
            json.dump(account.cookies, f, indent=2)

        click.echo(f"Exported {len(account.cookies)} cookies to {output_file}")

    run_async(_export())


# ============== Login ==============

@cli.command()
@click.argument('identifier', nargs=-1)
@click.option(
    '--mode',
    type=click.Choice(['manual', 'automatic']),
    default='automatic',
    help='manual: open browser + breakpoint() for human takeover. '
         'automatic: form-fill flow with stored credentials (with --cookies, '
         'try cookies first then fall back to form-fill). '
         '(Default: automatic)',
)
@click.option(
    '--cookies', is_flag=True,
    help="Inject the account's stored cookies into the browser context at "
         'creation. Orthogonal to --mode: with --mode manual the human starts '
         'in an already-logged-in browser if cookies are still valid; with '
         '--mode automatic we cookie-validate via the GraphQL viewer probe '
         'first, then form-fill only if cookies are missing or invalid.',
)
@click.option(
    '--headless/--no-headless',
    default=False,
    help='Run browser headless (auto-resolves to "virtual" on Linux). Default: --no-headless.',
)
@click.option(
    "--log-level",
    type=click.Choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']),
    default='INFO',
    help="Set the logging level. Default: INFO. "
         "DEBUG is useful for debugging, but may log sensitive information. "
         "Use INFO for normal operation. "
         "WARNING is for expected errors. "
         "ERROR is for unexpected errors. "
         "CRITICAL is for critical failures that may prevent the program from running. "
         "Use --log-level DEBUG to see all logs.",
)
@click.pass_context
def login(ctx, identifier, mode, cookies, headless, log_level):
    """Log in to one or more Facebook accounts and persist cookies to the DB.

    IDENTIFIER accepts one or more account identifiers (email or phone). With
    multiple accounts they are processed sequentially — one browser at a time —
    and a per-account summary is printed at the end. A failure on one account
    does not abort the others; the command exits non-zero if any failed.

    \b
    --mode manual: opens facebook.com in a non-headless browser (use noVNC at
        localhost:6080 in the container) and pauses at a (Pdb) prompt.
        Log in by hand, type 'c' + Enter to save cookies; 'q' + Enter
        (or Ctrl-D) to abort without saving. With multiple accounts you are
        prompted once per account, in order.

    \b
    --mode automatic: runs the form-fill login the worker uses on scrape
        start. The account must already have password / email_password
        stored. With --cookies, tries the stored cookies first (via the
        GraphQL viewer probe); on failure, wipes them and falls through to
        form-fill.

    \b
    --cookies: inject stored cookies into the browser context at creation.
        Pairs with either mode. Useful for skipping the form-fill step when
        the saved session is still alive.
    """
    from .browser_session import BrowserSession
    from .login import login_automatic, login_manual, login_with_cookies
    from .exceptions import FailedLoginError
    from .logger import set_log_level

    if not identifier:
        raise click.ClickException("Provide at least one account identifier.")

    async def _login_one(pool, ident) -> bool:
        """Drive login for a single account. Returns True on success, False on
        any per-account failure (already reported to the user)."""
        try:
            account = await pool.get(ident)
        except ValueError:
            click.echo(f"Account not found: {ident}")
            return False

        # auto_login=False so initialize() opens the browser without trying
        # cookies/form login on its own — we drive login explicitly below.
        async with BrowserSession(
            account, pool, headless=headless, auto_login=False
        ) as session:
            if mode == 'manual':
                if cookies:
                    if not session.account.cookies:
                        click.echo(f"--cookies: no stored cookies for {ident}; ignoring flag.")
                    else:
                        try:
                            await session._context.add_cookies(session.account.cookies)
                            click.echo(
                                f"Injected {len(session.account.cookies)} cookies for {ident}."
                            )
                        except Exception as e:
                            click.echo(f"--cookies: injection failed ({e}); continuing without.")
                # login_manual raises FailedLoginError on `q`/Ctrl-C abort.
                try:
                    await login_manual(session)
                except FailedLoginError as e:
                    click.echo(f"Aborted; no cookies saved. ({e})")
                    return False
                # Manual flow doesn't run _on_login_success, so save explicitly.
                await session.save_cookies()
                click.echo(f"Saved cookies for {ident}.")
                return True

            # mode == 'automatic'
            # If --cookies, try the cookie path first. login_with_cookies
            # re-injects them, probes the URL classifier (raises on checkpoint),
            # then confirms via GraphQL viewer. On success it persists fresh
            # cookies and marks the account active.
            try:
                if cookies:
                    if not session.account.cookies:
                        click.echo(f"--cookies: no stored cookies for {ident}; ignoring flag.")
                    elif await login_with_cookies(session):
                        click.echo(f"Login OK via cookies for {ident} (refreshed in DB).")
                        return True
                    else:
                        click.echo("Cookies didn't validate — falling back to form-fill.")

                # login_automatic returns False on "no login form visible",
                # raises typed exceptions on checkpoint / disabled / transient.
                # On success it runs _on_login_success which already saves cookies.
                if not await login_automatic(session):
                    click.echo(f"Automatic login failed for {ident}: no login form visible")
                    return False
                click.echo(f"Login OK for {ident} (cookies persisted).")
                return True
            except FailedLoginError as e:
                # Covers CheckpointError, AccountDisabledError, AutomationCheckpointError,
                # TransientLoginError, and generic FailedLoginError — all subclasses.
                click.echo(f"Login failed for {ident}: {e}")
                return False

    async def _login():
        set_log_level(log_level)

        pool = AccountsPool(ctx.obj['db'])
        results: dict[str, bool] = {}
        for ident in identifier:
            if len(identifier) > 1:
                click.echo(f"\n=== {ident} ({len(results) + 1}/{len(identifier)}) ===")
            results[ident] = await _login_one(pool, ident)

        if len(identifier) > 1:
            ok = [i for i, r in results.items() if r]
            failed = [i for i, r in results.items() if not r]
            click.echo(f"\nDone: {len(ok)} succeeded, {len(failed)} failed.")
            if failed:
                click.echo("Failed: " + ", ".join(failed))

        if not all(results.values()):
            raise click.ClickException("One or more logins failed.")

    run_async(_login())


# ============== Scraping ==============

# Recognized per-row keys in --input-file; everything else is dropped.
# `key_field` is the name of the required identifier field for the endpoint
# (handle for UserTimeline, query_text for Search). Date keys are constant.
# Longest-first so `.parquet.zstd` peels before `.parquet`.
_INPUT_FILE_EXTENSIONS = (
    '.parquet.zstd', '.csv', '.parquet', '.json', '.jsonl', '.ndjson',
    '.yaml', '.yml',
)


def _input_file_ext(path: str) -> str | None:
    """Return the recognized input-file extension for `path`, or None.

    Matches against `_INPUT_FILE_EXTENSIONS` longest-first so compound
    suffixes like `.parquet.zstd` win over `.zstd`.
    """
    lower = path.lower()
    for ext in _INPUT_FILE_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return None


def _load_scrape_targets(
    path: str,
    key_field: str = 'handle',
    extra_keys: tuple[str, ...] = ('start_date', 'end_date'),
    required_extra_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Read scrape targets from a structured file.

    Dispatches on file extension. Each row/entry must have a non-empty
    value for `key_field` and for every name in `required_extra_keys`;
    other names in `extra_keys` are optional. Anything outside those is
    silently dropped. NaN / None / empty-string cells are treated as
    "not supplied" (so a CSV with a sparsely-populated optional column
    is fine).

    Defaults preserve UserTimeline / Search semantics: required `handle`
    (or `query_text`) plus optional `start_date` / `end_date`. Single-shot
    endpoints (e.g., PageTransparency) override `extra_keys` with their
    own recognized field names.
    """
    import json as _json

    ext = _input_file_ext(path)
    if ext is None:
        raise click.UsageError(
            f"Unsupported --input-file extension for {path!r}. "
            f"Supported: {', '.join(_INPUT_FILE_EXTENSIONS)}"
        )

    if ext == '.csv':
        import csv
        with open(path, newline='') as f:
            raw_rows = list(csv.DictReader(f))
    elif ext in ('.parquet', '.parquet.zstd'):
        import polars as pl
        raw_rows = pl.read_parquet(path).to_dicts()
    elif ext in ('.yaml', '.yml'):
        import yaml
        with open(path) as f:
            doc = yaml.safe_load(f)
        if isinstance(doc, dict):
            raw_rows = [doc]
        elif isinstance(doc, list):
            raw_rows = doc
        else:
            raise click.UsageError(
                f"{path}: YAML must be a list of objects or a single object, "
                f"got {type(doc).__name__}"
            )
    elif ext == '.json':
        with open(path) as f:
            doc = _json.load(f)
        if isinstance(doc, dict):
            raw_rows = [doc]
        elif isinstance(doc, list):
            raw_rows = doc
        else:
            raise click.UsageError(
                f"{path}: JSON must be a list of objects or a single object, "
                f"got {type(doc).__name__}"
            )
    else:  # .jsonl / .ndjson
        raw_rows = []
        with open(path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError as e:
                    raise click.UsageError(f"{path}:{line_num} invalid JSON: {e}")
                if not isinstance(obj, dict):
                    raise click.UsageError(
                        f"{path}:{line_num} expected JSON object, "
                        f"got {type(obj).__name__}"
                    )
                raw_rows.append(obj)

    recognized_keys = (key_field, *extra_keys)
    required = (key_field, *required_extra_keys)
    targets: list[dict] = []
    for i, row in enumerate(raw_rows, 1):
        if not isinstance(row, dict):
            raise click.UsageError(
                f"{path} row {i}: expected object/dict, got {type(row).__name__}"
            )
        target: dict = {}
        for key in recognized_keys:
            if key not in row:
                continue
            val = row[key]
            if val is None:
                continue
            if isinstance(val, float) and val != val:  # NaN
                continue
            if isinstance(val, str):
                val = val.strip()
                if not val:
                    continue
            target[key] = val
        for req in required:
            if req not in target:
                raise click.UsageError(
                    f"{path} row {i}: missing or empty `{req}`"
                )
        targets.append(target)

    if not targets:
        raise click.UsageError(f"{path}: no rows found")

    return targets


def _resolve_targets(
    keys, input_file, start_date, end_date, key_field: str = 'handle',
    require_start: bool = True, require_end: bool = True,
    default_end_to_today: bool = True,
) -> list[dict]:
    """Resolve identifiers + dates from CLI flags and/or --input-file into a
    flat list of {<key_field>, start_date, end_date} dicts.

    `keys` is the tuple of positional CLI args (handles for UserTimeline,
    query texts for Search). `key_field` names the per-row identifier field.

    Per-endpoint policy via the boolean flags:
      - `require_start` — when False, missing start_date is allowed (None).
      - `require_end` — when False, missing end_date is allowed (None).
      - `default_end_to_today` — when True, a missing end_date is filled
        with today's UTC date (mirrors FB UI fingerprint for UserTimeline).
        Independent of `require_end`; both can be False to allow truly
        open upper bounds (GroupTimeline) or only one False to require
        a server-side default (Search keeps `True, True, True`).

    Enforces:
    - exactly one of (positional `keys`, --input-file) is supplied
    - if the file supplies a start_date for any row, --start-date must NOT be set
    - same exclusivity for end_date
    """
    if keys and input_file:
        raise click.UsageError(
            f"Cannot use both positional {key_field}s and --input-file. Pick one."
        )
    if not keys and not input_file:
        raise click.UsageError(
            f"Must provide either positional {key_field}s or --input-file."
        )

    if input_file:
        targets = _load_scrape_targets(input_file, key_field=key_field)
        if start_date is not None and any('start_date' in t for t in targets):
            raise click.UsageError(
                "Input file supplies start_date and --start-date is also set. "
                "Pick one source."
            )
        if end_date is not None and any('end_date' in t for t in targets):
            raise click.UsageError(
                "Input file supplies end_date and --end-date is also set. "
                "Pick one source."
            )
    else:
        targets = [{key_field: k} for k in keys]

    today = utc.now().strftime("%Y-%m-%d")
    resolved = []
    for t in targets:
        sd = t.get('start_date') or start_date
        if sd is None and require_start:
            raise click.UsageError(
                f"start_date missing for {key_field} {t[key_field]!r} "
                f"(no row value, no --start-date flag)"
            )
        ed = t.get('end_date') or end_date
        if ed is None and default_end_to_today:
            ed = today
        if ed is None and require_end:
            raise click.UsageError(
                f"end_date missing for {key_field} {t[key_field]!r} "
                f"(no row value, no --end-date flag)"
            )
        resolved.append({
            key_field: t[key_field],
            'start_date': sd,
            'end_date': ed,
        })
    return resolved


def _build_stem(handle: str, endpoint: str, mode: str) -> str:
    """File-name stem for saved scrape results.

    Dates are intentionally NOT included — the saved JSON's `query.query`
    field carries the actual scrape parameters, and the stem is the key
    that `--continue` / `--skip-existing` match on. One stem per (handle,
    endpoint, mode) means a rolling archive across multiple runs.
    """
    return f"{handle.replace('.', '_')}_{endpoint}_{mode}"


def _commentslist_post_id_label(post_id: str, max_len: int = 24) -> str:
    """Filename-safe label for a post_id, capped so pfbid forms don't blow
    out the filename. Numeric ids (≤ ~17 digits) pass through verbatim;
    pfbid-style strings get truncated. Resume relies on the full post_id
    in the saved JSON's `query.query`, not on this label."""
    s = post_id.replace('.', '_').replace('/', '_')
    return s if len(s) <= max_len else s[:max_len]


def _build_stem_for_query(query) -> str:
    """Per-endpoint stem builder. Most endpoints key on `handle`; CommentsList
    additionally encodes a truncated post_id label so one (handle, post_id)
    pair has a unique on-disk stem.
    """
    endpoint = query.endpoint
    mode = query.mode
    if endpoint == "CommentsList":
        handle = query.query["handle"]
        post_id = query.query["post_id"]
        return (
            f"{handle.replace('.', '_')}_"
            f"{_commentslist_post_id_label(post_id)}_"
            f"{endpoint}_{mode}"
        )
    if endpoint == "Search":
        query_text = query.query["query_text"]
        return _build_stem(_sanitize_query_for_filename(query_text), endpoint, mode)
    return _build_stem(query.query["handle"], endpoint, mode)


def _existing_output_for_stem(output_dir: str, stem: str) -> str | None:
    """Return path to a prior output for this stem, or None. Prefers the current
    `.jsonl.gz` format, then legacy whole-file envelopes (`.json.gz` / `.json`)
    so `--continue` / `--skip-existing` match across the migration."""
    for ext in ('.jsonl.gz', '.json.gz', '.json'):
        p = os.path.join(output_dir, f"{stem}{ext}")
        if os.path.exists(p):
            return p
    return None


@cli.group()
def scrape():
    """Run scraping jobs"""
    pass


@scrape.command(name='user-timeline')
@click.argument('handles', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read (handle, start_date?, end_date?) rows from a CSV, '
                   'Parquet, YAML, or JSON/JSONL file. Mutually exclusive with '
                   'positional handles. If the file supplies start_date / '
                   'end_date columns, the matching CLI flag must NOT be set.')
@click.option('--start-date', default=None,
              help='Start date YYYY-MM-DD (how far back to scrape). Optional — '
                   'when omitted there is no client-side lower bound and the '
                   'scrape relies on end-of-feed / no-new-posts / max-posts '
                   'termination.')
@click.option('--end-date', default=None,
              help='End date YYYY-MM-DD (most recent date to scrape from). '
                   'Default: today (UTC) — mirrors FB UI fingerprint, which '
                   'always sends `beforeTime`. Mutually exclusive with an '
                   'end_date column in --input-file.')
@click.option('--output-dir', default=None, help='Directory to save results (default: data/posts/)')
@click.option('--max-sessions', default=2, type=int, help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int, help='Scrolls (or hybrid paginations) before rotating account')
@click.option('--stall-timeout-seconds', default=None, type=int, help='[manual] bail out if no GraphQL response for N seconds (default 300, ignored for --mode hybrid)')
@click.option('--headless/--no-headless', default=True,
              help='Run browsers headless (default). Pass --no-headless to '
                   'see the browser window — useful for debugging login flows '
                   'or watching a scrape live.')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--mode', type=click.Choice(['manual', 'hybrid']), default='hybrid',
              help="Scrape strategy: 'manual' (scroll-driven) or 'hybrid' "
                   "(page.request POSTs, no scroll-induced DOM growth — default). "
                   "See docs/hybrid/overview.md.")
@click.option('--pagination-count', type=int, default=None,
              help='[hybrid] posts per pagination request (default 3, matches FB UI)')
@click.option('--scroll-burst-every', type=int, default=None,
              help='[hybrid] organic scroll burst every N paginations (default 10, set very high to disable)')
@click.option('--scroll-burst-min', type=int, default=None,
              help='[hybrid] minimum scrolls per organic burst (default 2)')
@click.option('--scroll-burst-max', type=int, default=None,
              help='[hybrid] maximum scrolls per organic burst (default 5)')
@click.option('--max-paginations', type=int, default=None,
              help='[hybrid] safety cap on paginations per session (default -1 = no cap)')
@click.option('--max-posts', type=int, default=None,
              help='[hybrid] cap on total accumulated posts (default -1 = no cap). '
                   'Checked at batch boundaries, so actual count can exceed by up '
                   'to pagination_count-1.')
@click.option('--pagination-sleep-mean', type=float, default=None,
              help='[hybrid] mean inter-pagination sleep seconds (default 2.5)')
@click.option('--pagination-sleep-std', type=float, default=None,
              help='[hybrid] std dev of inter-pagination sleep (default 0.5)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='[hybrid] max seconds to wait for first ProfileCometTimelineFeedRefetchQuery (default 20)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='[hybrid] pause after navigating to the profile, before bootstrap scroll (default 3)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='[hybrid] per-request timeout for page.request.post in milliseconds (default 30000)')
@click.option('--max-no-progress-streak', type=int, default=None,
              help='[hybrid] bail after N consecutive paginations with no new posts (default 5)')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='[hybrid] per-await safety timeout for hangs (default 900)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up instead of '
                   'raising NoAccountError when the pool is empty/locked. Useful for '
                   'long-running scrapes — only aborts if the pool has zero active accounts.')
@click.option('--skip-existing', is_flag=True,
              help='Skip targets whose output JSON file already exists in --output-dir '
                   '(filename matches handle, endpoint, mode, start_date, end_date). '
                   'Useful for resuming a partially-completed batch.')
@click.option('--continue', 'continue_', is_flag=True,
              help='[hybrid only] For each target, if a matching output file '
                   'exists in --output-dir, resume the scrape from its '
                   '`last_cursor` and seed the dedup set with its post_ids. '
                   'New posts are merged into the existing file. The cursor '
                   'is fed into leg 0 only; post-cursor_reset legs continue '
                   'to start fresh with adjusted end_date. Mutually exclusive '
                   'with --skip-existing. Errors when combined with '
                   '--mode manual (no cursor concept).')
@click.pass_context
def scrape_user_timeline(
    ctx, handles, input_file, start_date, end_date, output_dir, max_sessions,
    scroll_threshold, stall_timeout_seconds, headless, mobile, log_level,
    mode, pagination_count, scroll_burst_every, scroll_burst_min, scroll_burst_max,
    max_paginations, max_posts, pagination_sleep_mean, pagination_sleep_std,
    template_capture_timeout, post_nav_sleep_seconds, request_timeout_ms,
    max_no_progress_streak, operation_timeout_seconds, wait_for_account,
    skip_existing, continue_,
):
    """Scrape a user's timeline between two dates.

    \b
    Examples:
      fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
      fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --end-date 2025-01-01 --headless

    \b
    Read targets from a file (CSV / Parquet / YAML / JSON / JSONL):
      fbscrape scrape user-timeline --input-file targets.csv
      fbscrape scrape user-timeline --input-file handles.yaml --start-date 2024-01-01

    \b
    Force the scroll-driven path:
      fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01 \\
        --mode manual
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    if skip_existing and continue_:
        raise click.UsageError(
            "--skip-existing and --continue are mutually exclusive: one drops "
            "targets that already have output, the other resumes them. Pick one."
        )
    if continue_ and mode == 'manual':
        raise click.UsageError(
            "--continue requires --mode hybrid (manual scroll-driven mode has "
            "no cursor concept). Re-run with --mode hybrid (the default)."
        )

    # UserTimeline: start_date is optional (client-side stop only); end_date
    # auto-fills today to mirror FB UI fingerprint (which always sends
    # `beforeTime`).
    targets = _resolve_targets(
        handles, input_file, start_date, end_date,
        require_start=False, require_end=False, default_end_to_today=True,
    )

    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", "posts")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    if skip_existing:
        # File existing means a prior run reached `data.save(...)` — it does
        # NOT guarantee the scrape succeeded (errors are saved too). Delete
        # files for failed handles if you want them retried.
        before = len(targets)
        targets = [
            t for t in targets
            if _existing_output_for_stem(
                output_dir, _build_stem(t['handle'], 'UserTimeline', mode)
            ) is None
        ]
        skipped = before - len(targets)
        if skipped:
            logger.info(f"--skip-existing: {skipped}/{before} targets already have output files in {output_dir}")
        if not targets:
            logger.info("Nothing to scrape.")
            return

    # Bundle mode-specific kwargs. Only forward keys explicitly set on the CLI —
    # None falls through to the registry default in Query.__post_init__.
    # `stall_timeout_seconds` is manual-only; routed only when --mode manual to
    # avoid Query rejecting it as an unknown param under hybrid.
    mode_params = {
        "pagination_count": pagination_count,
        "scroll_burst_every": scroll_burst_every,
        "max_paginations": max_paginations,
        "max_posts": max_posts,
        "pagination_sleep_mean": pagination_sleep_mean,
        "pagination_sleep_std": pagination_sleep_std,
        "template_capture_timeout": template_capture_timeout,
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "request_timeout_ms": request_timeout_ms,
        "max_no_progress_streak": max_no_progress_streak,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    # scroll_burst_size_range is a tuple in the registry; expose as two flags
    # on the CLI and rebuild the tuple when both are set.
    if scroll_burst_min is not None or scroll_burst_max is not None:
        from .models import Query
        default_min, default_max = Query.ENDPOINT_REGISTRY["UserTimeline"]["modes"]["hybrid"]["params"]["scroll_burst_size_range"]
        mode_params["scroll_burst_size_range"] = (
            scroll_burst_min if scroll_burst_min is not None else default_min,
            scroll_burst_max if scroll_burst_max is not None else default_max,
        )
    if mode == 'manual' and stall_timeout_seconds is not None:
        mode_params['stall_timeout_seconds'] = stall_timeout_seconds
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    def _existing_output_path(target: dict) -> str | None:
        """Return path to a prior `.json` / `.json.gz` for this target, or None."""
        return _existing_output_for_stem(
            output_dir, _build_stem(target['handle'], 'UserTimeline', mode)
        )

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            finalize_tasks: list[asyncio.Task] = []
            finalize_sem = asyncio.Semaphore(MERGE_CONCURRENCY)
            async for result in gather(
                scraper.user_timeline(
                    handle=t['handle'],
                    start_date=t['start_date'],
                    end_date=t['end_date'],
                    mode=mode,
                    resume_from=_existing_output_path(t) if continue_ else None,
                    **mode_params,
                )
                for t in targets
            ):
                # Merge + save off the event loop: lets the next yielded
                # result enter this loop body immediately instead of
                # stalling behind a multi-minute json.load + gzip-write.
                finalize_tasks.append(asyncio.create_task(
                    _finalize_guarded(finalize_sem, result, output_dir, continue_)
                ))
            if finalize_tasks:
                await asyncio.gather(*finalize_tasks)

    run_async(_scrape())


@scrape.command(name='group-timeline')
@click.argument('handles', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read (handle, start_date?, end_date?) rows from a CSV, '
                   'Parquet, YAML, or JSON/JSONL file. `handle` may be a '
                   'vanity group handle (e.g. "albertaseparatism") or the '
                   'numeric group id. Mutually exclusive with positional '
                   'handles. If the file supplies start_date / end_date '
                   'columns, the matching CLI flag must NOT be set.')
@click.option('--start-date', default=None,
              help='Start date YYYY-MM-DD (how far back to scrape). Optional — '
                   'when omitted there is no client-side lower bound.')
@click.option('--end-date', default=None,
              help='End date YYYY-MM-DD (advisory: FB has no server-side date '
                   'filter for group feeds, so this only bounds client-side '
                   'stops like ConsecutiveOutOfRange). Optional — when '
                   'omitted there is no client-side upper bound, matching '
                   'FB\'s UI (which sends no date filter on group feeds).')
@click.option('--output-dir', default=None, help='Directory to save results (default: data/posts/)')
@click.option('--max-sessions', default=2, type=int, help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int, help='Paginations before rotating account')
@click.option('--headless/--no-headless', default=True,
              help='Run browsers headless (default). Pass --no-headless to '
                   'see the browser window — useful for debugging login flows '
                   'or watching a scrape live.')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--pagination-count', type=int, default=None,
              help='posts per pagination request (default 3, matches FB UI)')
@click.option('--sorting-setting', type=str, default=None,
              help='Group feed sort, sent as variables.sortingSetting on every '
                   'replay. Known-valid: "TOP_POSTS" (default — FB UI default; '
                   'algorithmic ranking; lowest-fingerprint choice; termination '
                   'relies on --max-consecutive-out-of-range since posts arrive '
                   'non-monotonically), "CHRONOLOGICAL" (stream-line tail '
                   'descending by post creation_time; closest to true creation-'
                   'time ordering but empirically associated with account '
                   'suspensions on this endpoint — opt-in only), "RECENT_ACTIVITY" '
                   '(sorts by most recent comment/reaction; treated as non-'
                   'chronological). Other values may be accepted by FB silently.')
@click.option('--scroll-burst-every', type=int, default=None,
              help='organic scroll burst every N paginations (default 10, set very high to disable)')
@click.option('--scroll-burst-min', type=int, default=None,
              help='minimum scrolls per organic burst (default 2)')
@click.option('--scroll-burst-max', type=int, default=None,
              help='maximum scrolls per organic burst (default 5)')
@click.option('--max-paginations', type=int, default=None,
              help='safety cap on paginations per session (default -1 = no cap)')
@click.option('--max-posts', type=int, default=None,
              help='cap on total accumulated posts (default -1 = no cap). '
                   'Checked at batch boundaries, so actual count can exceed '
                   'by up to pagination_count-1.')
@click.option('--pagination-sleep-mean', type=float, default=None,
              help='mean inter-pagination sleep seconds (default 2.5)')
@click.option('--pagination-sleep-std', type=float, default=None,
              help='std dev of inter-pagination sleep (default 0.5)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='max seconds to wait for first GroupsCometFeedRegularStoriesPaginationQuery (default 20)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='pause after navigating to the group, before bootstrap scroll (default 3)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='per-request timeout for page.request.post in milliseconds (default 30000)')
@click.option('--max-no-progress-streak', type=int, default=None,
              help='bail after N consecutive paginations with no new posts (default 30)')
@click.option('--max-consecutive-out-of-range', type=int, default=None,
              help='bail after N posts in a row outside [start_date, end_date] '
                   '(default 20). Primary date-tail stop under non-chronological '
                   'sorts (TOP_POSTS, RECENT_ACTIVITY) where oldest-in-batch is '
                   'unreliable; kept enabled on CHRONOLOGICAL too as belt-and-'
                   'suspenders against bootstrap-edge highlights. -1 disables.')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='per-await safety timeout for hangs (default 900)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up instead of '
                   'raising NoAccountError when the pool is empty/locked.')
@click.option('--skip-existing', is_flag=True,
              help='Skip targets whose output JSON file already exists in --output-dir.')
@click.option('--continue', 'continue_', is_flag=True,
              help='For each target, if a matching output file exists in '
                   '--output-dir, resume the scrape from its `last_cursor` and '
                   'seed the dedup set with its post_ids. New posts are merged '
                   'into the existing file. Mutually exclusive with '
                   '--skip-existing. Per FB docs, cursors are ephemeral '
                   '(server-side state), so a stale cursor may yield empty '
                   'results or trip the cursor-reset detector — partial data '
                   'is still preserved in either case.')
@click.pass_context
def scrape_group_timeline(
    ctx, handles, input_file, start_date, end_date, output_dir, max_sessions,
    scroll_threshold, headless, mobile, log_level,
    pagination_count, sorting_setting, scroll_burst_every, scroll_burst_min,
    scroll_burst_max, max_paginations, max_posts, pagination_sleep_mean,
    pagination_sleep_std, template_capture_timeout, post_nav_sleep_seconds,
    request_timeout_ms, max_no_progress_streak, max_consecutive_out_of_range,
    operation_timeout_seconds, wait_for_account, skip_existing, continue_,
):
    """Scrape a group's feed between two dates (hybrid mode only).

    \b
    Examples:
      fbscrape scrape group-timeline albertaseparatism --start-date 2024-01-01 --end-date 2025-01-01
      fbscrape scrape group-timeline 787909081545196 --start-date 2024-01-01

    \b
    Read targets from a file:
      fbscrape scrape group-timeline --input-file groups.csv

    Notes:
      - `handle` accepts a vanity group handle OR the numeric group id.
      - GraphQL has no server-side date filter for group feeds, so
        --end-date is advisory; termination relies on client-side stop
        conditions (oldest-in-batch on CHRONOLOGICAL, consecutive-out-of-
        range on non-chronological sorts).
      - Default --sorting-setting is "TOP_POSTS" (matches FB's UI;
        empirically safer than CHRONOLOGICAL for sustained scraping).
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    if skip_existing and continue_:
        raise click.UsageError(
            "--skip-existing and --continue are mutually exclusive: one drops "
            "targets that already have output, the other resumes them. Pick one."
        )

    mode = 'hybrid'  # Only mode supported for GroupTimeline.
    # GroupTimeline: both dates fully optional (FB's UI sends no date filter
    # on group feeds, so the cleanest fingerprint match is to send nothing
    # unless the user explicitly bounds the scrape).
    targets = _resolve_targets(
        handles, input_file, start_date, end_date,
        require_start=False, require_end=False, default_end_to_today=False,
    )

    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", "posts")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    if skip_existing:
        before = len(targets)
        targets = [
            t for t in targets
            if _existing_output_for_stem(
                output_dir, _build_stem(t['handle'], 'GroupTimeline', mode)
            ) is None
        ]
        skipped = before - len(targets)
        if skipped:
            logger.info(f"--skip-existing: {skipped}/{before} targets already have output files in {output_dir}")
        if not targets:
            logger.info("Nothing to scrape.")
            return

    mode_params = {
        "pagination_count": pagination_count,
        "sorting_setting": sorting_setting,
        "scroll_burst_every": scroll_burst_every,
        "max_paginations": max_paginations,
        "max_posts": max_posts,
        "pagination_sleep_mean": pagination_sleep_mean,
        "pagination_sleep_std": pagination_sleep_std,
        "template_capture_timeout": template_capture_timeout,
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "request_timeout_ms": request_timeout_ms,
        "max_no_progress_streak": max_no_progress_streak,
        "max_consecutive_out_of_range": max_consecutive_out_of_range,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    if scroll_burst_min is not None or scroll_burst_max is not None:
        from .models import Query
        default_min, default_max = Query.ENDPOINT_REGISTRY["GroupTimeline"]["modes"]["hybrid"]["params"]["scroll_burst_size_range"]
        mode_params["scroll_burst_size_range"] = (
            scroll_burst_min if scroll_burst_min is not None else default_min,
            scroll_burst_max if scroll_burst_max is not None else default_max,
        )
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    def _existing_output_path(target: dict) -> str | None:
        """Return path to a prior `.json` / `.json.gz` for this target, or None."""
        return _existing_output_for_stem(
            output_dir, _build_stem(target['handle'], 'GroupTimeline', mode)
        )

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            finalize_tasks: list[asyncio.Task] = []
            finalize_sem = asyncio.Semaphore(MERGE_CONCURRENCY)
            async for result in gather(
                scraper.group_timeline(
                    handle=t['handle'],
                    start_date=t['start_date'],
                    end_date=t['end_date'],
                    resume_from=_existing_output_path(t) if continue_ else None,
                    **mode_params,
                )
                for t in targets
            ):
                # Merge + save off the event loop: see `scrape user-timeline`.
                finalize_tasks.append(asyncio.create_task(
                    _finalize_guarded(finalize_sem, result, output_dir, continue_)
                ))
            if finalize_tasks:
                await asyncio.gather(*finalize_tasks)

    run_async(_scrape())


# Cap on concurrent --continue finalize/merge tasks. Each streams its prior file
# disk→disk (KDD 25), so per-merge memory is bounded by the new leg; this cap is
# cheap insurance against many merges thrashing disk/CPU at once.
MERGE_CONCURRENCY = 3


async def _finalize_guarded(sem, data, output_dir, continue_):
    """Run `_finalize_continue_result` off the event loop under a concurrency
    cap (semaphore-bounded), so completions keep yielding without N merges
    contending at once."""
    async with sem:
        await asyncio.to_thread(_finalize_continue_result, data, output_dir, continue_)


def _finalize_continue_result(
    data: 'ScrapingResult',
    output_dir: str,
    continue_: bool,
) -> None:
    """Save the leg as JSONL; on `--continue`, APPEND to the rolling archive.

    Synchronous so it can be dispatched via `asyncio.to_thread` — running it
    inline in the async for-loop would block other workers' completions behind
    the gzip write.

    JSONL makes `--continue` an append (a new gzip member), not a whole-file
    rewrite — the memory floor is the new leg, never the prior-file size. A
    legacy whole-file envelope prior is migrated to JSONL once (self-healing),
    then appended to. On a `no_new_posts_streak` resume leg, a deeper cursor is
    chosen (auto-unstick) and appended as a trailing status line so the next
    resume jumps past the dedup wall.
    """
    from .logger import logger
    from .jsonl_store import convert_envelope_to_jsonl, looks_like_jsonl
    handle = data.query.query.get('handle')
    stem = _build_stem_for_query(data.query)
    dest = os.path.join(output_dir, f"{stem}.jsonl.gz")

    if not continue_:
        data.save(dest, compress=True)
        return

    prior = _existing_output_for_stem(output_dir, stem)  # .jsonl.gz | .json.gz | .json | None
    if prior is None:
        data.save(dest, compress=True)
    elif looks_like_jsonl(prior):
        # Already JSONL — append the new leg's lines (no rewrite). In steady
        # state `prior == dest`; if it's a differently-named .jsonl, append there.
        target = dest if prior == dest else prior
        data.save(target, compress=target.endswith('.gz'), append=True)
        dest = target
    else:
        # Legacy whole-file envelope → migrate to the JSONL dest once, then append.
        n, _ = convert_envelope_to_jsonl(prior, dest)
        logger.info(f"@{handle}: migrated legacy envelope ({n} posts) → {os.path.basename(dest)}")
        if prior != dest and os.path.exists(prior):
            try:
                os.remove(prior)
            except OSError:
                pass
        data.save(dest, compress=True, append=True)

    if data.result == 'no_new_posts_streak':
        _append_unstick_line(dest, data, handle)


def _append_unstick_line(dest: str, data: 'ScrapingResult', handle) -> None:
    """On a `no_new_posts_streak` --continue leg, pick the rank-3 chronologically
    oldest cursored post in the (merged) file and append a status-only line
    carrying that cursor — so the next `--continue`'s tail-read resumes from a
    deeper anchor, past the dedup wall. Best-effort; no-op if too few cursored
    posts. Streams the file via `iter_posts` (a generator) so `_find_unstick_cursor`
    holds only the (created_at, idx, cursor) tuples, never the full posts —
    bounded memory even on a large merged archive."""
    from .logger import logger
    from .jsonl_store import iter_posts, JsonlPostWriter
    try:
        chosen = _find_unstick_cursor(iter_posts(dest), endpoint=data.query.endpoint, rank=3)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"@{handle}: auto-unstick scan failed ({type(e).__name__}: {e})")
        return
    if not chosen:
        return
    new_cursor, diag = chosen
    writer = JsonlPostWriter(dest, data.query.to_dict(), data.time_started,
                             append=True, compress=dest.endswith('.gz'))
    writer.finalize(data.result, data.time_taken, last_cursor=new_cursor)  # status-only line
    logger.info(
        f"@{handle}: no_new_posts_streak on --continue — auto-unstuck cursor to "
        f"rank #{diag['chosen_rank']} (data[{diag['chosen_idx']}])"
    )


def _sanitize_query_for_filename(s: str) -> str:
    """Map free-form search text to a safe filename component."""
    return s.replace(' ', '_').replace('/', '_').replace('.', '_')


def _resolve_page_transparency_targets(
    pairs: tuple[str, ...], input_file: str | None,
) -> list[dict]:
    """Resolve page_id targets (with optional handle) from CLI or --input-file.

    Positional args are accepted in two forms:
      - bare `<page_id>` (e.g. `899800046546098`)
      - `<handle>:<page_id>` (e.g. `habsfanhub:899800046546098`)

    File form expects a `page_id` column (required) and optional `handle`
    column. Mutually exclusive with positional args.
    """
    if pairs and input_file:
        raise click.UsageError(
            "Cannot use both positional args and --input-file."
        )
    if not pairs and not input_file:
        raise click.UsageError(
            "Must provide either positional args ('<page_id>' or "
            "'<handle>:<page_id>') or --input-file."
        )

    if input_file:
        return _load_scrape_targets(
            input_file,
            key_field='page_id',
            extra_keys=('handle',),
            required_extra_keys=(),
        )

    targets = []
    for s in pairs:
        s = s.strip()
        if not s:
            raise click.UsageError("Empty positional arg.")
        if ':' in s:
            handle, page_id = s.split(':', 1)
            handle = handle.strip() or None
            page_id = page_id.strip()
        else:
            handle = None
            page_id = s
        if not page_id:
            raise click.UsageError(f"Positional arg {s!r}: page_id required.")
        target: dict = {'page_id': page_id}
        if handle:
            target['handle'] = handle
        targets.append(target)
    return targets


def _resolve_profile_authenticity_targets(
    user_ids: tuple[str, ...], input_file: str | None,
) -> list[dict]:
    """Resolve user_id targets from CLI flags or --input-file.

    Positional args are accepted as bare numeric user IDs. The file form
    expects a `user_id` column and is mutually exclusive with positional args.
    """
    if user_ids and input_file:
        raise click.UsageError(
            "Cannot use both positional user_id args and --input-file."
        )
    if not user_ids and not input_file:
        raise click.UsageError(
            "Must provide either positional user_id args or --input-file."
        )

    if input_file:
        return _load_scrape_targets(
            input_file,
            key_field='user_id',
            extra_keys=(),
        )

    targets = []
    for s in user_ids:
        s = s.strip()
        if not s:
            raise click.UsageError("Empty user_id positional arg.")
        targets.append({'user_id': s})
    return targets


def _resolve_handle_pair_targets(
    pairs: tuple[str, ...], input_file: str | None, paired_key: str,
) -> list[dict]:
    """Generic 'handle:<paired>' resolver shared by single-shot endpoints."""
    if pairs and input_file:
        raise click.UsageError(
            f"Cannot use both positional 'handle:{paired_key}' pairs and --input-file."
        )
    if not pairs and not input_file:
        raise click.UsageError(
            f"Must provide either positional 'handle:{paired_key}' pairs or --input-file."
        )

    if input_file:
        return _load_scrape_targets(
            input_file,
            key_field='handle',
            extra_keys=(paired_key,),
            required_extra_keys=(paired_key,),
        )

    targets = []
    for s in pairs:
        if ':' not in s:
            raise click.UsageError(
                f"Positional arg {s!r} must be 'handle:{paired_key}'."
            )
        handle, paired = s.split(':', 1)
        handle = handle.strip()
        paired = paired.strip()
        if not handle or not paired:
            raise click.UsageError(
                f"Positional arg {s!r}: both handle and {paired_key} required."
            )
        targets.append({'handle': handle, paired_key: paired})
    return targets


def _parse_filter_options(
    filter_opts: tuple[str, ...],
    raw_filter_opts: tuple[tuple[str, str, str], ...],
) -> dict | None:
    """Parse ``--filter`` and ``--raw-filter`` CLI values into a filters dict.

    ``--filter`` formats::

        NAME                 no-arg filter      e.g. ``recent_posts``
        NAME.key=value       known filter kwarg e.g. ``creation_time.start=2025-01-01``
        NAME=JSON            full kwargs        e.g. ``creation_time={"start":"2025-01-01"}``

    ``--raw-filter`` format (three space-separated values)::

        FB_KEY NAME ARGS_JSON   e.g. ``city:0 city '{"city_id":"123"}'``
    """
    import json as _json
    filters: dict = {}
    for v in filter_opts:
        if '=' in v:
            lhs, rhs = v.split('=', 1)
            if '.' in lhs:
                name, kwarg = lhs.rsplit('.', 1)
                filters.setdefault(name, {})[kwarg] = rhs
            else:
                try:
                    filters[lhs] = _json.loads(rhs)
                except _json.JSONDecodeError:
                    raise click.UsageError(
                        f"--filter {v!r}: value after '=' must be valid JSON"
                    )
        else:
            filters[v] = {}
    for fb_key, name, args_json in raw_filter_opts:
        filters[fb_key] = {"name": name, "args": args_json}
    return filters or None


@scrape.command(name='search')
@click.argument('queries', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read query_text rows from a CSV, Parquet, YAML, or JSON/JSONL '
                   'file. Mutually exclusive with positional queries.')
@click.option('--filter', 'filter_opts', multiple=True, metavar='NAME[.key=value|=JSON]',
              help='Add a search filter. May be specified multiple times. '
                   'Formats: "recent_posts" (no args), '
                   '"creation_time.start=YYYY-MM-DD" or "creation_time.end=YYYY-MM-DD" '
                   '(date bounds), "NAME=JSON" (full kwargs as JSON). '
                   'Example: --filter recent_posts --filter creation_time.start=2025-01-01 '
                   '--filter creation_time.end=2025-12-31')
@click.option('--raw-filter', 'raw_filter_opts', type=(str, str, str), multiple=True,
              metavar='FB_KEY NAME ARGS_JSON',
              help='Add a raw filter blob entry for unknown filter types. '
                   'Provide the FB outer key, inner name, and args JSON string. '
                   'Example: --raw-filter "city:0" "city" \'{"city_id":"123"}\'')
@click.option('--output-dir', default=None,
              help='Directory to save results (default: data/posts/)')
@click.option('--max-sessions', default=2, type=int,
              help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int,
              help='Hybrid paginations before rotating account')
@click.option('--headless', is_flag=True, help='Run browsers headless')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--pagination-count', type=int, default=None,
              help='[hybrid] posts per pagination request (default 5, matches FB UI for search)')
@click.option('--scroll-burst-every', type=int, default=None,
              help='[hybrid] organic scroll burst every N paginations (default 10, set very high to disable)')
@click.option('--scroll-burst-min', type=int, default=None,
              help='[hybrid] minimum scrolls per organic burst (default 2)')
@click.option('--scroll-burst-max', type=int, default=None,
              help='[hybrid] maximum scrolls per organic burst (default 5)')
@click.option('--max-paginations', type=int, default=None,
              help='[hybrid] safety cap on paginations per session (default -1 = no cap)')
@click.option('--max-posts', type=int, default=None,
              help='[hybrid] cap on total accumulated posts (default -1 = no cap). '
                   'Checked at batch boundaries, so actual count can exceed by up '
                   'to pagination_count-1.')
@click.option('--pagination-sleep-mean', type=float, default=None,
              help='[hybrid] mean inter-pagination sleep seconds (default 2.5)')
@click.option('--pagination-sleep-std', type=float, default=None,
              help='[hybrid] std dev of inter-pagination sleep (default 0.5)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='[hybrid] max seconds to wait for first SearchCometResultsPaginatedResultsQuery (default 20)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='[hybrid] pause after navigating to the search URL, before bootstrap scroll (default 3)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='[hybrid] per-request timeout for page.request.post in milliseconds (default 30000)')
@click.option('--max-no-progress-streak', type=int, default=None,
              help='[hybrid] bail after N consecutive paginations with no new posts (default 5)')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='[hybrid] per-await safety timeout for hangs (default 900)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up instead of '
                   'raising NoAccountError when the pool is empty/locked.')
@click.option('--skip-existing', is_flag=True,
              help='Skip queries whose output JSON file already exists in --output-dir.')
@click.pass_context
def scrape_search(
    ctx, queries, input_file, filter_opts, raw_filter_opts, output_dir, max_sessions,
    scroll_threshold, headless, mobile, log_level,
    pagination_count, scroll_burst_every, scroll_burst_min, scroll_burst_max,
    max_paginations, max_posts, pagination_sleep_mean, pagination_sleep_std,
    template_capture_timeout, post_nav_sleep_seconds, request_timeout_ms,
    max_no_progress_streak, operation_timeout_seconds, wait_for_account,
    skip_existing,
):
    """Scrape Facebook search results.

    \b
    Targets the SearchCometResultsPaginatedResultsQuery GraphQL endpoint.
    Filters (sort order, date range, etc.) are optional — without any
    --filter flags Facebook returns results under its default ranking.
    Hybrid mode only.

    \b
    Examples:
      fbscrape scrape search 'mark carney'
      fbscrape scrape search 'mark carney' --filter recent_posts --filter creation_time.start=2025-01-01 --filter creation_time.end=2025-12-31
      fbscrape scrape search 'mark carney' 'pierre poilievre' --filter recent_posts --headless

    \b
    Read targets from a file (CSV / Parquet / YAML / JSON / JSONL):
      fbscrape scrape search --input-file queries.csv
      fbscrape scrape search --input-file queries.yaml --filter recent_posts
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    filters = _parse_filter_options(filter_opts, raw_filter_opts)

    if queries and input_file:
        raise click.UsageError("Cannot use both positional queries and --input-file. Pick one.")
    if not queries and not input_file:
        raise click.UsageError("Must provide either positional queries or --input-file.")

    if input_file:
        rows = _load_scrape_targets(input_file, key_field='query_text', extra_keys=())
        targets = [{'query_text': row['query_text'], 'filters': filters} for row in rows]
    else:
        targets = [{'query_text': q, 'filters': filters} for q in queries]

    mode = 'hybrid'

    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", "posts")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    def _stem_for_target(t: dict) -> str:
        return _build_stem(_sanitize_query_for_filename(t['query_text']), 'Search', mode)

    if skip_existing:
        before = len(targets)
        targets = [
            t for t in targets
            if _existing_output_for_stem(output_dir, _stem_for_target(t)) is None
        ]
        skipped = before - len(targets)
        if skipped:
            logger.info(f"--skip-existing: {skipped}/{before} queries already have output files in {output_dir}")
        if not targets:
            logger.info("Nothing to scrape.")
            return

    mode_params = {
        "pagination_count": pagination_count,
        "scroll_burst_every": scroll_burst_every,
        "max_paginations": max_paginations,
        "max_posts": max_posts,
        "pagination_sleep_mean": pagination_sleep_mean,
        "pagination_sleep_std": pagination_sleep_std,
        "template_capture_timeout": template_capture_timeout,
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "request_timeout_ms": request_timeout_ms,
        "max_no_progress_streak": max_no_progress_streak,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    if scroll_burst_min is not None or scroll_burst_max is not None:
        from .models import Query
        default_min, default_max = Query.ENDPOINT_REGISTRY["Search"]["modes"]["hybrid"]["params"]["scroll_burst_size_range"]
        mode_params["scroll_burst_size_range"] = (
            scroll_burst_min if scroll_burst_min is not None else default_min,
            scroll_burst_max if scroll_burst_max is not None else default_max,
        )
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            async for result in gather(
                scraper.search(
                    query_text=t['query_text'],
                    filters=t['filters'],
                    mode=mode,
                    **mode_params,
                )
                for t in targets
            ):
                qt = result.query.query.get('query_text')
                stem = _stem_for_target({'query_text': qt})
                outpath = os.path.join(output_dir, f"{stem}.jsonl.gz")
                result.save(outpath)
                logger.info(
                    f"[search] {qt!r}: {result.result}, "
                    f"{result.num_records} records → {outpath}"
                )

    run_async(_scrape())


@scrape.command(name='comments-list')
@click.argument('pairs', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read (handle, post_id) rows from a CSV, Parquet, YAML, '
                   'or JSON/JSONL file. Both columns are required. '
                   'Mutually exclusive with positional args.')
@click.option('--output-dir', default=None,
              help='Directory to save results (default: data/comments/)')
@click.option('--max-sessions', default=2, type=int,
              help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int,
              help='Paginations before rotating account')
@click.option('--headless/--no-headless', default=True,
              help='Run browsers headless (default). Pass --no-headless to see '
                   'the browser window — useful for debugging login flows.')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO',
              help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--comments-after-count', type=int, default=None,
              help='variables.commentsAfterCount on each replay. -1 (default) '
                   'mirrors FB UI (server picks ~10 per page).')
@click.option('--feed-location', type=str, default=None,
              help='variables.feedLocation (default "POST_PERMALINK_DIALOG").')
@click.option('--scroll-burst-every', type=int, default=None,
              help='organic scroll burst every N paginations (default 50)')
@click.option('--scroll-burst-min', type=int, default=None,
              help='minimum scrolls per organic burst (default 2)')
@click.option('--scroll-burst-max', type=int, default=None,
              help='maximum scrolls per organic burst (default 5)')
@click.option('--max-paginations', type=int, default=None,
              help='safety cap on paginations per session (default -1 = no cap)')
@click.option('--max-results', type=int, default=None,
              help='cap on total accumulated comments (default -1 = no cap). '
                   'Checked at batch boundaries; actual count can exceed by '
                   'up to ~one page.')
@click.option('--pagination-sleep-mean', type=float, default=None,
              help='mean inter-pagination sleep seconds (default 2.5)')
@click.option('--pagination-sleep-std', type=float, default=None,
              help='std dev of inter-pagination sleep (default 0.5)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='max seconds to wait for first '
                   'CommentsListComponentsPaginationQuery (default 20)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='pause after navigating to the post permalink (default 3)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='per-request timeout for page.request.post in milliseconds '
                   '(default 30000)')
@click.option('--max-no-progress-streak', type=int, default=None,
              help='bail after N consecutive paginations with no new comments '
                   '(default 5)')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='per-await safety timeout for hangs (default 900)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up instead '
                   'of raising NoAccountError when the pool is empty/locked.')
@click.option('--skip-existing', is_flag=True,
              help='Skip targets whose output JSON file already exists.')
@click.option('--continue', 'continue_', is_flag=True,
              help='Resume each target from its prior saved file (matches on '
                   '<handle>_<post_id>_CommentsList_hybrid stem). '
                   'Mutually exclusive with --skip-existing.')
@click.pass_context
def scrape_comments_list(
    ctx, pairs, input_file, output_dir, max_sessions, scroll_threshold,
    headless, mobile, log_level, comments_after_count, feed_location,
    scroll_burst_every, scroll_burst_min, scroll_burst_max, max_paginations,
    max_results, pagination_sleep_mean, pagination_sleep_std,
    template_capture_timeout, post_nav_sleep_seconds, request_timeout_ms,
    max_no_progress_streak, operation_timeout_seconds, wait_for_account,
    skip_existing, continue_,
):
    """Scrape top-level comments on a post (hybrid mode only).

    \b
    Each target needs both a handle (drives the navigation URL) and a
    post_id (numeric form OR pfbid form — both work in
    /<handle>/posts/<post_id>/). The base64 `feedback:<id>` GraphQL
    variable is captured from the natural request template.

    \b
    Examples:
      fbscrape scrape comments-list brianlilley:pfbid0FocuLnBJtzSwMWrdRtkAX8oLDYM9koTY7Ph8RKVTTX9wxKNL8EDshFTohjmixSo9l
      fbscrape scrape comments-list zuck:10115311901107991 --headless
      fbscrape scrape comments-list --input-file posts.csv

    \b
    Notes:
      - Exhaustion-only by default — set --max-results to cap.
      - Comments are returned non-chronologically by FB's "Most Relevant"
        ranking; no date filtering applies.
      - Replies (depth>0) are NOT collected here — each comment carries
        `replies_total_count` for callers that want to drill into a
        separate reply-fetching endpoint.
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    if skip_existing and continue_:
        raise click.UsageError(
            "--skip-existing and --continue are mutually exclusive: one drops "
            "targets that already have output, the other resumes them. Pick one."
        )

    mode = 'hybrid'
    # Reuse the generic 'handle:post_id' resolver — same shape as
    # PageTransparency's `handle:page_id` form.
    targets = _resolve_handle_pair_targets(pairs, input_file, paired_key='post_id')

    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", "comments")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    def _stem_for_target(target: dict) -> str:
        return (
            f"{target['handle'].replace('.', '_')}_"
            f"{_commentslist_post_id_label(target['post_id'])}_"
            f"CommentsList_{mode}"
        )

    if skip_existing:
        before = len(targets)
        targets = [
            t for t in targets
            if _existing_output_for_stem(output_dir, _stem_for_target(t)) is None
        ]
        skipped = before - len(targets)
        if skipped:
            logger.info(
                f"--skip-existing: {skipped}/{before} targets already have "
                f"output files in {output_dir}"
            )
        if not targets:
            logger.info("Nothing to scrape.")
            return

    mode_params = {
        "comments_after_count": comments_after_count,
        "feed_location": feed_location,
        "scroll_burst_every": scroll_burst_every,
        "max_paginations": max_paginations,
        "pagination_sleep_mean": pagination_sleep_mean,
        "pagination_sleep_std": pagination_sleep_std,
        "template_capture_timeout": template_capture_timeout,
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "request_timeout_ms": request_timeout_ms,
        "max_no_progress_streak": max_no_progress_streak,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    if scroll_burst_min is not None or scroll_burst_max is not None:
        from .models import Query
        default_min, default_max = (
            Query.ENDPOINT_REGISTRY["CommentsList"]["modes"]["hybrid"]
            ["params"]["scroll_burst_size_range"]
        )
        mode_params["scroll_burst_size_range"] = (
            scroll_burst_min if scroll_burst_min is not None else default_min,
            scroll_burst_max if scroll_burst_max is not None else default_max,
        )
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    def _existing_output_path(target: dict) -> str | None:
        return _existing_output_for_stem(output_dir, _stem_for_target(target))

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            finalize_tasks: list[asyncio.Task] = []
            finalize_sem = asyncio.Semaphore(MERGE_CONCURRENCY)
            async for result in gather(
                scraper.comments_list(
                    handle=t['handle'],
                    post_id=t['post_id'],
                    max_results=max_results if max_results is not None else -1,
                    resume_from=_existing_output_path(t) if continue_ else None,
                    **mode_params,
                )
                for t in targets
            ):
                finalize_tasks.append(asyncio.create_task(
                    _finalize_guarded(finalize_sem, result, output_dir, continue_)
                ))
            if finalize_tasks:
                await asyncio.gather(*finalize_tasks)

    run_async(_scrape())


@scrape.command(name='page-transparency')
@click.argument('pairs', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read (handle, page_id) rows from a CSV, Parquet, YAML, '
                   'or JSON/JSONL file. Both columns are required.')
@click.option('--output-dir', default=None,
              help='Directory to save results (default: data/page_transparency/)')
@click.option('--max-sessions', default=2, type=int,
              help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int,
              help='Paginations before rotating account')
@click.option('--headless', is_flag=True, help='Run browsers headless')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO',
              help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='[hybrid] pause after navigating to the profile, before '
                   'capturing the GraphQL template (default 3)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='[hybrid] max seconds to wait for any natural GraphQL POST '
                   'to harvest auth-bearing fields (default 20)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='[hybrid] per-request timeout for the transparency POST '
                   'in milliseconds (default 30000)')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='[hybrid] per-await safety timeout for hangs (default 120)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up.')
@click.pass_context
def scrape_page_transparency(
    ctx, pairs, input_file, output_dir, max_sessions, scroll_threshold,
    headless, mobile, log_level, post_nav_sleep_seconds,
    template_capture_timeout, request_timeout_ms, operation_timeout_seconds,
    wait_for_account,
):
    """Scrape Facebook page transparency info for one or more pages.

    \b
    Single-shot — no pagination, no date range. Each target needs both a
    handle (drives bootstrap navigation) and a numeric page_id (sent as
    `variables.pageID` in the synthesized ProfileTransparencyDialogQuery).

    \b
    Examples:
      fbscrape scrape page-transparency habsfanhub:899800046546098
      fbscrape scrape page-transparency 899800046546098 100044331674441 --headless
      fbscrape scrape page-transparency habsfanhub:899800046546098

    \b
    Read targets from a file (CSV / Parquet / YAML / JSON / JSONL) with a
    required `page_id` column and an optional `handle` column:
      fbscrape scrape page-transparency --input-file pages.csv
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    targets = _resolve_page_transparency_targets(pairs, input_file)

    if output_dir is None:
        output_dir = os.path.join(
            get_home_dir_path(), "data", "page_transparency",
        )
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    mode_params = {
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "template_capture_timeout": template_capture_timeout,
        "request_timeout_ms": request_timeout_ms,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            async for result in gather(
                scraper.page_transparency(
                    page_id=t['page_id'],
                    handle=t.get('handle'),
                    **mode_params,
                )
                for t in targets
            ):
                data: ScrapingResult = result
                page_id = data.query.query.get('page_id')
                handle = data.query.query.get('handle')
                label = handle or page_id

                ts = utc.now().strftime("%Y%m%dT%H%M%SZ")
                filename = (
                    f"{label.replace('.', '_')}"
                    f"_pagetransparency_{ts}.jsonl"
                )
                data.save(os.path.join(output_dir, filename))

    run_async(_scrape())


@scrape.command(name='profile-authenticity')
@click.argument('user_ids', nargs=-1)
@click.option('--input-file', default=None, type=click.Path(exists=True),
              help='Read user_id rows from a CSV, Parquet, YAML, or '
                   'JSON/JSONL file with a `user_id` column.')
@click.option('--output-dir', default=None,
              help='Directory to save results (default: data/profile_authenticity/)')
@click.option('--max-sessions', default=5, type=int,
              help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=5000, type=int,
              help='Paginations before rotating account')
@click.option('--headless', is_flag=True, help='Run browsers headless')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO',
              help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.option('--scale', type=int, default=None,
              help='[hybrid] image scale variable (default 3, matches FB UI)')
@click.option('--post-nav-sleep-seconds', type=float, default=None,
              help='[hybrid] pause after navigating to the profile, before '
                   'capturing the GraphQL template (default 3)')
@click.option('--template-capture-timeout', type=float, default=None,
              help='[hybrid] max seconds to wait for any natural GraphQL POST '
                   'to harvest auth-bearing fields (default 20)')
@click.option('--request-timeout-ms', type=int, default=None,
              help='[hybrid] per-request timeout for the authenticity POST '
                   'in milliseconds (default 30000)')
@click.option('--operation-timeout-seconds', type=float, default=None,
              help='[hybrid] per-await safety timeout for hangs (default 120)')
@click.option('--wait-for-account', is_flag=True,
              help='Block (polling every 5s) until an account frees up.')
@click.pass_context
def scrape_profile_authenticity(
    ctx, user_ids, input_file, output_dir, max_sessions, scroll_threshold,
    headless, mobile, log_level, scale, post_nav_sleep_seconds,
    template_capture_timeout, request_timeout_ms, operation_timeout_seconds,
    wait_for_account,
):
    """Scrape Facebook profile authenticity info for one or more profiles.

    \b
    Single-shot — no pagination, no date range. Each target is a numeric
    user_id (sent as `variables.userID` in the synthesized
    ProfileCometDirectoryAuthenticityModalQuery). Bootstrap navigation hits
    https://www.facebook.com/<user_id>/, which FB redirects to the
    canonical profile — no handle resolution needed.

    \b
    Examples:
      fbscrape scrape profile-authenticity 100044331674441
      fbscrape scrape profile-authenticity 100044331674441 4 --headless

    \b
    Read targets from a file (CSV / Parquet / YAML / JSON / JSONL) with
    a `user_id` column:
      fbscrape scrape profile-authenticity --input-file profiles.csv
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level, logger
    from .models import ScrapingResult

    set_log_level(log_level)

    targets = _resolve_profile_authenticity_targets(user_ids, input_file)

    if output_dir is None:
        output_dir = os.path.join(
            get_home_dir_path(), "data", "profile_authenticity",
        )
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    mode_params = {
        "scale": scale,
        "post_nav_sleep_seconds": post_nav_sleep_seconds,
        "template_capture_timeout": template_capture_timeout,
        "request_timeout_ms": request_timeout_ms,
        "operation_timeout_seconds": operation_timeout_seconds,
    }
    mode_params = {k: v for k, v in mode_params.items() if v is not None}

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            raise_when_no_account=not wait_for_account,
        ) as scraper:
            async for result in gather(
                scraper.profile_authenticity(
                    user_id=t['user_id'],
                    **mode_params,
                )
                for t in targets
            ):
                data: ScrapingResult = result
                user_id = data.query.query.get('user_id')

                ts = utc.now().strftime("%Y%m%dT%H%M%SZ")
                filename = f"{user_id}_profileauthenticity_{ts}.jsonl"
                data.save(os.path.join(output_dir, filename))

    run_async(_scrape())


# ============== Post-processing ==============

@cli.command()
@click.argument('input_path')
@click.option('--output', default=None,
              help='Output path. May be a file (single named output) or a folder (per-file outputs land inside). '
                   'For directory inputs, must be a folder unless --concat is set. '
                   'Heuristic: existing dir or trailing "/" → folder; .parquet/.csv/.jsonl suffix → file; '
                   'otherwise → folder (created if absent).')
@click.option('--format', 'fmt', type=click.Choice(['csv', 'jsonl', 'parquet', 'all']), default='csv', help='Output format (default csv). "all" writes csv + jsonl + parquet.')
@click.option('--concat', is_flag=True,
              help='Concatenate a directory of inputs into a single output file. Requires --output to be a file path. '
                   'With --format all, the file extension in --output is replaced per format '
                   '(foo.parquet → foo.csv, foo.jsonl, foo.parquet).')
@click.option('--endpoint', default=None, help='Override endpoint flattener (default: read from saved query.endpoint, fallback to UserTimeline)')
def flatten(input_path, output, fmt, concat, endpoint):
    """Flatten a scraped posts JSON (or a directory of them) into a tabular dataset.

    \b
    Accepts .json or .json.gz files (single file or a directory of either).
    Routes through FacebookGraphQLParser.ENDPOINT_FLATTENERS based on the
    saved file's query.endpoint. Output rows are passed through
    pl.json_normalize to flatten nested dicts (shared_post, engagement) into
    `__`-separated columns; list-typed columns (hashtags, attachments,
    top_comments) stay as lists in parquet/jsonl and become JSON-serialized
    strings in csv (round-trippable, lossless).

    \b
    Examples:
      fbscrape flatten data/posts/foo.json
      fbscrape flatten data/posts/foo.json.gz
      fbscrape flatten data/posts/2025-06-01_2026-02-17/ --format parquet
      fbscrape flatten data/posts/foo.json --format all
      fbscrape flatten data/posts/ --output data/flat/ --format parquet
      fbscrape flatten data/posts/ --output data/merged.parquet --concat
    """
    import json
    import polars as pl
    from .response import FacebookGraphQLParser
    from .jsonl_store import load_scrape_file

    if not os.path.exists(input_path):
        raise click.UsageError(f"Path not found: {input_path}")

    input_is_dir = os.path.isdir(input_path)

    if input_is_dir:
        files = sorted(
            os.path.join(input_path, f)
            for f in os.listdir(input_path)
            if f.endswith(('.jsonl.gz', '.jsonl', '.json.gz', '.json'))
        )
        if not files:
            raise click.UsageError(f"No .jsonl/.json (.gz) files in {input_path}")
    else:
        files = [input_path]

    formats = ['csv', 'jsonl', 'parquet'] if fmt == 'all' else [fmt]

    # Longest-first so _strip_out_ext peels '.parquet.zstd' before '.parquet'.
    _RECOGNIZED_OUT_EXTS = ('.parquet.zstd', '.parquet', '.jsonl', '.csv')

    def _ext_for(active_fmt: str) -> str:
        # zstd is the only parquet compression we write — make it explicit
        # in auto-derived filenames so downstream tooling can route by suffix.
        return 'parquet.zstd' if active_fmt == 'parquet' else active_fmt

    def _classify_output_kind(out):
        if out is None:
            return None
        if os.path.isdir(out):
            return 'FOLDER'
        if out.endswith(os.sep) or out.endswith('/'):
            return 'FOLDER'
        if out.lower().endswith(_RECOGNIZED_OUT_EXTS):
            return 'FILE'
        return 'FOLDER'

    out_kind = _classify_output_kind(output)

    if concat and not input_is_dir:
        raise click.UsageError("--concat requires a directory input.")
    if concat and out_kind is None:
        raise click.UsageError("--concat requires --output (a file path).")
    if concat and out_kind == 'FOLDER':
        raise click.UsageError("--concat requires --output to be a file path, not a folder.")
    if input_is_dir and out_kind == 'FILE' and not concat:
        raise click.UsageError(
            "With a directory input, --output must be a folder unless --concat is set."
        )
    if not input_is_dir and out_kind == 'FILE' and len(formats) > 1:
        raise click.UsageError("--output as a file cannot be combined with --format all; pass a folder instead.")

    if concat:
        mode = 'CONCAT'
    elif out_kind == 'FILE':
        mode = 'FILE_OUT'
    elif out_kind == 'FOLDER':
        mode = 'FOLDER_OUT'
    else:
        mode = 'SIBLING'

    if mode == 'FOLDER_OUT':
        os.makedirs(output, exist_ok=True)
    elif mode in ('FILE_OUT', 'CONCAT'):
        parent = os.path.dirname(output)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _strip_json_ext(path: str) -> str:
        for ext in ('.jsonl.gz', '.jsonl', '.json.gz', '.json'):
            if path.endswith(ext):
                return path[:-len(ext)]
        return os.path.splitext(path)[0]

    def _strip_out_ext(path: str) -> str:
        for ext in _RECOGNIZED_OUT_EXTS:
            if path.lower().endswith(ext):
                return path[:-len(ext)]
        return path

    def _serialize_complex_cols(df: pl.DataFrame) -> pl.DataFrame:
        """JSON-stringify any List / Struct columns so write_csv accepts them.
        Round-trippable: callers can `json.loads(cell)` to restore.

        polars' map_elements hands Series objects to the lambda for List
        columns, so we explicitly call .to_list() to get plain Python before
        json.dumps. Struct columns yield dicts directly.
        """
        def _to_json(v):
            if v is None:
                return None
            if hasattr(v, "to_list"):  # polars Series (List dtype)
                v = v.to_list()
            return json.dumps(v, default=str)

        exprs = []
        for col, dtype in zip(df.columns, df.dtypes):
            if isinstance(dtype, (pl.List, pl.Struct)) or dtype == pl.Object:
                exprs.append(
                    pl.col(col).map_elements(_to_json, return_dtype=pl.String).alias(col)
                )
            else:
                exprs.append(pl.col(col))
        return df.select(exprs)

    def _build_df(rows):
        # json_normalize flattens nested dicts (shared_post.* → shared_post__*).
        # Lists stay as List columns; strict=False tolerates schema drift; the
        # full-scan infer (infer_schema_length=None) avoids type-flip errors
        # when a rare-populated column (e.g. music_artist) is null in the first
        # N rows but a string later — polars otherwise locks the schema to Null.
        df = (pl.json_normalize(rows, separator='__', strict=False, infer_schema_length=None)
              if rows else pl.DataFrame())
        # Drop the unflattened parent of any flattened struct (polars quirk —
        # it keeps `shared_post: Null` alongside `shared_post__*` children when
        # some rows have None for the dict).
        if df.width:
            parents = {c.split('__', 1)[0] for c in df.columns if '__' in c}
            redundant = [c for c in df.columns if c in parents and df.schema[c] == pl.Null]
            if redundant:
                df = df.drop(redundant)
        return df

    def _write(df, rows, out_path, active_fmt):
        if active_fmt == 'csv':
            _serialize_complex_cols(df).write_csv(out_path)
        elif active_fmt == 'jsonl':
            # Preserve raw nesting — write the original row dicts, not the
            # normalized DataFrame. `default=str` handles datetimes etc.
            with open(out_path, 'w') as out:
                for r in rows:
                    out.write(json.dumps(r, default=str) + '\n')
        elif active_fmt == 'parquet':
            df.write_parquet(out_path, compression='zstd')

    parser = FacebookGraphQLParser()

    if mode == 'CONCAT':
        all_rows = []
        endpoints_seen = set()
        total_in = 0
        per_file_summary = []
        for f in files:
            query_meta, records = load_scrape_file(f)
            ep = endpoint or (query_meta or {}).get('endpoint') or 'UserTimeline'
            endpoints_seen.add(ep)
            rows = [r for p in records if (r := parser.flatten(p, ep))]
            all_rows.extend(rows)
            total_in += len(records)
            per_file_summary.append((f, len(rows), len(records), ep))

        if not endpoint and len(endpoints_seen) > 1:
            raise click.UsageError(
                f"--concat requires homogeneous endpoints across inputs "
                f"(found: {sorted(endpoints_seen)}). Pass --endpoint to override."
            )

        df = _build_df(all_rows)
        ep_label = sorted(endpoints_seen)[0] if endpoints_seen else (endpoint or 'UserTimeline')
        for f, nrows, nrecs, ep in per_file_summary:
            click.echo(f"  {f}: {nrows}/{nrecs} (endpoint={ep})")
        # Single-format concat honors --output literally; --format all derives
        # sibling filenames from the stem so the user's chosen extension only
        # picks the stem, not the encoding.
        for active_fmt in formats:
            if len(formats) == 1:
                out_path = output
            else:
                stem = _strip_out_ext(output)
                out_path = f"{stem}.{_ext_for(active_fmt)}"
            _write(df, all_rows, out_path, active_fmt)
            click.echo(f"-> {out_path} ({len(all_rows)} records, endpoint={ep_label})")
        click.echo(f"\nTotal: {len(all_rows)}/{total_in} records flattened (concatenated from {len(files)} files)")
        return

    total_in = total_out = 0
    for f in files:
        # Endpoint priority: CLI flag > saved query.endpoint > UserTimeline default.
        # load_scrape_file is dual-format: one-post-per-line JSONL (current) or a
        # legacy whole-file envelope (data/posts key).
        query_meta, records = load_scrape_file(f)
        ep = endpoint or (query_meta or {}).get('endpoint') or 'UserTimeline'
        rows = [r for p in records if (r := parser.flatten(p, ep))]
        total_in += len(records)
        total_out += len(rows)

        df = _build_df(rows)

        for active_fmt in formats:
            ext = _ext_for(active_fmt)
            if mode == 'FILE_OUT':
                out_path = output
            elif mode == 'FOLDER_OUT':
                stem = os.path.basename(_strip_json_ext(f))
                out_path = os.path.join(output, f"{stem}_flat.{ext}")
            else:  # SIBLING
                out_path = f"{_strip_json_ext(f)}_flat.{ext}"

            _write(df, rows, out_path, active_fmt)
            click.echo(f"{f} -> {out_path} ({len(rows)}/{len(records)} records, endpoint={ep})")

    click.echo(f"\nTotal: {total_out}/{total_in} records flattened")


@cli.command(name='unstick-cursor')
@click.argument('paths', nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option('--rank', type=int, default=3, show_default=True,
              help='Anchor at the Nth chronologically-oldest post with a cursor. '
                   'Default 3 skips the rank-1 post (often a bootstrap-edge '
                   'highlight outlier) and gives a small buffer. Higher = '
                   'anchor deeper into the file.')
@click.option('--only-if-stuck', is_flag=True,
              help='Only modify files whose result == "no_new_posts_streak". '
                   'Files with other results are reported and skipped.')
@click.option('--dry-run', is_flag=True,
              help='Show the swap that would happen without writing the file.')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
def unstick_cursor(paths, rank, only_if_stuck, dry_run, log_level):
    """Unstick a saved scrape's last_cursor by swapping it to the per-edge
    cursor of a deeper post in the file.

    \b
    Use when a --continue scrape is deadlocked — i.e., it consistently bails
    on no_new_posts_streak because the saved cursor anchors at a position
    where FB serves only posts already in the file's dedup seed. The fix
    picks a chronologically-deeper anchor (default: 3rd-oldest cursored post)
    so the next --continue resumes past the dedup wall and into uncovered
    territory.

    \b
    Examples:
      fbscrape unstick-cursor data/posts/foo.json.gz
      fbscrape unstick-cursor data/posts/*.json.gz --only-if-stuck
      fbscrape unstick-cursor foo.json.gz --rank 5 --dry-run

    \b
    Schema requirements (transparent — no checks needed before running):
      - File must be a ScrapingResult JSON (gzipped or plain).
      - File must have at least `rank` posts with parseable creation_time
        and a per-edge `cursor` field.
    """
    import hashlib
    from .logger import set_log_level, logger
    set_log_level(log_level)

    fixed: list[str] = []
    skipped: list[tuple[str, str]] = []
    errored: list[tuple[str, str]] = []

    from .jsonl_store import looks_like_jsonl, load_scrape_file, read_meta, JsonlPostWriter
    for path in paths:
        try:
            is_jsonl = looks_like_jsonl(path)
            if is_jsonl:
                meta = read_meta(path) or {}
                query = meta.get('query') or {}
                result = meta.get('result')
                old_lc = meta.get('last_cursor')
                _, data = load_scrape_file(path)
            else:
                with _open_scrape_input(path) as f:
                    d = json.load(f)
                query = d.get('query') or {}
                result = d.get('result')
                old_lc = d.get('last_cursor')
                data = d.get('data') or d.get('posts') or []
        except Exception as e:
            errored.append((path, f"failed to load: {e}"))
            click.echo(f"[ERROR] {path}: {e}")
            continue

        endpoint = query.get('endpoint') or 'GroupTimeline'

        if only_if_stuck and result != 'no_new_posts_streak':
            skipped.append((path, f"result={result!r}"))
            click.echo(f"[skip ] {path}  (result={result!r}; --only-if-stuck specified)")
            continue

        chosen = _find_unstick_cursor(data, endpoint=endpoint, rank=rank)
        if chosen is None:
            msg = f"no cursored post at or beyond rank #{rank} (data has {len(data)} entries)"
            errored.append((path, msg))
            click.echo(f"[ERROR] {path}: {msg}")
            continue

        new_cursor, diag = chosen
        old_fp = hashlib.sha1(old_lc.encode()).hexdigest()[:8] if old_lc else '<null>'
        new_fp = hashlib.sha1(new_cursor.encode()).hexdigest()[:8]
        chosen_iso = datetime.fromtimestamp(
            diag["chosen_created_at"], tz=timezone.utc
        ).isoformat()

        if dry_run:
            click.echo(
                f"[dry  ] {path}\n"
                f"        result={result!r}  posts={len(data)}\n"
                f"        would anchor: rank #{diag['chosen_rank']}, "
                f"data[{diag['chosen_idx']}] @ {chosen_iso}\n"
                f"        last_cursor: {old_fp} → {new_fp}"
            )
            continue

        if is_jsonl:
            # Append a status-only line carrying the new cursor — the next
            # tail-read resumes from it. No whole-file rewrite.
            writer = JsonlPostWriter(path, query, meta.get('time_started'),
                                     append=True, compress=path.endswith('.gz'))
            writer.finalize(result, None, last_cursor=new_cursor)
        else:
            d['last_cursor'] = new_cursor
            if path.endswith('.gz'):
                with gzip.open(path, 'wt') as f:
                    json.dump(d, f, indent=2)
            else:
                with open(path, 'w') as f:
                    json.dump(d, f, indent=2)

        fixed.append(path)
        click.echo(
            f"[FIXED] {path}\n"
            f"        result={result!r}  posts={len(data)}\n"
            f"        anchor: rank #{diag['chosen_rank']}, "
            f"data[{diag['chosen_idx']}] @ {chosen_iso}\n"
            f"        last_cursor: {old_fp} → {new_fp}"
        )

    click.echo("")
    click.echo(f"=== Summary ===")
    click.echo(f"  fixed   : {len(fixed)}")
    click.echo(f"  skipped : {len(skipped)}")
    click.echo(f"  errored : {len(errored)}")


@cli.command(name='download-media')
@click.argument('input_path')
@click.option('--out-dir', default=None, help='Directory to save media (default: <input_dir>/media/<handle>/)')
@click.option('--include-thumbnails', is_flag=True, help='Also download video thumbnails')
@click.option('--concurrency', default=8, type=int, help='Concurrent downloads (default 8)')
@click.option('--no-skip-existing', is_flag=True, help='Re-download files that already exist')
@click.option('--timeout', default=60, type=int, help='Per-request timeout in seconds (default 60)')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
def download_media(input_path, out_dir, include_thumbnails, concurrency, no_skip_existing, timeout, log_level):
    """Download media (images and videos) from a scraped posts JSON or directory.

    \b
    Accepts .json or .json.gz files (single file or a directory of either).
    fbcdn URLs are signed and expire ~4-5 days after scraping, so run this within
    a few days of the scrape or expect HTTP 403 ("Bad URL hash") on the stale
    subset. No account cookies needed — URLs are self-authenticating.

    \b
    Filenames: {post_id}_img_00.jpg, {post_id}_vid_00.mp4, {post_id}_thumb_00.jpg

    \b
    Examples:
      fbscrape download-media data/posts/foo.json
      fbscrape download-media data/posts/foo.json.gz
      fbscrape download-media data/posts/2025-06-01_2026-02-17/ --include-thumbnails
    """
    import json as _json
    from .downloaders import download_media_from_posts
    from .logger import set_log_level

    set_log_level(log_level)

    if not os.path.exists(input_path):
        raise click.UsageError(f"Path not found: {input_path}")

    if os.path.isdir(input_path):
        files = sorted(
            os.path.join(input_path, f)
            for f in os.listdir(input_path)
            if f.endswith(('.jsonl.gz', '.jsonl', '.json.gz', '.json'))
        )
        if not files:
            raise click.UsageError(f"No .jsonl/.json (.gz) files in {input_path}")
    else:
        files = [input_path]

    def _strip_json_ext(name: str) -> str:
        for ext in ('.jsonl.gz', '.jsonl', '.json.gz', '.json'):
            if name.endswith(ext):
                return name[:-len(ext)]
        return os.path.splitext(name)[0]

    async def _run():
        totals = {"total": 0, "saved": 0, "skipped": 0, "failed": 0}
        for f in files:
            # Dual-format: one-post-per-line JSONL (current) or legacy envelope.
            from .jsonl_store import load_scrape_file
            query_meta, posts = load_scrape_file(f)
            handle = (query_meta or {}).get('query', {}).get('handle') or _strip_json_ext(os.path.basename(f))

            if out_dir:
                target_dir = out_dir if len(files) == 1 else os.path.join(out_dir, handle)
            else:
                target_dir = os.path.join(os.path.dirname(f), 'media', handle)

            click.echo(f"{f} -> {target_dir}")
            summary = await download_media_from_posts(
                posts=posts,
                out_dir=target_dir,
                include_thumbnails=include_thumbnails,
                concurrency=concurrency,
                skip_existing=not no_skip_existing,
                timeout_sec=timeout,
            )
            click.echo(f"  total={summary['total']} saved={summary['saved']} skipped={summary['skipped']} failed={summary['failed']}")
            for k in totals:
                totals[k] += summary[k]

        if len(files) > 1:
            click.echo(f"\nGrand total: {totals}")

    run_async(_run())


@cli.group()
def utils():
    """Developer utilities (parse cURL, etc.)."""
    pass


@utils.command(name='parse-curl')
@click.argument('curl_string')
@click.option('--full', is_flag=True, default=False,
              help='Show every header and body field (default: structured summary).')
@click.option('--raw', is_flag=True, default=False,
              help='Do not redact Cookie / fb_dtsg / lsd / jazoest.')
def parse_curl_cmd(curl_string: str, full: bool, raw: bool) -> None:
    """Parse a cURL command (e.g. copied from DevTools) and print a cleaned view.

    By default, prints a structured GraphQL-aware summary (method/URL,
    friendly_name, doc_id, decoded `variables` JSON, key headers, and
    non-telemetry body fields), with Cookie / fb_dtsg / lsd / jazoest
    redacted. Pass --full for every header and body field; --raw to disable
    redaction.
    """
    from fbscrape.utils import parse_curl, format_parsed_curl
    parsed = parse_curl(curl_string)
    click.echo(format_parsed_curl(parsed, full=full, redact=not raw))


@utils.command(name='convert-to-jsonl')
@click.argument('paths', nargs=-1, required=True)
@click.option('--delete-original', is_flag=True, default=False,
              help='Remove the legacy envelope file after a successful convert.')
@click.option('--dry-run', is_flag=True, default=False,
              help='Report what would be converted; write nothing.')
@click.option('-q', '--quiet', is_flag=True, default=False,
              help='Only print the final summary.')
def convert_to_jsonl_cmd(paths, delete_original, dry_run, quiet):
    """Convert legacy whole-file envelope outputs to one-post-per-line JSONL.

    Given file(s) or directories, writes `<stem>.jsonl.gz` next to each legacy
    `<stem>.json{,.gz}` by streaming its `data` array (one ijson pass per file —
    safe on multi-hundred-MB outputs). Files already in JSONL are skipped.
    `--continue` also migrates a legacy prior on first resume, so this is an
    optional up-front bulk pass.

        fbscrape utils convert-to-jsonl data/posts/ --dry-run
        fbscrape utils convert-to-jsonl data/posts/ --delete-original
    """
    from fbscrape.jsonl_store import looks_like_jsonl, convert_envelope_to_jsonl

    def _collect(ps):
        out = []
        for raw in ps:
            if os.path.isdir(raw):
                out += sorted(
                    os.path.join(raw, f) for f in os.listdir(raw)
                    if f.endswith(('.json.gz', '.json'))
                )
            elif os.path.exists(raw):
                out.append(raw)
            else:
                click.echo(f"  (missing: {raw})", err=True)
        return out

    inputs = _collect(list(paths))
    if not inputs:
        raise click.ClickException("No .json/.json.gz files found.")

    click.echo(f"Processing {len(inputs)} file(s){' (dry-run)' if dry_run else ''}...")
    tally = {"converted": 0, "skipped-jsonl": 0, "error": 0}
    for path in inputs:
        base = os.path.basename(path)
        try:
            if looks_like_jsonl(path):
                tally["skipped-jsonl"] += 1
                if not quiet:
                    click.echo(f"  already-jsonl  {base}")
                continue
            stem = path[:-len('.json.gz')] if path.endswith('.json.gz') else path[:-len('.json')]
            dest = stem + '.jsonl.gz'
            if dry_run:
                tally["converted"] += 1
                click.echo(f"  WOULD  {os.path.basename(dest)}")
                continue
            n, endpoint = convert_envelope_to_jsonl(path, dest)
            tally["converted"] += 1
            if delete_original and os.path.abspath(path) != os.path.abspath(dest):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if not quiet:
                click.echo(f"  wrote  {os.path.basename(dest)} (endpoint={endpoint}, posts={n})")
        except Exception as e:  # noqa: BLE001
            tally["error"] += 1
            click.echo(f"  ERROR  {base}: {type(e).__name__}: {e}", err=True)

    click.echo(
        f"=== done: converted={tally['converted']}, "
        f"already-jsonl={tally['skipped-jsonl']}, errors={tally['error']} ==="
    )
    if tally["error"]:
        raise SystemExit(1)


def main():
    cli(obj={})


if __name__ == '__main__':
    main()