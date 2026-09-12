"""Proves the Account.balance_cache lost-update race is fixed.

Two threads post many small transfers between the same pair of accounts
concurrently. The original implementation did:

    debit_acc = db.get(Account, debit_account_id)
    credit_acc = db.get(Account, credit_account_id)
    debit_acc.balance_cache -= amount
    credit_acc.balance_cache += amount

which reads balance_cache into Python, computes the new value, and writes
it back -- a classic read-modify-write race. Two threads can both read the
same starting balance before either commits; whichever commits last wins,
silently discarding the other's update. Ledger entries (append-only rows)
are unaffected either way, which is exactly what makes this bug dangerous:
nothing crashes, nothing logs an error, the audit trail is complete and
correct, but balance_cache quietly drifts from what the ledger actually
says.

The fix (see app/ledger.py: post_ledger_pair) does the increment as a
single SQL UPDATE ... SET balance_cache = balance_cache +/- amount, so the
read and write happen atomically inside the database engine rather than
across two round trips through Python.

This test uses a file-backed SQLite database (not :memory:), matching the
pattern already used by test_ledger.py's concurrency test, because
separate threads need separate connections that actually contend with each
other -- an in-memory DB is per-connection and wouldn't be shared.
"""
import threading
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ledger import post_ledger_pair
from app.models import Account, Base

THREADS = 4
TRANSFERS_PER_THREAD = 25
AMOUNT_PER_TRANSFER = Decimal("1")


@pytest.fixture
def file_db_sessionmaker(tmp_path):
    db_path = tmp_path / "balance_race_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_concurrent_transfers_do_not_lose_updates(file_db_sessionmaker):
    Session = file_db_sessionmaker

    setup = Session()
    source = Account(owner="source", balance_cache=Decimal("10000"))
    sink = Account(owner="sink", balance_cache=Decimal("0"))
    setup.add_all([source, sink])
    setup.commit()
    source_id, sink_id = source.id, sink.id
    setup.close()

    errors = []

    def worker():
        session = Session()
        try:
            for _ in range(TRANSFERS_PER_THREAD):
                post_ledger_pair(
                    session,
                    transaction_id="race-test",
                    debit_account_id=source_id,
                    credit_account_id=sink_id,
                    amount=AMOUNT_PER_TRANSFER,
                    memo="concurrency-test-transfer",
                )
        except Exception as e:  # capture in the main thread instead of losing it
            errors.append(e)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker thread(s) raised: {errors}"

    verify = Session()
    total_transferred = AMOUNT_PER_TRANSFER * TRANSFERS_PER_THREAD * THREADS
    refreshed_source = verify.get(Account, source_id)
    refreshed_sink = verify.get(Account, sink_id)

    assert refreshed_source.balance_cache == Decimal("10000") - total_transferred, (
        f"source balance is {refreshed_source.balance_cache}, expected "
        f"{Decimal('10000') - total_transferred} -- some concurrent debit was lost"
    )
    assert refreshed_sink.balance_cache == total_transferred, (
        f"sink balance is {refreshed_sink.balance_cache}, expected {total_transferred} "
        "-- some concurrent credit was lost"
    )
    verify.close()
