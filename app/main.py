from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.idempotency import IdempotencyConflict, IdempotencyInProgress
from app.ledger import AccountNotFound, ConcurrentModification, InvalidTransition
from app.routes_payments import router as payments_router

app = FastAPI(title="Idempotent Payments Ledger")
app.include_router(payments_router)


@app.exception_handler(IdempotencyConflict)
def handle_idempotency_conflict(request: Request, exc: IdempotencyConflict):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(IdempotencyInProgress)
def handle_idempotency_in_progress(request: Request, exc: IdempotencyInProgress):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(InvalidTransition)
def handle_invalid_transition(request: Request, exc: InvalidTransition):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ConcurrentModification)
def handle_concurrent_modification(request: Request, exc: ConcurrentModification):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(AccountNotFound)
def handle_account_not_found(request: Request, exc: AccountNotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Confirms the API can reach Postgres."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
