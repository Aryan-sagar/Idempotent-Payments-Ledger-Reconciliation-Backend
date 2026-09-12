
import sys
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Account, LedgerEntry


@dataclass
class ReconciliationReport:
    global_debits: Decimal = Decimal("0")
    global_credits: Decimal = Decimal("0")
    account_mismatches: list = field(default_factory=list)  # list[dict]
    duplicate_owners: list = field(default_factory=list)  # list[str]

    @property
    def is_clean(self) -> bool:
        return (
            self.global_debits == self.global_credits
            and not self.account_mismatches
            and not self.duplicate_owners
        )


def check_global_balance(db: Session) -> tuple[Decimal, Decimal]:
    """Sum every debit and every credit ever posted. For a correct
    double-entry ledger these must be exactly equal, always -- this is the
    single invariant the whole system exists to preserve."""
    debits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.direction == "debit"
        )
    ).scalar_one()
    credits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.direction == "credit"
        )
    ).scalar_one()
    return Decimal(str(debits)), Decimal(str(credits))


def check_account_balances(db: Session) -> list[dict]:
    """Recompute each account's balance from its ledger_entries (the
    append-only source of truth) and compare against balance_cache (the
    derived/cached value the API actually reads). A mismatch here is
    exactly what the balance_cache lost-update race used to produce
    silently -- this is the check that would have caught it."""
    mismatches = []
    for account in db.query(Account).all():
        credits = db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.account_id == account.id,
                LedgerEntry.direction == "credit",
            )
        ).scalar_one()
        debits = db.execute(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.account_id == account.id,
                LedgerEntry.direction == "debit",
            )
        ).scalar_one()
        recomputed = Decimal(str(credits)) - Decimal(str(debits))
        cached = Decimal(str(account.balance_cache))
        if recomputed != cached:
            mismatches.append(
                {
                    "account_id": account.id,
                    "owner": account.owner,
                    "cached_balance": cached,
                    "recomputed_balance": recomputed,
                    "drift": cached - recomputed,
                }
            )
    return mismatches


def check_duplicate_owners(db: Session) -> list[str]:
    """accounts.owner is supposed to be unique (see the
    add_accounts_owner_unique_constraint migration). This check catches the
    case where that constraint is missing or was added after duplicates
    already existed -- the exact scenario the escrow-account bug relied on."""
    rows = (
        db.query(Account.owner, func.count(Account.id))
        .group_by(Account.owner)
        .having(func.count(Account.id) > 1)
        .all()
    )
    return [owner for owner, _count in rows]


def run_reconciliation(db: Session) -> ReconciliationReport:
    debits, credits = check_global_balance(db)
    return ReconciliationReport(
        global_debits=debits,
        global_credits=credits,
        account_mismatches=check_account_balances(db),
        duplicate_owners=check_duplicate_owners(db),
    )


def _print_report(report: ReconciliationReport) -> None:
    print(f"Global debits:  {report.global_debits}")
    print(f"Global credits: {report.global_credits}")
    if report.global_debits != report.global_credits:
        print("  MISMATCH -- the ledger does not balance globally")

    if report.duplicate_owners:
        print(f"Duplicate account owners: {report.duplicate_owners}")
    else:
        print("No duplicate account owners.")

    if report.account_mismatches:
        print(f"{len(report.account_mismatches)} account(s) with drifted balance_cache:")
        for m in report.account_mismatches:
            print(f"  {m['owner']} ({m['account_id']}): "
                  f"cached={m['cached_balance']} recomputed={m['recomputed_balance']} "
                  f"drift={m['drift']}")
    else:
        print("All account balance_cache values match their ledger entries.")

    print("CLEAN" if report.is_clean else "DISCREPANCIES FOUND")


if __name__ == "__main__":
    session = SessionLocal()
    try:
        report = run_reconciliation(session)
        _print_report(report)
    finally:
        session.close()
    sys.exit(0 if report.is_clean else 1)
