"""
CLI for managing Facebook scraper accounts
"""

import asyncio
import click
import os
from tabulate import tabulate

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
            headers = ['Identifier', 'Username', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)', 'Locks', 'Error', 'Proxy']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
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
            headers = ['Identifier', 'Username', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)', 'Locks']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
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
@click.argument('identifier')
@click.option(
    '--mode',
    type=click.Choice(['manual', 'automatic', 'cookies']),
    default='automatic',
    help='manual: open browser + breakpoint() for human takeover. '
         'automatic: form-fill flow with stored credentials. '
         'cookies: inject stored cookies and verify (no form-login fallback). '
         '(Default: automatic)',
)
@click.option(
    '--headless/--no-headless',
    default=False,
    help='Run browser headless (auto-resolves to "virtual" on Linux). Default: --no-headless.',
)
@click.pass_context
def login(ctx, identifier, mode, headless):
    """Log in to a Facebook account and persist cookies to the DB.

    \b
    --mode manual: opens facebook.com in a non-headless browser (use noVNC at
        localhost:6080 in the container) and pauses at a (Pdb) prompt.
        Log in by hand, type 'c' + Enter to save cookies; 'q' + Enter
        (or Ctrl-D) to abort without saving.

    \b
    --mode automatic: runs the form-fill login the worker uses on scrape start.
        The account must already have password / email_password stored.

    \b
    --mode cookies: injects the account's stored cookies and verifies with the
        GraphQL viewer probe. Refreshes cookies in the DB on success. Does NOT
        fall back to form login on failure — exits with an error so you can
        decide whether to re-run with --mode automatic or --mode manual.
    """
    from .browser_session import BrowserSession
    from .login import login_automatic, login_manual, login_with_cookies

    async def _login():
        pool = AccountsPool(ctx.obj['db'])
        try:
            account = await pool.get(identifier)
        except ValueError:
            raise click.ClickException(f"Account not found: {identifier}")

        # auto_login=False so initialize() opens the browser without trying
        # cookies/form login on its own — we drive login explicitly below.
        async with BrowserSession(
            account, pool, headless=headless, auto_login=False
        ) as session:
            if mode == 'manual':
                ok = await login_manual(session)
                if not ok:
                    click.echo("Aborted; no cookies saved.")
                    return
                # Manual flow doesn't run _on_login_success, so save explicitly.
                await session.save_cookies()
                click.echo(f"Saved cookies for {identifier}.")

            elif mode == 'automatic':
                # login_automatic returns False on "no login form visible",
                # raises typed exceptions on checkpoint / disabled / transient.
                # On success it runs _on_login_success which already saves cookies.
                ok = await login_automatic(session)
                if not ok:
                    raise click.ClickException(
                        f"Automatic login failed for {identifier}: no login form visible"
                    )
                click.echo(f"Login OK for {identifier} (cookies persisted).")

            else:  # cookies
                # Injects stored cookies, verifies via viewer probe. On success
                # _on_login_success runs (refreshes cookies in DB, marks active).
                ok = await login_with_cookies(session)
                if not ok:
                    raise click.ClickException(
                        f"Cookie validation failed for {identifier}. "
                        f"Re-run with --mode automatic or --mode manual to recover."
                    )
                click.echo(f"Cookies valid for {identifier} (refreshed in DB).")

    run_async(_login())


# ============== Scraping ==============

# Recognized per-row keys in --input-file; everything else is dropped.
_RECOGNIZED_TARGET_KEYS = ('handle', 'start_date', 'end_date')
_INPUT_FILE_EXTENSIONS = ('.csv', '.parquet', '.json', '.jsonl', '.ndjson', '.yaml', '.yml')


def _load_scrape_targets(path: str) -> list[dict]:
    """Read scrape targets from a structured file.

    Dispatches on file extension. Each row/entry must have a non-empty
    `handle`; `start_date` and `end_date` are optional. Other columns/keys
    are silently dropped. NaN / None / empty-string cells are treated as
    "not supplied" (so a CSV with a sparsely-populated start_date column
    is fine).
    """
    import json as _json

    ext = os.path.splitext(path)[1].lower()
    if ext not in _INPUT_FILE_EXTENSIONS:
        raise click.UsageError(
            f"Unsupported --input-file extension {ext!r}. "
            f"Supported: {', '.join(_INPUT_FILE_EXTENSIONS)}"
        )

    if ext == '.csv':
        import csv
        with open(path, newline='') as f:
            raw_rows = list(csv.DictReader(f))
    elif ext == '.parquet':
        import pandas as pd
        raw_rows = pd.read_parquet(path).to_dict(orient='records')
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

    targets: list[dict] = []
    for i, row in enumerate(raw_rows, 1):
        if not isinstance(row, dict):
            raise click.UsageError(
                f"{path} row {i}: expected object/dict, got {type(row).__name__}"
            )
        target: dict = {}
        for key in _RECOGNIZED_TARGET_KEYS:
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
        if 'handle' not in target:
            raise click.UsageError(f"{path} row {i}: missing or empty `handle`")
        targets.append(target)

    if not targets:
        raise click.UsageError(f"{path}: no rows found")

    return targets


def _resolve_targets(handles, input_file, start_date, end_date) -> list[dict]:
    """Resolve handles + dates from CLI flags and/or --input-file into a flat
    list of {handle, start_date, end_date} dicts.

    Enforces:
    - exactly one of (positional handles, --input-file) is supplied
    - if the file supplies a start_date for any row, --start-date must NOT be set
    - same exclusivity for end_date
    - every resolved row ends up with a start_date (CLI flag fills rows that
      don't carry one); end_date defaults to today UTC if neither file nor flag
      supplies it
    """
    if handles and input_file:
        raise click.UsageError(
            "Cannot use both positional handles and --input-file. Pick one."
        )
    if not handles and not input_file:
        raise click.UsageError(
            "Must provide either positional handles or --input-file."
        )

    if input_file:
        targets = _load_scrape_targets(input_file)
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
        targets = [{'handle': h} for h in handles]

    today = utc.now().strftime("%Y-%m-%d")
    resolved = []
    for t in targets:
        sd = t.get('start_date') or start_date
        if sd is None:
            raise click.UsageError(
                f"start_date missing for handle {t['handle']!r} "
                f"(no row value, no --start-date flag)"
            )
        ed = t.get('end_date') or end_date or today
        resolved.append({'handle': t['handle'], 'start_date': sd, 'end_date': ed})
    return resolved


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
              help='Start date YYYY-MM-DD (how far back to scrape). Required '
                   'unless supplied per-row via --input-file.')
@click.option('--end-date', default=None,
              help='End date YYYY-MM-DD (most recent date to scrape from). '
                   'Default: today (UTC). Mutually exclusive with an end_date '
                   'column in --input-file.')
@click.option('--output-dir', default=None, help='Directory to save results (default: data/posts/{start}_{end})')
@click.option('--max-sessions', default=2, type=int, help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=500, type=int, help='Scrolls (or hybrid paginations) before rotating account')
@click.option('--stall-timeout-seconds', default=None, type=int, help='[manual] bail out if no GraphQL response for N seconds (default 300, ignored for --mode hybrid)')
@click.option('--headless', is_flag=True, help='Run browsers headless')
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
@click.pass_context
def scrape_user_timeline(
    ctx, handles, input_file, start_date, end_date, output_dir, max_sessions,
    scroll_threshold, stall_timeout_seconds, headless, mobile, log_level,
    mode, pagination_count, scroll_burst_every, scroll_burst_min, scroll_burst_max,
    max_paginations, pagination_sleep_mean, pagination_sleep_std,
    template_capture_timeout, post_nav_sleep_seconds, request_timeout_ms,
    max_no_progress_streak, operation_timeout_seconds, wait_for_account,
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
    from .logger import set_log_level
    from .models import ScrapingResult

    set_log_level(log_level)

    targets = _resolve_targets(handles, input_file, start_date, end_date)

    if output_dir is None:
        starts = [t['start_date'] for t in targets]
        ends = [t['end_date'] for t in targets]
        output_dir = os.path.join(
            get_home_dir_path(), "data", "posts",
            f"{min(starts)}_{max(ends)}",
        )
    os.makedirs(output_dir, exist_ok=True)

    # Bundle mode-specific kwargs. Only forward keys explicitly set on the CLI —
    # None falls through to the registry default in Query.__post_init__.
    # `stall_timeout_seconds` is manual-only; routed only when --mode manual to
    # avoid Query rejecting it as an unknown param under hybrid.
    mode_params = {
        "pagination_count": pagination_count,
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
                scraper.user_timeline(
                    handle=t['handle'],
                    start_date=t['start_date'],
                    end_date=t['end_date'],
                    mode=mode,
                    **mode_params,
                )
                for t in targets
            ):
                data: ScrapingResult = result
                handle = data.query.query.get('handle')
                click.echo(f"{handle}: {data.result} ({len(data.posts)} posts, {data.time_taken})")

                filename = (
                    f"{handle.replace('.', '_')}"
                    f"_{data.query.endpoint}_{data.query.mode}"
                    f"_{data.query.query['start_date']}_{data.query.query['end_date']}.json"
                )
                data.save(os.path.join(output_dir, filename))

        click.echo(f"\nResults saved to: {output_dir}")

    run_async(_scrape())


# ============== Post-processing ==============

@cli.command()
@click.argument('input_path')
@click.option('--output', default=None, help='Output file (default: alongside input with _flat suffix)')
@click.option('--format', 'fmt', type=click.Choice(['csv', 'jsonl', 'parquet', 'all']), default='csv', help='Output format (default csv). "all" writes csv + jsonl + parquet.')
def flatten(input_path, output, fmt):
    """Flatten a scraped posts JSON (or a directory of them) into a tabular dataset.

    \b
    Uses FacebookGraphQLParser.flatten_post to extract post_id, url, created_at,
    author_{id,name,url}, text, reactions, top_reactions, shares, attachments.

    \b
    Examples:
      fbscrape flatten data/posts/foo.json
      fbscrape flatten data/posts/2025-06-01_2026-02-17/ --format jsonl
      fbscrape flatten data/posts/foo.json --format all
    """
    import json
    from .response import FacebookGraphQLParser

    if not os.path.exists(input_path):
        raise click.UsageError(f"Path not found: {input_path}")

    if os.path.isdir(input_path):
        files = sorted(
            os.path.join(input_path, f)
            for f in os.listdir(input_path)
            if f.endswith('.json')
        )
        if not files:
            raise click.UsageError(f"No .json files in {input_path}")
        if output:
            raise click.UsageError("--output cannot be used with a directory; outputs are written alongside each input")
    else:
        files = [input_path]

    parser = FacebookGraphQLParser()
    total_in = total_out = 0

    fieldnames = ['post_id', 'story_id', 'url', 'permalink_url',
                  'created_at', 'created_at_utc', 'privacy',
                  'is_reel', 'is_live', 'video_duration_sec', 'video_views',
                  'author_id', 'author_name', 'author_url', 'text', 'external_urls',
                  'reactions', 'like', 'love', 'haha', 'wow', 'sad', 'angry', 'care',
                  'shares', 'comments', 'top_comments',
                  'shared_post_id', 'shared_post_url', 'shared_post_created_at',
                  'shared_post_author_id', 'shared_post_author_name', 'shared_post_text',
                  'attachments']

    formats = ['csv', 'jsonl', 'parquet'] if fmt == 'all' else [fmt]
    if output and len(formats) > 1:
        raise click.UsageError('--output cannot be combined with --format all')

    for f in files:
        with open(f) as fh:
            data = json.load(fh)

        posts = data.get('posts', [])
        rows = [r for p in posts if (r := parser.flatten_post(p))]
        total_in += len(posts)
        total_out += len(rows)

        for active_fmt in formats:
            if output and len(files) == 1:
                out_path = output
            else:
                base = os.path.splitext(f)[0]
                out_path = f"{base}_flat.{active_fmt}"

            if active_fmt == 'csv':
                import csv
                with open(out_path, 'w', newline='') as out:
                    w = csv.DictWriter(out, fieldnames=fieldnames)
                    w.writeheader()
                    for r in rows:
                        row = {k: (json.dumps(v, default=str) if isinstance(v, (list, dict)) else v) for k, v in r.items()}
                        w.writerow(row)
            elif active_fmt == 'jsonl':
                with open(out_path, 'w') as out:
                    for r in rows:
                        out.write(json.dumps(r, default=str) + '\n')
            elif active_fmt == 'parquet':
                import pandas as pd
                df = pd.DataFrame(rows, columns=fieldnames)
                # Nested list/dict cols are kept as Python objects — Parquet handles them natively.
                df.to_parquet(out_path, index=False)

            click.echo(f"{f} -> {out_path} ({len(rows)}/{len(posts)} posts)")

    click.echo(f"\nTotal: {total_out}/{total_in} posts flattened")


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
    fbcdn URLs are signed and expire ~30 days after scraping, so run this soon
    after a scrape. No account cookies needed — URLs are self-authenticating.

    \b
    Filenames: {post_id}_img_00.jpg, {post_id}_vid_00.mp4, {post_id}_thumb_00.jpg

    \b
    Examples:
      fbscrape download-media data/posts/foo.json
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
            if f.endswith('.json')
        )
        if not files:
            raise click.UsageError(f"No .json files in {input_path}")
    else:
        files = [input_path]

    async def _run():
        totals = {"total": 0, "saved": 0, "skipped": 0, "failed": 0}
        for f in files:
            with open(f) as fh:
                data = _json.load(fh)
            posts = data.get('posts', [])
            handle = (data.get('query') or {}).get('query', {}).get('handle') or os.path.splitext(os.path.basename(f))[0]

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


def main():
    cli(obj={})


if __name__ == '__main__':
    main()