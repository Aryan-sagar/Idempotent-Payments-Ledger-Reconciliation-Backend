import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models import Account, LedgerEntry, Transaction

ESCROW_ACCOUNT_OWNER = "PLATFORM_ESCROW"

ALLOWED_TRANSITIONS = {
    "created": {"authorized", "failed"},
    "authorized": {"captured", "failed"},
    "captured": {"settled", "refunded"},
    "settled": {"refunded"},
    "failed": set(),
    "refunded": set(),
}


class InvalidTransition(Exception):
    """Requested status change isn't allowed from the transaction's current status."""


class ConcurrentModification(Exception):
    """Transaction was modified by another request."""
    
def get_or_create_escrow_account(db: Session) -> Account:
    
    acc = db.query(Account).filter_by(owner=ESCROW_ACCOUNT_OWNER).first()
    if acc is None:
        acc = Account(owner=ESCROW_ACCOUNT_OWNER, balance_cache=0)
        db.add(acc)
        db.commit()
        db.refresh(acc)
    return acc


def _transition(db: Session, txn: Transaction, to_status: str, commit: bool = True) -> None:
    
    allowed = ALLOWED_TRANSITIONS.get(txn.status, set())
    if to_status not in allowed:
        raise InvalidTransition(
            f"Cannot transition transaction {txn.id} from '{txn.status}' to '{to_status}'"
        )

    expected_version = txn.version
    result = db.execute(
        sa_update(Transaction)
        .where(Transaction.id == txn.id, Transaction.version == expected_version)
        .values(status=to_status, version=Transaction.version + 1)
    )
    if result.rowcount == 0:
        db.rollback()
        raise ConcurrentModification(
            f"Transaction {txn.id} was modified concurrently "
            f"(expected version {expected_version}); reload and retry"
        )

    # Keep the in-memory object in sync without needing a query (nothing
    # has been committed yet, so we must not rely on a re-fetch here).
    txn.status = to_status
    txn.version = expected_version + 1

    if commit:
        db.commit()
        db.refresh(txn)


def post_ledger_pair(
    db: Session,
    transaction_id: str,
    debit_account_id: str,
    credit_account_id: str,
    amount,
    memo: str,
    commit: bool = True,
) -> None:
    
    if amount <= 0:
        raise ValueError("amount must be positive")
    amount = Decimal(str(amount))  # avoid Decimal/float TypeError on balance arithmetic below

    db.add_all(
        [
            LedgerEntry(
                account_id=debit_account_id,
                transaction_id=transaction_id,
                amount=amount,
                direction="debit",
                memo=memo,
            ),
            LedgerEntry(
                account_id=credit_account_id,
                transaction_id=transaction_id,
                amount=amount,
                direction="credit",
                memo=memo,
            ),
        ]
    )

    debit_acc = db.get(Account, debit_account_id)
    credit_acc = db.get(Account, credit_account_id)
    debit_acc.balance_cache -= amount
    credit_acc.balance_cache += amount

    if commit:
        db.commit()


def create_and_capture_payment(
    db: Session,
    payer_account_id: str,
    payee_account_id: str,
    amount,
    idempotency_key: str,
) -> Transaction:
    
    amount = Decimal(str(amount))
    txn = Transaction(
        idempotency_key=idempotency_key,
        status="created",
        version=0,
        amount=amount,
        payer_account_id=payer_account_id,
        payee_account_id=payee_account_id,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    _transition(db, txn, "authorized")  # no money movement -- safe to commit alone

    _transition(db, txn, "captured", commit=False)
    escrow = get_or_create_escrow_account(db)
    post_ledger_pair(
        db,
        transaction_id=txn.id,
        debit_account_id=payer_account_id,
        credit_account_id=escrow.id,
        amount=amount,
        memo="capture",
        commit=False,
    )
    db.commit()
    db.refresh(txn)

    return txn


def settle_payment(db: Session, txn: Transaction) -> Transaction:
    
    if txn.status != "captured":
        raise InvalidTransition(
            f"Cannot settle transaction {txn.id}: status is '{txn.status}', not 'captured'"
        )

    _transition(db, txn, "settled", commit=False)

    escrow = get_or_create_escrow_account(db)
    post_ledger_pair(
        db,
        transaction_id=txn.id,
        debit_account_id=escrow.id,
        credit_account_id=txn.payee_account_id,
        amount=txn.amount,
        memo="settlement",
        commit=False,
    )
    db.commit()
    db.refresh(txn)
    return txn


def refund_payment(db: Session, txn: Transaction) -> Transaction:
    
    if txn.status == "captured":
        holder_account_id = get_or_create_escrow_account(db).id
    elif txn.status == "settled":
        holder_account_id = txn.payee_account_id
    else:
        raise InvalidTransition(
            f"Cannot refund transaction {txn.id}: status is '{txn.status}' "
            f"(must be 'captured' or 'settled')"
        )

    _transition(db, txn, "refunded", commit=False)

    post_ledger_pair(
        db,
        transaction_id=txn.id,
        debit_account_id=holder_account_id,
        credit_account_id=txn.payer_account_id,
        amount=txn.amount,
        memo="refund",
        commit=False,
    )
    db.commit()
    db.refresh(txn)
    return txn


def get_transaction(db: Session, transaction_id: str) -> Optional[Transaction]:
    return db.get(Transaction, transaction_id)


def get_ledger_entries_for_transaction(db: Session, transaction_id: str):
    return (
        db.query(LedgerEntry)
        .filter_by(transaction_id=transaction_id)
        .order_by(LedgerEntry.created_at)
        .all()
    )
