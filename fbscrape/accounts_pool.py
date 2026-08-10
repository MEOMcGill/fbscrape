import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .account import Account
from .db import execute, fetchall, fetchone
from .logger import logger
from .utils import get_env_bool, parse_cookies, utc


class NoAccountError(Exception):
    pass


class AccountsPool:
    _order_by: str = "scroll_count_overall_24h ASC"

    def __init__(
            self,
            db_file="accounts.db",
            _raise_when_no_account: bool = get_env_bool("FB_RAISE_WHEN_NO_ACCOUNT"),
    ):
        self._db_file = db_file
        self._raise_when_no_account = _raise_when_no_account

    def _identifier_condition(self, identifier: str) -> str:
        """Build SQL condition for matching by email or phone_number"""
        return f"(email = '{identifier}' OR phone_number = '{identifier}')"

    def _identifiers_condition(self, identifiers: list[str]) -> str:
        """Build SQL condition for matching multiple identifiers"""
        quoted = ",".join([f"'{x}'" for x in identifiers])
        return f"(email IN ({quoted}) OR phone_number IN ({quoted}))"

    async def add_account(
            self,
            password: str,
            email: str = None,
            username: str = None,
            email_password: str = None,
            phone_number: str = None,
            cookies: str | dict | list = None,
            proxy_server: str = None,
            proxy_username: str = None,
            proxy_password: str = None,
            twofa_id: str = None,
    ):
        """Add account to the db. Must provide either email or phone_number."""
        if email is None and phone_number is None:
            raise ValueError("Must provide either email or phone_number")

        identifier = email or phone_number

        # Check if account already exists
        qs = f"SELECT * FROM accounts WHERE {self._identifier_condition(identifier)}"
        rs = await fetchone(self._db_file, qs)
        if rs:
            logger.warning(f"Account {identifier} already exists")
            return

        # Parse cookies if provided as string
        if isinstance(cookies, str):
            cookies = parse_cookies(cookies)
        elif cookies is None:
            cookies = []

        account = Account(
            email=email,
            password=password,
            username=username,
            email_password=email_password,
            phone_number=phone_number,
            active=False,
            locks={},
            scroll_count_per_endpoint_total={},
            cookies=cookies,
            proxy_server=proxy_server,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
            twofa_id=twofa_id,
        )

        # If cookies are provided, assume account is active
        if cookies:
            account.active = True

        await self.save(account)
        logger.info(f"Account {identifier} added successfully (active={account.active})")

    async def delete_account(self, identifier: str | list[str]):
        """Delete account(s) from the db by email or phone_number"""
        identifiers = identifier if isinstance(identifier, list) else [identifier]
        identifiers = list(set(identifiers))
        if not identifiers:
            logger.warning("No identifiers provided")
            return

        qs = f"DELETE FROM accounts WHERE {self._identifiers_condition(identifiers)}"
        await execute(self._db_file, qs)
        logger.info(f"Deleted {len(identifiers)} account(s)")

    async def get_inactive_accounts(self):
        """Fetch inactive accounts from the db where active is False"""
        qs = "SELECT * FROM accounts WHERE active = false"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get_active_accounts(self):
        """Fetch active accounts from the db where active is True"""
        logger.debug("get_active_accounts()")
        qs = "SELECT * FROM accounts WHERE active = true"
        rs = await fetchall(self._db_file, qs)
        accounts = [Account.from_rs(x) for x in rs]
        logger.debug(f"get_active_accounts() -> {len(accounts)} accounts")
        return accounts

    async def get(self, identifier: str | list[str] | None) -> Account | list[Account]:
        """Fetch account(s) information from db by identifier (email or phone_number) or all accounts if None"""
        if identifier is None:
            qs = "SELECT * FROM accounts"
            rs = await fetchall(self._db_file, qs)
            return [Account.from_rs(x) for x in rs]
        elif isinstance(identifier, list):
            identifiers = list(set(identifier))
            qs = f"SELECT * FROM accounts WHERE {self._identifiers_condition(identifiers)}"
            rs = await fetchall(self._db_file, qs)
            return [Account.from_rs(x) for x in rs]
        else:
            qs = f"SELECT * FROM accounts WHERE {self._identifier_condition(identifier)}"
            rs = await fetchone(self._db_file, qs)
            if not rs:
                raise ValueError(f"Account {identifier} not found")
            return Account.from_rs(rs)

    async def save(self, account: Account):
        """
        Save account information to db (inserts or updates based on email/phone_number match)
        """
        data = account.to_rs()
        cols = list(data.keys())

        # Build upsert - check for existing account by email or phone
        identifier = account.identifier
        existing = await fetchone(
            self._db_file,
            f"SELECT * FROM accounts WHERE {self._identifier_condition(identifier)}"
        )

        if existing:
            # Update existing account
            set_clause = ",".join([f"{x}=:{x}" for x in cols if x not in ('email', 'phone_number')])
            qs = f"""
            UPDATE accounts SET {set_clause}
            WHERE {self._identifier_condition(identifier)}
            """
        else:
            # Insert new account
            qs = f"""
            INSERT INTO accounts ({",".join(cols)}) VALUES ({",".join([f":{x}" for x in cols])})
            """

        await execute(self._db_file, qs, data)

    async def login(self, identifier: str | list[str] | None):
        """Logs into account(s) by identifier or all inactive accounts if None"""
        # Get accounts to login
        if identifier is None:
            accounts = await self.get_inactive_accounts()
        else:
            accounts = await self.get(identifier)
            if not isinstance(accounts, list):
                accounts = [accounts]

        # TODO: Implement Facebook login logic here
        logger.warning("Facebook login not implemented yet")
        return {"total": len(accounts), "success": 0, "failed": len(accounts)}

    async def login_inactive_accounts(self):
        """Logs into inactive accounts"""
        return await self.login(None)

    async def reset_locks(self, identifier: str | list[str] | None):
        """Reset locks for account(s) by identifier or all accounts if None"""
        if identifier is None:
            qs = "UPDATE accounts SET locks = json_object()"
        else:
            identifiers = identifier if isinstance(identifier, list) else [identifier]
            identifiers = list(set(identifiers))
            qs = f"UPDATE accounts SET locks = json_object() WHERE {self._identifiers_condition(identifiers)}"

        await execute(self._db_file, qs)
        logger.info(f"Reset locks for {identifier if identifier else 'all accounts'}")

    async def set_active(
            self,
            identifier: str | list[str] | None,
            active: bool | list[bool],
            error_message: str | list[str] | None = None):
        """Set active status for account(s) by identifier or all accounts if None"""
        if identifier is None:
            # Set all accounts
            qs = "UPDATE accounts SET active = :active, error_msg = :error_msg"
            await execute(self._db_file, qs, {"active": active, "error_msg": error_message})
        else:
            identifiers = identifier if isinstance(identifier, list) else [identifier]
            identifiers = list(set(identifiers))

            # Handle list of active statuses and error messages
            if isinstance(active, list):
                for i, ident in enumerate(identifiers):
                    act = active[i] if i < len(active) else active[-1]
                    err = None
                    if error_message is not None:
                        if isinstance(error_message, list):
                            err = error_message[i] if i < len(error_message) else error_message[-1]
                        else:
                            err = error_message

                    qs = f"UPDATE accounts SET active = :active, error_msg = :error_msg WHERE {self._identifier_condition(ident)}"
                    await execute(self._db_file, qs, {"active": act, "error_msg": err})
            else:
                qs = f"""UPDATE accounts SET active = :active, error_msg = :error_msg
                         WHERE {self._identifiers_condition(identifiers)}"""
                await execute(self._db_file, qs, {"active": active, "error_msg": error_message})

        logger.info(f"Set active={active} for {identifier if identifier else 'all accounts'}")

    async def lock_until(
        self,
        identifier: str | list[str] | None,
        until: str,
        error_msg: str | None = None,
    ):
        """Lock account(s) until given time (for rate limiting).

        When `error_msg` is provided, also writes it to the account's
        `error_msg` column so the DB record explains *why* the lock is in
        place — useful for after-the-fact diagnosis when the lock has
        already expired (the lock state is cleared, but the error_msg
        persists until something else overwrites it).
        """
        logger.debug(f"lock_until({identifier}, {until}, error_msg={error_msg!r})")
        identifiers = identifier if isinstance(identifier, list) else [identifier] if identifier else []

        # `until` is an SQL expression (interpolated, not parameterised);
        # error_msg is a user-supplied string and goes through proper
        # parameter binding.
        set_clause = (
            f"locks = json_set(locks, '$.locked_until', {until}),\n"
            f"                last_used = datetime({utc.ts()}, 'unixepoch')"
        )
        if error_msg is not None:
            set_clause += ",\n                error_msg = :error_msg"

        if not identifiers:
            qs = f"UPDATE accounts SET {set_clause} WHERE TRUE"
        else:
            qs = f"UPDATE accounts SET {set_clause} WHERE {self._identifiers_condition(identifiers)}"

        params = {"error_msg": error_msg} if error_msg is not None else None
        await execute(self._db_file, qs, params)

    async def unlock(self, identifier: str | list[str] | None):
        """Unlock account(s) - remove rate limit lock"""
        logger.debug(f"unlock({identifier})")
        identifiers = identifier if isinstance(identifier, list) else [identifier] if identifier else []

        if not identifiers:
            qs = f"""
            UPDATE accounts SET
                locks = json_remove(locks, '$.locked_until'),
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE TRUE
            """
        else:
            qs = f"""
            UPDATE accounts SET
                locks = json_remove(locks, '$.locked_until'),
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE {self._identifiers_condition(identifiers)}
            """

        await execute(self._db_file, qs)

    async def _get_and_mark_in_use(self, condition: str):
        """Internal method to get an account and mark it as in_use"""
        # condition is a subquery that selects the identifier
        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true
            WHERE COALESCE(email, phone_number) = ({condition})
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                last_used = datetime({utc.ts()}, 'unixepoch'),
                in_use = true,
                _tx = '{tx}'
            WHERE COALESCE(email, phone_number) = ({condition})
            """
            await execute(self._db_file, qs)

            qs = f"SELECT * FROM accounts WHERE _tx = '{tx}'"
            rs = await fetchone(self._db_file, qs)

        return Account.from_rs(rs) if rs else None

    async def get_available(self, order_by: str | None = None) -> Account | None:
        """Get an available account (active, not in use, not locked).

        `order_by` overrides the default `scroll_count_overall_24h ASC`
        priority — e.g. callers whose endpoint doesn't accumulate scroll
        count (so that default never actually spreads load for them) can
        pass `last_used ASC` instead to prioritize least-recently-used.
        """
        logger.debug("get_available() searching for active, not in-use, not locked account")
        q = f"""
        SELECT COALESCE(email, phone_number) FROM accounts
        WHERE active = true
            AND in_use = false
            AND (
                locks IS NULL
                OR json_extract(locks, '$.locked_until') IS NULL
                OR json_extract(locks, '$.locked_until') < datetime('now')
            )
        ORDER BY {order_by or self._order_by}
        LIMIT 1
        """
        account = await self._get_and_mark_in_use(q)
        logger.debug(f"get_available() -> {account.display_name if account else 'None'}")
        return account

    async def get_for_queue(self, queue: str = "general") -> Account | None:
        """Alias for get_available() - queue parameter is ignored (backward compatibility)"""
        return await self.get_available()

    async def get_available_or_wait(self, order_by: str | None = None) -> Account | None:
        """Get an available account, or wait until one is available.

        `order_by` — see `get_available`.
        """
        msg_shown = False
        while True:
            # 1. probe to see if there's even an account that could pick from -
            # query: active=True & in_use=False.
            #       0 -> "excess worker, quit"
            #       1 -> "log unlock ETA" but continue looping every 5s
            account = await self.get_available(order_by=order_by)
            if not account:
                if self._raise_when_no_account or get_env_bool("FB_RAISE_WHEN_NO_ACCOUNT"):
                    raise NoAccountError("No account available")

                nat = await self.next_available_at()
                if nat:
                    if not msg_shown:
                        msg = f"No account available. Next available at {nat}"
                        logger.info(msg)
                        msg_shown = True
                else:
                    logger.info("Not enough active accounts, exiting worker.")
                    return None
                await asyncio.sleep(5)
                continue
            else:
                logger.info(f"Continuing with account {account.identifier}")
                return account

    async def get_for_queue_or_wait(self, queue: str = "general") -> Account | None:
        """Alias for get_available_or_wait() - queue parameter is ignored (backward compatibility)"""
        return await self.get_available_or_wait()

    async def next_available_at(self):
        """Get the next available time for a locked account"""
        qs = """
        SELECT json_extract(locks, '$.locked_until') as locked_until
        FROM accounts
        WHERE active = true AND in_use = false
            AND json_extract(locks, '$.locked_until') IS NOT NULL
            AND json_extract(locks, '$.locked_until') > datetime('now')
        ORDER BY locked_until ASC
        LIMIT 1
        """
        rs = await fetchone(self._db_file, qs)
        if rs and rs["locked_until"]:
            now, trg = utc.now(), utc.from_iso(rs["locked_until"])
            if trg < now:
                return "now"

            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")
        return None

    async def release_account(self, identifier: str | list[str] | None):
        """Release account(s) after use - sets in_use=false"""
        logger.debug(f"release_account({identifier})")
        identifiers = identifier if isinstance(identifier, list) else [identifier] if identifier else []

        if not identifiers:
            # Release all accounts
            qs = f"""
            UPDATE accounts SET
                in_use = false,
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE TRUE
            """
        else:
            # Release specific account(s)
            qs = f"""
            UPDATE accounts SET
                in_use = false,
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE {self._identifiers_condition(identifiers)}
            """

        await execute(self._db_file, qs)

    async def mark_inactive(self, identifier: str, error_msg: str | None):
        """Mark an account as inactive with an error message"""
        logger.debug(f"mark_inactive({identifier}, {error_msg})")
        qs = f"""
        UPDATE accounts SET active = false, error_msg = :error_msg, in_use = false
        WHERE {self._identifier_condition(identifier)}
        """
        await execute(self._db_file, qs, {"error_msg": error_msg})
        logger.warning(f"Marked account {identifier} as inactive: {error_msg}")

    async def update_cookies(
            self,
            identifier: str,
            cookies: str | dict | list,
    ):
        """
        Update cookies for an account.

        Args:
            identifier: Account identifier (email or phone_number)
            cookies: Cookies in any format accepted by parse_cookies()
        """
        logger.debug(f"update_cookies({identifier}, {len(cookies) if isinstance(cookies, list) else 'parsing'} cookies)")
        # Use parse_cookies to normalize to Playwright format
        if isinstance(cookies, str):
            cookies = parse_cookies(cookies)
        elif isinstance(cookies, dict):
            if "cookies" in cookies:
                cookies = cookies["cookies"]
            else:
                cookies = parse_cookies(json.dumps(cookies))
        # If already a list, assume it's in Playwright format

        cookies_json = json.dumps(cookies)

        qs = f"UPDATE accounts SET cookies = :cookies WHERE {self._identifier_condition(identifier)}"
        await execute(self._db_file, qs, {"cookies": cookies_json})
        logger.info(f"Updated cookies for {identifier} ({len(cookies)} cookies)")

    async def update_fingerprint(self, identifier: str, fingerprints: dict[str, str]):
        """Persist the full per-OS fingerprint dict for an account."""
        payload = json.dumps(fingerprints)
        logger.debug(f"update_fingerprint({identifier}, oses={list(fingerprints)}, {len(payload)} bytes)")
        qs = f"UPDATE accounts SET fingerprints = :fp WHERE {self._identifier_condition(identifier)}"
        await execute(self._db_file, qs, {"fp": payload})
        logger.info(f"Updated fingerprints for {identifier} (oses={list(fingerprints)})")

    async def update_last_used(self, identifier: str):
        """Update last_used timestamp for an account"""
        qs = f"UPDATE accounts SET last_used = datetime({utc.ts()}, 'unixepoch') WHERE {self._identifier_condition(identifier)}"
        await execute(self._db_file, qs)

    async def update_scroll_count(
            self,
            identifier: str,
            endpoint: str,
            increment: int = 1,
    ):
        """
        Update scroll counts for an account (per endpoint and overall 24h).

        Args:
            identifier: Account identifier (email or phone_number)
            endpoint: Endpoint name (e.g., 'user_page', 'search')
            increment: Number of scrolls to add (default 1)
        """
        logger.debug(f"update_scroll_count({identifier}, {endpoint}, +{increment})")
        qs = f"""
        UPDATE accounts SET
            scroll_count_per_endpoint_total = json_set(
                scroll_count_per_endpoint_total,
                '$.{endpoint}',
                COALESCE(json_extract(scroll_count_per_endpoint_total, '$.{endpoint}'), 0) + :increment
            ),
            scroll_count_overall_24h = scroll_count_overall_24h + :increment,
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE {self._identifier_condition(identifier)}
        """
        await execute(self._db_file, qs, {"increment": increment})

    async def get_scroll_count(self, identifier: str, endpoint: str | None = None) -> int:
        """
        Get scroll count for an account.

        Args:
            identifier: Account identifier (email or phone_number)
            endpoint: If provided, get count for specific endpoint; otherwise get overall 24h count

        Returns:
            Scroll count
        """
        logger.debug(f"get_scroll_count({identifier}, endpoint={endpoint})")
        if endpoint:
            qs = f"SELECT json_extract(scroll_count_per_endpoint_total, '$.{endpoint}') as count FROM accounts WHERE {self._identifier_condition(identifier)}"
        else:
            qs = f"SELECT scroll_count_overall_24h as count FROM accounts WHERE {self._identifier_condition(identifier)}"

        rs = await fetchone(self._db_file, qs)
        count = rs["count"] or 0 if rs else 0
        logger.debug(f"get_scroll_count() -> {count}")
        return count

    async def reset_scroll_counts(self, identifier: str | None = None, endpoint: str | None = None):
        """
        Reset scroll counts for account(s).

        Args:
            identifier: Account identifier (or None for all accounts)
            endpoint: Reset only this endpoint (or None for all counts including 24h)
        """
        if endpoint:
            # Reset specific endpoint
            if identifier:
                qs = f"UPDATE accounts SET scroll_count_per_endpoint_total = json_remove(scroll_count_per_endpoint_total, '$.{endpoint}') WHERE {self._identifier_condition(identifier)}"
                await execute(self._db_file, qs)
            else:
                qs = f"UPDATE accounts SET scroll_count_per_endpoint_total = json_remove(scroll_count_per_endpoint_total, '$.{endpoint}')"
                await execute(self._db_file, qs)
        else:
            # Reset all counts
            if identifier:
                qs = f"UPDATE accounts SET scroll_count_per_endpoint_total = '{{}}', scroll_count_overall_24h = 0 WHERE {self._identifier_condition(identifier)}"
                await execute(self._db_file, qs)
            else:
                qs = "UPDATE accounts SET scroll_count_per_endpoint_total = '{}', scroll_count_overall_24h = 0"
                await execute(self._db_file, qs)

        logger.info(f"Reset scroll counts for {identifier if identifier else 'all accounts'}" + (f" endpoint={endpoint}" if endpoint else ""))

    # Allowed fields for update_field (prevents SQL injection by whitelisting)
    _updatable_fields = {
        'password', 'email', 'username', 'email_password', 'phone_number',
        'active', 'proxy_server', 'proxy_username', 'proxy_password',
        'error_msg', 'twofa_id',
    }

    async def update_field(
            self,
            identifier: str,
            field: str,
            value: str | bool | int | None,
    ):
        """
        Update a single field for an account.

        Args:
            identifier: Account identifier (email or phone_number)
            field: Field name to update (must be in _updatable_fields)
            value: New value for the field

        Raises:
            ValueError: If field is not in _updatable_fields or account not found
        """
        if field not in self._updatable_fields:
            raise ValueError(
                f"Field '{field}' is not updatable. "
                f"Allowed fields: {', '.join(sorted(self._updatable_fields))}"
            )

        # Check account exists
        existing = await fetchone(
            self._db_file,
            f"SELECT * FROM accounts WHERE {self._identifier_condition(identifier)}"
        )
        if not existing:
            raise ValueError(f"Account {identifier} not found")

        # Handle type conversion for boolean fields
        if field == 'active':
            if isinstance(value, str):
                value = value.lower() in ('true', '1', 'yes', 'y')

        qs = f"UPDATE accounts SET {field} = :value WHERE {self._identifier_condition(identifier)}"
        await execute(self._db_file, qs, {"value": value})
        logger.info(f"Updated {field}={value} for {identifier}")

    async def stats(self):
        """Get statistics about accounts"""
        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            ("in_use", "SELECT COUNT(*) FROM accounts WHERE in_use = true"),
            ("locked", """
                SELECT COUNT(*) FROM accounts
                WHERE json_extract(locks, '$.locked_until') IS NOT NULL
                    AND json_extract(locks, '$.locked_until') > datetime('now')
            """),
        ]

        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}