"""
CLI for managing Facebook scraper accounts
"""

import asyncio
import click
import os
from tabulate import tabulate

from .accounts_pool import AccountsPool
from .utils import get_home_dir_path


def get_default_db():
    return os.path.join(get_home_dir_path(), "db", "accounts.db")


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


# ============== Account Management ==============

@cli.command()
@click.option('--email', default=None, help='Account email')
@click.option('--phone', default=None, help='Account phone number')
@click.option('--password', required=True, help='Account password')
@click.option('--username', default=None, help='Facebook username')
@click.option('--email-password', default=None, help='Email account password')
@click.option('--proxy', default=None, help='Proxy server URL')
@click.option('--proxy-user', default=None, help='Proxy username')
@click.option('--proxy-pass', default=None, help='Proxy password')
@click.option('--cookies', default=None, help='Cookies (JSON string or file path)')
@click.option('--os', 'os_type', default='macos', help='OS type for fingerprint (macos/windows/linux)')
@click.pass_context
def add(ctx, email, phone, password, username, email_password, proxy, proxy_user, proxy_pass, cookies, os_type):
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
            os=os_type,
        )
        identifier = email or phone
        click.echo(f"Added account: {identifier}")

    run_async(_add())


@cli.command()
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


@cli.command()
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


@cli.command(name='list')
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
            headers = ['Identifier', 'Username', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)', 'Error', 'Proxy']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
                    a.username or '-',
                    'Y' if a.active else 'N',
                    'Y' if a.in_use else 'N',
                    str(a.last_used)[:19] if a.last_used else '-',
                    a.scroll_count_overall_24h,
                    (a.error_msg[:30] + '...') if a.error_msg and len(a.error_msg) > 30 else (a.error_msg or '-'),
                    a.proxy_server or '-',
                ])
        else:
            headers = ['Identifier', 'Active', 'In Use', 'Last Used', 'Scrolls (24h)']
            rows = []
            for a in accounts:
                rows.append([
                    a.identifier,
                    'Y' if a.active else 'N',
                    'Y' if a.in_use else 'N',
                    str(a.last_used)[:19] if a.last_used else '-',
                    a.scroll_count_overall_24h,
                ])

        click.echo(tabulate(rows, headers=headers, tablefmt='simple'))
        click.echo(f"\nTotal: {len(accounts)} accounts")

    run_async(_list())


@cli.command()
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
        click.echo(f"  OS:            {account.os}")
        click.echo(f"  Proxy:         {account.proxy_server or '-'}")
        click.echo(f"  Cookies:       {len(account.cookies)} stored")
        click.echo(f"  Scrolls (24h): {account.scroll_count_overall_24h}")
        click.echo(f"  Scrolls/endpoint: {account.scroll_count_per_endpoint_total or '-'}")
        click.echo(f"  Locks:         {account.locks or '-'}")
        click.echo(f"  Error:         {account.error_msg or '-'}")

    run_async(_info())


@cli.command()
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

@cli.command()
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


@cli.command()
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


@cli.command()
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


@cli.command()
@click.argument('identifier', nargs=-1)
@click.option('--all', 'release_all', is_flag=True, help='Release all accounts')
@click.option('--queue', default='general', help='Queue to release from')
@click.pass_context
def release(ctx, identifier, release_all, queue):
    """Release account(s) from use"""
    if not identifier and not release_all:
        raise click.UsageError("Provide identifier(s) or use --all")

    async def _release():
        pool = AccountsPool(ctx.obj['db'])
        target = None if release_all else list(identifier)
        await pool.release_account(target, queue)
        click.echo(f"Released {'all accounts' if release_all else len(identifier)} from queue '{queue}'")

    run_async(_release())


# ============== Scroll Management ==============

@cli.command()
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

@cli.command()
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


@cli.command()
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


def main():
    cli(obj={})


if __name__ == '__main__':
    main()