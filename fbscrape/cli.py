"""
CLI for managing Facebook scraper accounts
"""

import asyncio
import click
import os
from tabulate import tabulate

from .accounts_pool import AccountsPool
from .utils import gather, get_home_dir_path


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

@cli.command(name='set')
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
      fingerprint, os, error_msg, twofa_id

    \b
    Examples:
      fbscrape set user@example.com username myusername
      fbscrape set user@example.com active true
      fbscrape set user@example.com proxy_server http://proxy:8080
      fbscrape set user@example.com error_msg null
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


@cli.command(name='fields')
def list_fields():
    """List all updatable fields for the 'set' command"""
    fields = sorted(AccountsPool._updatable_fields)
    click.echo("Updatable fields for 'fbscrape set':")
    click.echo("-" * 35)
    for f in fields:
        click.echo(f"  {f}")


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


# ============== Scraping ==============

@cli.group()
def scrape():
    """Run scraping jobs"""
    pass


@scrape.command(name='user-timeline')
@click.argument('handles', nargs=-1, required=True)
@click.option('--start-date', required=True, help='Start date YYYY-MM-DD (how far back to scrape)')
@click.option('--end-date', required=True, help='End date YYYY-MM-DD (most recent date to scrape from)')
@click.option('--output-dir', default=None, help='Directory to save results (default: data/posts/{start}_{end})')
@click.option('--max-sessions', default=2, type=int, help='Max concurrent browser sessions')
@click.option('--scroll-threshold', default=500, type=int, help='Scrolls before rotating account')
@click.option('--stall-timeout-seconds', default=300, type=int, help='Bail out if no GraphQL response for N seconds (default 300)')
@click.option('--headless', is_flag=True, help='Run browsers headless')
@click.option('--mobile', is_flag=True, help='Use mobile emulation')
@click.option('--log-level', default='INFO', help='Log level (DEBUG/INFO/WARNING/ERROR)')
@click.pass_context
def scrape_user_timeline(ctx, handles, start_date, end_date, output_dir, max_sessions, scroll_threshold, stall_timeout_seconds, headless, mobile, log_level):
    """Scrape a user's timeline between two dates.

    \b
    Examples:
      fbscrape scrape user-timeline zuck --start-date 2024-01-01 --end-date 2025-01-01
      fbscrape scrape user-timeline zuck meta --start-date 2024-01-01 --end-date 2025-01-01 --headless
    """
    from .scraper import FacebookScraper
    from .logger import set_log_level
    from .models import ScrapingResult

    set_log_level(log_level)

    if output_dir is None:
        output_dir = os.path.join(get_home_dir_path(), "data", "posts", f"{start_date}_{end_date}")
    os.makedirs(output_dir, exist_ok=True)

    async def _scrape():
        pool = AccountsPool(ctx.obj['db'])
        async with FacebookScraper(
            db=pool,
            max_browser_sessions=max_sessions,
            scroll_threshold=scroll_threshold,
            headless=headless,
            mobile=mobile,
            stall_timeout_seconds=stall_timeout_seconds,
        ) as scraper:
            async for result in gather(
                scraper.user_timeline(handle=h, start_date=start_date, end_date=end_date)
                for h in handles
            ):
                data: ScrapingResult = result
                handle = data.query.query.get('handle')
                click.echo(f"{handle}: {data.result} ({len(data.posts)} posts, {data.time_taken})")

                filename = (
                    f"{handle.replace('.', '_')}"
                    f"_{data.query.endpoint}"
                    f"_{start_date}_{end_date}.json"
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