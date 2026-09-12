import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
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


class AccountNotFound(Exception):
    """Ledger posting referenced an account id that doesn't exist."""


def get_or_create_escrow_account(db: Session) -> Account:
    """Look up the single platform escrow account, creating it on first use.

    accounts.owner has a unique constraint (see the
    add_accounts_owner_unique_constraint migration), so if two concurrent
    requests both race the "does it exist" check and both try to insert,
    the loser's INSERT raises IntegrityError instead of silently creating
    a second escrow row that later reads would nondeterministically pick
    between.
    """
    acc = db.query(Account).filter_by(owner=ESCROW_ACCOUNT_OWNER).first()
    if acc is not None:
        return acc

    acc = Account(owner=ESCROW_ACCOUNT_OWNER, balance_cache=0)
    db.add(acc)
    try:
        db.commit()
    except IntegrityError:
        # Someone else's concurrent create_escrow won the race and committed
        # first. That's fine -- their row is the real one; use it.
        db.rollback()
        acc = db.query(Account).filter_by(owner=ESCROW_ACCOUNT_OWNER).first()
        if acc is None:
            raise  # something other than the expected unique-violation happened
        return acc
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

    # Balance updates are done as a single atomic SQL UPDATE
    # (balance_cache = balance_cache +/- amount) rather than a Python
    # read-modify-write (db.get(...) then mutate the attribute). The naive
    # version reads the current value into Python, computes the new value,
    # and writes it back -- two concurrent postings to the *same* account
    # (the escrow account is shared by every transaction, so this isn't a
    # rare case) can both read the same starting balance before either
    # commits, and whichever commits last silently overwrites the other's
    # update with no error. Doing the arithmetic inside the UPDATE statement
    # means the read and write happen atomically in the database engine, so
    # the second UPDATE always sees the first one's result. This makes the
    # per-account optimistic `version` column unnecessary here -- SQL-level
    # atomic increments give the same safety without needing one.
    debit_result = db.execute(
        sa_update(Account)
        .where(Account.id == debit_account_id)
        .values(balance_cache=Account.balance_cache - amount)
    )
    if debit_result.rowcount == 0:
        raise AccountNotFound(f"No such account: {debit_account_id}")

    credit_result = db.execute(
        sa_update(Account)
        .where(Account.id == credit_account_id)
        .values(balance_cache=Account.balance_cache + amount)
    )
    if credit_result.rowcount == 0:
        raise AccountNotFound(f"No such account: {credit_account_id}")

    if commit:
        db.commit()


def create_and_capture_payment(
    db: Session,
    payer_account_id: str,
    payee_account_id: str,
    amount,
    idempotency_key: str,
) -> Transaction:
    """Create a transaction (if one doesn't already exist for this
    idempotency_key) and drive it to 'captured'.

    Resumable by design: 'authorized' commits on its own before 'captured'
    + the ledger postings commit together, so a process crash between those
    two commits leaves a transaction stuck in 'authorized'. Without the
    lookup-before-insert below, a retry would try to INSERT a second
    Transaction row with the same idempotency_key, hit the unique
    constraint, and surface as an unhandled 500 instead of a safe replay.
    Looking the row up first means a retry resumes the state machine from
    wherever the crashed attempt left off, instead of colliding with it.
    """
    amount = Decimal(str(amount))

    txn = db.query(Transaction).filter_by(idempotency_key=idempotency_key).first()
    if txn is None:
        txn = Transaction(
            idempotency_key=idempotency_key,
            status="created",
            version=0,
            amount=amount,
            payer_account_id=payer_account_id,
            payee_account_id=payee_account_id,
        )
        db.add(txn)
        try:
            db.commit()
        except IntegrityError:
            # Lost a create-time race: another concurrent call for the same
            # key (e.g. the Redis lock expired mid-request, see
            # app/idempotency.py) inserted its row first. Use the winner's
            # row rather than erroring out.
            db.rollback()
            txn = db.query(Transaction).filter_by(idempotency_key=idempotency_key).first()
        else:
            db.refresh(txn)

    if txn.status not in ("created", "authorized"):
        # Already driven to 'captured' (or beyond) by this attempt or an
        # earlier one -- nothing left to do, return as-is for replay.
        return txn

    if txn.status == "created":
        _transition(db, txn, "authorized")  # no money movement -- safe to commit alone

    if txn.status == "authorized":
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
