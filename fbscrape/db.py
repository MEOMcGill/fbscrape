import asyncio
import os.path
import random
import sqlite3
from collections import defaultdict

import aiosqlite

from .logger import logger
from .utils import get_home_dir_path

_lock = asyncio.Lock()


def lock_retry(max_retries=10):
    # this lock decorator has double nature:
    # 1. it uses asyncio lock in same process
    # 2. it retries when db locked by other process (eg. two cli instances running)
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    async with _lock:
                        return await func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if i == max_retries - 1 or "database is locked" not in str(e):
                        raise e

                    await asyncio.sleep(random.uniform(0.5, 1.0))

        return wrapper

    return decorator


async def migrate(db: aiosqlite.Connection):
    """
    Migration system for database schema changes.

    To add a new migration:
    1. Add a new async function: async def migrate_vN(db) where N is the version number
    2. Add it to the MIGRATIONS list below
    3. The migration will run automatically if user_version < N
    """
    async with db.execute("PRAGMA user_version") as cur:
        rs = await cur.fetchone()
        current_version = rs[0] if rs else 0

    # List of migrations in order. Each tuple is (version, migration_function)
    MIGRATIONS = [
        (1, migrate_v1),
        (2, migrate_v2),
        (3, migrate_v3),
    ]

    for version, migration_fn in MIGRATIONS:
        if current_version < version:
            logger.info(f"Running migration to v{version}")
            await migration_fn(db)
            await db.execute(f"PRAGMA user_version = {version}")
            await db.commit()


async def migrate_v1(db: aiosqlite.Connection):
    """Initial schema - accounts table with email OR phone_number as identifier"""
    qs = """
    CREATE TABLE IF NOT EXISTS accounts (
        email TEXT DEFAULT NULL COLLATE NOCASE,
        password TEXT NOT NULL,
        username TEXT DEFAULT NULL COLLATE NOCASE,
        email_password TEXT DEFAULT NULL,
        phone_number TEXT DEFAULT NULL COLLATE NOCASE,
        active BOOLEAN DEFAULT FALSE NOT NULL,
        locks TEXT DEFAULT '{}' NOT NULL,
        scroll_count_per_endpoint_total TEXT DEFAULT '{}' NOT NULL,
        cookies TEXT DEFAULT '[]' NOT NULL,
        twofa_id TEXT DEFAULT NULL,
        proxy_server TEXT DEFAULT NULL,
        proxy_username TEXT DEFAULT NULL,
        proxy_password TEXT DEFAULT NULL,
        fingerprints TEXT DEFAULT '{}' NOT NULL,
        error_msg TEXT DEFAULT NULL,
        last_used TEXT DEFAULT NULL,
        in_use BOOLEAN DEFAULT FALSE NOT NULL,
        scroll_count_overall_24h INTEGER DEFAULT 0 NOT NULL,
        _tx TEXT DEFAULT NULL,
        CHECK (email IS NOT NULL OR phone_number IS NOT NULL)
    );"""
    await db.execute(qs)

    # Create indexes for lookups by email or phone
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email) WHERE email IS NOT NULL")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone_number) WHERE phone_number IS NOT NULL")


async def migrate_v2(db: aiosqlite.Connection):
    """
    Migration for existing databases: make email nullable, allow phone_number as identifier.
    SQLite doesn't support ALTER COLUMN, so we recreate the table.
    """
    # Check if table exists and has old schema (email NOT NULL)
    async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'") as cur:
        row = await cur.fetchone()
        if not row:
            # No accounts table - nothing to migrate
            return

        table_sql = row[0]
        if "email TEXT DEFAULT NULL" in table_sql:
            # Already has new schema, skip
            logger.info("Database already has new schema, skipping v2 migration")
            return

    logger.info("Migrating accounts table to allow email OR phone_number as identifier")

    # Create new table with correct schema
    await db.execute("""
    CREATE TABLE IF NOT EXISTS accounts_new (
        email TEXT DEFAULT NULL COLLATE NOCASE,
        password TEXT NOT NULL,
        username TEXT DEFAULT NULL COLLATE NOCASE,
        email_password TEXT DEFAULT NULL,
        phone_number TEXT DEFAULT NULL COLLATE NOCASE,
        active BOOLEAN DEFAULT FALSE NOT NULL,
        locks TEXT DEFAULT '{}' NOT NULL,
        scroll_count_per_endpoint_total TEXT DEFAULT '{}' NOT NULL,
        cookies TEXT DEFAULT '[]' NOT NULL,
        twofa_id TEXT DEFAULT NULL,
        proxy_server TEXT DEFAULT NULL,
        proxy_username TEXT DEFAULT NULL,
        proxy_password TEXT DEFAULT NULL,
        fingerprint TEXT DEFAULT NULL,
        os TEXT DEFAULT 'macos',
        error_msg TEXT DEFAULT NULL,
        last_used TEXT DEFAULT NULL,
        in_use BOOLEAN DEFAULT FALSE NOT NULL,
        scroll_count_overall_24h INTEGER DEFAULT 0 NOT NULL,
        _tx TEXT DEFAULT NULL,
        CHECK (email IS NOT NULL OR phone_number IS NOT NULL)
    )
    """)

    # Copy existing data
    await db.execute("""
    INSERT INTO accounts_new (
        email, password, username, email_password, phone_number,
        active, locks, scroll_count_per_endpoint_total, cookies,
        twofa_id, proxy_server, proxy_username, proxy_password,
        fingerprint, os, error_msg, last_used, in_use, scroll_count_overall_24h
    )
    SELECT
        email, password, username, email_password, phone_number,
        active, locks, scroll_count_per_endpoint_total, cookies,
        twofa_id, proxy_server, proxy_username, proxy_password,
        fingerprint, os, error_msg, last_used, in_use,
        COALESCE(scroll_count_overall_24h, 0)
    FROM accounts
    """)

    # Drop old table and rename
    await db.execute("DROP TABLE accounts")
    await db.execute("ALTER TABLE accounts_new RENAME TO accounts")

    # Create indexes
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email) WHERE email IS NOT NULL")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone_number) WHERE phone_number IS NOT NULL")

    logger.info("Migration v2 complete")


async def migrate_v3(db: aiosqlite.Connection):
    """
    Replace single `fingerprint` column + unused `os` column with a
    JSON-dict `fingerprints` column keyed by OS, e.g. {"macos": "<json>"}.
    Existing fingerprints are placed in the "macos" slot since this DB
    has only ever run on macOS hosts up to this point.
    """
    async with db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'") as cur:
        row = await cur.fetchone()
        if not row:
            return

        table_sql = row[0]
        if "fingerprint TEXT" not in table_sql:
            logger.info("Database already on v3 schema (no `fingerprint` column), skipping v3 migration")
            return

    logger.info("Migrating accounts table: collapsing `fingerprint` + `os` into per-OS `fingerprints` dict")

    await db.execute("""
    CREATE TABLE IF NOT EXISTS accounts_new (
        email TEXT DEFAULT NULL COLLATE NOCASE,
        password TEXT NOT NULL,
        username TEXT DEFAULT NULL COLLATE NOCASE,
        email_password TEXT DEFAULT NULL,
        phone_number TEXT DEFAULT NULL COLLATE NOCASE,
        active BOOLEAN DEFAULT FALSE NOT NULL,
        locks TEXT DEFAULT '{}' NOT NULL,
        scroll_count_per_endpoint_total TEXT DEFAULT '{}' NOT NULL,
        cookies TEXT DEFAULT '[]' NOT NULL,
        twofa_id TEXT DEFAULT NULL,
        proxy_server TEXT DEFAULT NULL,
        proxy_username TEXT DEFAULT NULL,
        proxy_password TEXT DEFAULT NULL,
        fingerprints TEXT DEFAULT '{}' NOT NULL,
        error_msg TEXT DEFAULT NULL,
        last_used TEXT DEFAULT NULL,
        in_use BOOLEAN DEFAULT FALSE NOT NULL,
        scroll_count_overall_24h INTEGER DEFAULT 0 NOT NULL,
        _tx TEXT DEFAULT NULL,
        CHECK (email IS NOT NULL OR phone_number IS NOT NULL)
    )
    """)

    await db.execute("""
    INSERT INTO accounts_new (
        email, password, username, email_password, phone_number,
        active, locks, scroll_count_per_endpoint_total, cookies,
        twofa_id, proxy_server, proxy_username, proxy_password,
        fingerprints, error_msg, last_used, in_use, scroll_count_overall_24h
    )
    SELECT
        email, password, username, email_password, phone_number,
        active, locks, scroll_count_per_endpoint_total, cookies,
        twofa_id, proxy_server, proxy_username, proxy_password,
        CASE WHEN fingerprint IS NULL THEN '{}' ELSE json_object('macos', fingerprint) END,
        error_msg, last_used, in_use, scroll_count_overall_24h
    FROM accounts
    """)

    await db.execute("DROP TABLE accounts")
    await db.execute("ALTER TABLE accounts_new RENAME TO accounts")

    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email) WHERE email IS NOT NULL")
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone_number) WHERE phone_number IS NOT NULL")

    logger.info("Migration v3 complete")


class DB:
    _init_once: defaultdict[str, bool] = defaultdict(bool)

    def __init__(self, db_path):
        self.db_path: str = str(os.path.join(get_home_dir_path(), "db", db_path))
        self.conn = None

    async def __aenter__(self):
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row

        if not self._init_once[self.db_path]:
            await migrate(db)
            self._init_once[self.db_path] = True

        self.conn = db
        return db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            await self.conn.commit()
            await self.conn.close()


@lock_retry()
async def execute(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        await db.execute(qs, params)


@lock_retry()
async def fetchone(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        async with db.execute(qs, params) as cur:
            row = await cur.fetchone()
            return row


@lock_retry()
async def fetchall(db_path: str, qs: str, params: dict | None = None):
    async with DB(db_path) as db:
        async with db.execute(qs, params) as cur:
            rows = await cur.fetchall()
            return rows


@lock_retry()
async def executemany(db_path: str, qs: str, params: list[dict]):
    async with DB(db_path) as db:
        await db.executemany(qs, params)