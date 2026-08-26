import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ledger import (
    ConcurrentModification,
    InvalidTransition,
    create_and_capture_payment,
    get_ledger_entries_for_transaction,
    get_or_create_escrow_account,
    refund_payment,
    settle_payment,
)
from app.models import Account, Base, LedgerEntry, Transaction

# ---------------------------------------------------------------------------
# Single-session tests: use an in-memory DB with StaticPool. Fine for
# sequential lifecycle tests since there's no real concurrency to isolate.
# ---------------------------------------------------------------------------


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


def test_capture_moves_funds_payer_to_escrow(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")
    db_session.refresh(payer)
    escrow = get_or_create_escrow_account(db_session)
    db_session.refresh(escrow)

    assert txn.status == "captured"
    assert payer.balance_cache == -100
    assert escrow.balance_cache == 100

    entries = get_ledger_entries_for_transaction(db_session, txn.id)
    assert len(entries) == 2
    assert {e.direction for e in entries} == {"debit", "credit"}
    assert all(e.memo == "capture" for e in entries)


def test_settle_moves_funds_escrow_to_payee(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")

    settle_payment(db_session, txn)
    db_session.refresh(payee)
    escrow = get_or_create_escrow_account(db_session)
    db_session.refresh(escrow)

    assert txn.status == "settled"
    assert escrow.balance_cache == 0
    assert payee.balance_cache == 100


def test_refund_after_settlement_reverses_from_payee(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")
    settle_payment(db_session, txn)

    refund_payment(db_session, txn)
    db_session.refresh(payer)
    db_session.refresh(payee)

    assert txn.status == "refunded"
    assert payer.balance_cache == 0, "payer should be made whole"
    assert payee.balance_cache == 0, "payee's settled funds should be clawed back"


def test_refund_before_settlement_reverses_from_escrow(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")

    refund_payment(db_session, txn)  # refund straight from 'captured', never settled
    db_session.refresh(payer)
    escrow = get_or_create_escrow_account(db_session)
    db_session.refresh(escrow)

    assert txn.status == "refunded"
    assert payer.balance_cache == 0
    assert escrow.balance_cache == 0


def test_cannot_double_refund(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")
    refund_payment(db_session, txn)

    with pytest.raises(InvalidTransition):
        refund_payment(db_session, txn)


def test_cannot_settle_an_already_settled_payment(db_session, accounts):
    payer, payee = accounts
    txn = create_and_capture_payment(db_session, payer.id, payee.id, 100, "idem-1")
    settle_payment(db_session, txn)

    with pytest.raises(InvalidTransition):
        settle_payment(db_session, txn)




@pytest.fixture
def file_db_sessionmaker(tmp_path):
    db_path = tmp_path / "concurrency_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_concurrent_settle_rejects_the_loser(file_db_sessionmaker):
   
    Session = file_db_sessionmaker

    setup = Session()
    payer = Account(owner="alice")
    payee = Account(owner="merchant_bob")
    setup.add_all([payer, payee])
    setup.commit()
    payee_id = payee.id
    txn = create_and_capture_payment(setup, payer.id, payee.id, 50, "idem-race")
    txn_id = txn.id
    setup.close()

    session_a = Session()
    session_b = Session()
    txn_view_a = session_a.get(Transaction, txn_id)
    txn_view_b = session_b.get(Transaction, txn_id)
    assert txn_view_a.version == txn_view_b.version  # both see the same starting state

    settle_payment(session_a, txn_view_a)  # winner

    with pytest.raises(ConcurrentModification):
        settle_payment(session_b, txn_view_b)  # loser must be rejected

    session_a.close()
    session_b.close()

    verify = Session()
    entries = (
        verify.query(LedgerEntry)
        .filter_by(transaction_id=txn_id, memo="settlement")
        .all()
    )
    payee_balance = verify.get(Account, payee_id).balance_cache

    assert len(entries) == 2, (
        f"expected exactly 2 settlement ledger entries, got {len(entries)} -- "
        "the rejected concurrent request illegally posted money movement"
    )
    assert payee_balance == 50, (
        f"expected payee balance 50 (one settlement), got {payee_balance} -- "
        "double-posting occurred"
    )
    verify.close()
