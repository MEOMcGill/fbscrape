"""The 24h scroll-count reset, which lives in account acquisition.

Regression cover for a reset that silently never fired. It used to sit in
`login._on_login_success`, guarded by
`now - session.account.last_used > timedelta(hours=24)`. But
`AccountsPool._get_and_mark_in_use` stamps `last_used = now` while acquiring
the account and hands back the POST-update row, so that delta was always ~0s
and no account ever reset. `scroll_count_overall_24h` then grew for the life
of the account (237k observed on accounts idle for days), which also broke
the pool's default `scroll_count_overall_24h ASC` ordering: it sorted by
lifetime load rather than recent load, so rest could never move an account up
the queue and only a manual reset could.

The reset now happens inside the acquisition UPDATE, the only place that can
still see the previous `last_used`.
"""

import sqlite3

import pytest

from fbscrape.accounts_pool import AccountsPool


def _set_state(db, identifier, *, last_used_sql, count, endpoint_total=None):
    """Backdate an account and give it a scroll count, bypassing the pool.

    `last_used_sql` is interpolated as SQL, not bound, so callers can pass
    either a literal timestamp (`'2026-11-05 13:31:00'`) or an expression
    (`datetime('now', '-1 minute')`). Test-only input.
    """
    con = sqlite3.connect(db)
    con.execute(
        f"UPDATE accounts SET last_used = {last_used_sql}, "
        "scroll_count_overall_24h = ?, scroll_count_per_endpoint_total = ? "
        "WHERE COALESCE(email, phone_number) = ?",
        (
            count,
            endpoint_total if endpoint_total is not None else '{"GroupTimeline": %d}' % count,
            identifier,
        ),
    )
    con.commit()
    con.close()


def _read(db, identifier):
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT scroll_count_overall_24h, scroll_count_per_endpoint_total, last_used "
        "FROM accounts WHERE COALESCE(email, phone_number) = ?",
        (identifier,),
    ).fetchone()
    con.close()
    return row


@pytest.fixture
def pool(tmp_path):
    return AccountsPool(db_file=str(tmp_path / "accounts.db"))


@pytest.mark.asyncio
async def test_idle_over_24h_resets_scroll_counts_on_acquire(pool):
    await pool.add_account(password="pw", phone_number="+10000000001")
    await pool.set_active("+10000000001", True, None)
    # Four days idle, carrying a large accumulated count.
    _set_state(pool._db_file, "+10000000001",
               last_used_sql="'2026-11-05 13:31:00'", count=237035)

    acc = await pool.get_available()
    assert acc is not None

    count, endpoint_total, _ = _read(pool._db_file, "+10000000001")
    assert count == 0, "a >24h idle account must start from a clean window"
    assert endpoint_total == "{}"
    # The Account handed to the caller reflects the reset, not the stale value.
    assert acc.scroll_count_overall_24h == 0


@pytest.mark.asyncio
async def test_recently_used_account_keeps_its_scroll_count(pool):
    await pool.add_account(password="pw", phone_number="+10000000002")
    await pool.set_active("+10000000002", True, None)
    # Used a minute ago — inside the window, so the count must survive.
    _set_state(pool._db_file, "+10000000002",
               last_used_sql="datetime('now', '-1 minute')", count=500)
    await pool.get_available()

    count, endpoint_total, _ = _read(pool._db_file, "+10000000002")
    assert count == 500, "an account used within 24h must keep its window"
    assert endpoint_total == '{"GroupTimeline": 500}'


@pytest.mark.asyncio
async def test_acquisition_still_stamps_last_used_and_marks_in_use(pool):
    """The reset must not disturb what acquisition already guaranteed."""
    await pool.add_account(password="pw", phone_number="+10000000003")
    await pool.set_active("+10000000003", True, None)
    _set_state(pool._db_file, "+10000000003",
               last_used_sql="'2026-11-05 13:31:00'", count=10)

    await pool.get_available()

    con = sqlite3.connect(pool._db_file)
    in_use, last_used = con.execute(
        "SELECT in_use, last_used FROM accounts "
        "WHERE COALESCE(email, phone_number) = '+10000000003'"
    ).fetchone()
    con.close()
    assert in_use == 1
    assert last_used != "2026-11-05 13:31:00", "last_used must advance to now"


@pytest.mark.asyncio
async def test_default_ordering_breaks_ties_by_least_recently_used(pool):
    """Two accounts tied at 0 must not resolve to the same one every time.

    After the reset lands, every rested account sits at 0, so the primary
    sort key stops discriminating. Without the `last_used ASC` tie-break a
    single account can absorb an entire batch.
    """
    for n, last_used in (("+10000000010", "'2026-11-01 00:00:00'"),
                         ("+10000000011", "'2026-11-02 00:00:00'")):
        await pool.add_account(password="pw", phone_number=n)
        await pool.set_active(n, True, None)
        _set_state(pool._db_file, n, last_used_sql=last_used, count=0)

    first = await pool.get_available()
    second = await pool.get_available()

    assert {first.identifier, second.identifier} == {"+10000000010", "+10000000011"}
    # Oldest last_used is handed out first.
    assert first.identifier == "+10000000010"
