import uuid

from sqlalchemy import (
    Column,
    String,
    Integer,
    Numeric,
    DateTime,
    ForeignKey,
    CheckConstraint,
    func,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def gen_uuid() -> str:
    return str(uuid.uuid4())


class Account(Base):
    

    __tablename__ = "accounts"

    id = Column(String, primary_key=True, default=gen_uuid)
    owner = Column(String, nullable=False, unique=True)
    balance_cache = Column(Numeric(precision=18, scale=2), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
   

    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=gen_uuid)
    status = Column(String, nullable=False, default="created")
    idempotency_key = Column(String, unique=True, nullable=False)
    version = Column(Integer, nullable=False, default=0)

    
    amount = Column(Numeric(precision=18, scale=2), nullable=False)
    payer_account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    payee_account_id = Column(String, ForeignKey("accounts.id"), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('created','authorized','captured','settled','failed','refunded')",
            name="ck_transaction_status",
        ),
    )


class LedgerEntry(Base):
    

    __tablename__ = "ledger_entries"

    id = Column(String, primary_key=True, default=gen_uuid)
    account_id = Column(String, ForeignKey("accounts.id"), nullable=False)
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False)
    amount = Column(Numeric(precision=18, scale=2), nullable=False)
    direction = Column(String, nullable=False)  # 'debit' or 'credit'
    # Why this entry was posted -- distinguishes the original capture from a
    # later settlement or refund reversal on the same transaction_id.
    memo = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')", name="ck_direction"),
        CheckConstraint("amount > 0", name="ck_amount_positive"),
    )


class IdempotencyKey(Base):
    

    __tablename__ = "idempotency_keys"

    key = Column(String, primary_key=True)
    request_hash = Column(String, nullable=False)
    response_snapshot = Column(String, nullable=True)  # JSON stored as text
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
