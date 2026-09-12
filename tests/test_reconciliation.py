from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ledger import create_and_capture_payment
from app.models import Account, Base
from app.reconciliation import (
    check_account_balances,
    check_duplicate_owners,
    check_global_balance,
    run_reconciliation,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def accounts(db_session):
    payer = Account(owner="alice")
    payee = Account(owner="merchant_bob")
    db_session.add_all([payer, payee])
    db_session.commit()
    db_session.refresh(payer)
    db_session.refresh(payee)
    return payer, payee


def test_clean_ledger_reconciles(db_session, accounts):
    payer, payee = accounts
    create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")

    report = run_reconciliation(db_session)
    assert report.is_clean
    debits, credits = check_global_balance(db_session)
    assert debits == credits == Decimal("100")
    assert check_account_balances(db_session) == []
    assert check_duplicate_owners(db_session) == []


def test_balance_cache_drift_is_detected(db_session, accounts):
    """Directly simulate the kind of corruption the balance_cache race used
    to cause: ledger entries are correct and balanced, but balance_cache on
    one account has drifted. Reconciliation must catch this even though
    nothing about it would raise an exception on its own."""
    payer, payee = accounts
    create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")

    # Corrupt balance_cache directly, bypassing post_ledger_pair -- this is
    # standing in for a lost update from a concurrent write.
    db_session.query(Account).filter_by(id=payer.id).update(
        {"balance_cache": Decimal("-5")}
    )
    db_session.commit()

    report = run_reconciliation(db_session)
    assert not report.is_clean
    assert len(report.account_mismatches) == 1
    assert report.account_mismatches[0]["account_id"] == payer.id
    assert report.account_mismatches[0]["cached_balance"] == Decimal("-5")
    assert report.account_mismatches[0]["recomputed_balance"] == Decimal("-100")


def test_duplicate_owner_is_detected():
    """Standing in for the escrow-account duplication bug. accounts.owner
    is unique now, so Base.metadata.create_all() would reject this setup
    -- which is exactly the point: this test builds the accounts table
    *without* that constraint (raw DDL, deliberately), representing a
    database that predates the add_accounts_owner_unique_constraint
    migration, or one where it was never applied. Reconciliation should
    catch the duplicate independently, rather than relying solely on a
    constraint that migration drift could leave missing.
    """
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE accounts (
                    id VARCHAR PRIMARY KEY,
                    owner VARCHAR NOT NULL,
                    balance_cache NUMERIC(18, 2) NOT NULL DEFAULT 0,
                    created_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text("INSERT INTO accounts (id, owner, balance_cache) VALUES (:id, :owner, 0)"),
            [
                {"id": "escrow-1", "owner": "PLATFORM_ESCROW"},
                {"id": "escrow-2", "owner": "PLATFORM_ESCROW"},
            ],
        )
    Base.metadata.tables["ledger_entries"].create(engine, checkfirst=True)

    db = sessionmaker(bind=engine)()
    try:
        report = run_reconciliation(db)
    finally:
        db.close()

    assert not report.is_clean
    assert "PLATFORM_ESCROW" in report.duplicate_owners
