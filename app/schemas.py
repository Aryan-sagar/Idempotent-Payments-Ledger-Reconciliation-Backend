from pydantic import BaseModel, ConfigDict, Field


class CreatePaymentRequest(BaseModel):
    payer_account_id: str
    payee_account_id: str
    amount: float = Field(gt=0)


class LedgerEntryOut(BaseModel):
    id: str
    account_id: str
    direction: str
    amount: float
    memo: str | None

    model_config = ConfigDict(from_attributes=True)


class PaymentResponse(BaseModel):
    transaction_id: str
    status: str
    version: int
    payer_account_id: str
    payee_account_id: str
    amount: float


class PaymentDetailResponse(PaymentResponse):
    ledger_entries: list[LedgerEntryOut]
