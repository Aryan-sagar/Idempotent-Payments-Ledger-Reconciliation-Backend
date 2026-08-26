from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.idempotency import check_idempotency, compute_request_hash, store_idempotent_response
from app.ledger import (
    get_ledger_entries_for_transaction,
    get_transaction,
    refund_payment,
    settle_payment,
)
from app.ledger import create_and_capture_payment as _create_and_capture_payment
from app.redis_client import redis_client
from app.schemas import CreatePaymentRequest, PaymentDetailResponse, PaymentResponse

router = APIRouter()


def _to_payment_response(txn) -> dict:
    return {
        "transaction_id": txn.id,
        "status": txn.status,
        "version": txn.version,
        "payer_account_id": txn.payer_account_id,
        "payee_account_id": txn.payee_account_id,
        "amount": float(txn.amount),
    }


@router.post("/payments", response_model=PaymentResponse)
async def create_payment(
    request: Request,
    payload: CreatePaymentRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
   
    body = await request.body()
    request_hash = compute_request_hash("POST", "/payments", body)

    cached = check_idempotency(db, redis_client, idempotency_key, request_hash)
    if cached is not None:
        return cached

    txn = _create_and_capture_payment(
        db,
        payer_account_id=payload.payer_account_id,
        payee_account_id=payload.payee_account_id,
        amount=payload.amount,
        idempotency_key=idempotency_key,
    )
    result = _to_payment_response(txn)
    store_idempotent_response(db, redis_client, idempotency_key, result)
    return result


@router.get("/payments/{transaction_id}", response_model=PaymentDetailResponse)
def get_payment(transaction_id: str, db: Session = Depends(get_db)):
    txn = get_transaction(db, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail=f"No such transaction: {transaction_id}")
    entries = get_ledger_entries_for_transaction(db, transaction_id)
    return {
        **_to_payment_response(txn),
        "ledger_entries": [
            {
                "id": e.id,
                "account_id": e.account_id,
                "direction": e.direction,
                "amount": float(e.amount),
                "memo": e.memo,
            }
            for e in entries
        ],
    }


@router.post("/payments/{transaction_id}/settle", response_model=PaymentResponse)
def settle(transaction_id: str, db: Session = Depends(get_db)):
    
    txn = get_transaction(db, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail=f"No such transaction: {transaction_id}")
    txn = settle_payment(db, txn)
    return _to_payment_response(txn)


@router.post("/payments/{transaction_id}/refund", response_model=PaymentResponse)
def refund(transaction_id: str, db: Session = Depends(get_db)):
    """captured|settled -> refunded."""
    txn = get_transaction(db, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail=f"No such transaction: {transaction_id}")
    txn = refund_payment(db, txn)
    return _to_payment_response(txn)
