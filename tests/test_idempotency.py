
import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.idempotency import (
    IdempotencyConflict,
    IdempotencyInProgress,
    check_idempotency,
    compute_request_hash,
    store_idempotent_response,
)
from app.models import Account, Base



# Unit-level fixtures: exercise check_idempotency()/store_idempotent_response()
# directly, with no HTTP layer involved.



@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def fake_redis():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_new_key_returns_none(db_session, fake_redis):
   
    request_hash = compute_request_hash("POST", "/payments", b'{"amount": 100}')
    result = check_idempotency(db_session, fake_redis, "key-A", request_hash)
    assert result is None


def test_concurrent_duplicate_is_blocked(db_session, fake_redis):
    """A second request with the same key, arriving before the first finishes,
    must be rejected rather than allowed to run business logic twice."""
    request_hash = compute_request_hash("POST", "/payments", b'{"amount": 100}')
    check_idempotency(db_session, fake_redis, "key-A", request_hash)  # first claim

    with pytest.raises(IdempotencyInProgress):
        check_idempotency(db_session, fake_redis, "key-A", request_hash)


def test_completed_request_is_replayed(db_session, fake_redis):
    
    request_hash = compute_request_hash("POST", "/payments", b'{"amount": 100}')
    check_idempotency(db_session, fake_redis, "key-A", request_hash)
    store_idempotent_response(
        db_session, fake_redis, "key-A", {"transaction_id": "key-A", "status": "captured"}
    )

    result = check_idempotency(db_session, fake_redis, "key-A", request_hash)
    assert result == {"transaction_id": "key-A", "status": "captured"}


def test_same_key_different_body_is_a_conflict(db_session, fake_redis):
    
    request_hash = compute_request_hash("POST", "/payments", b'{"amount": 100}')
    check_idempotency(db_session, fake_redis, "key-A", request_hash)
    store_idempotent_response(
        db_session, fake_redis, "key-A", {"transaction_id": "key-A", "status": "captured"}
    )

    different_hash = compute_request_hash("POST", "/payments", b'{"amount": 999}')
    with pytest.raises(IdempotencyConflict):
        check_idempotency(db_session, fake_redis, "key-A", different_hash)



# HTTP-level tests: exercise the real POST /payments endpoint, header
# parsing, and exception handlers end to end.


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient against an isolated in-memory DB and fake Redis,
    so these tests never touch the real Postgres/Redis from docker-compose."""
    import app.main as main_module
    import app.redis_client as redis_module
    import app.routes_payments as routes_module

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "redis_client", fake)
    # routes_payments.py imports redis_client at module import time too.
    monkeypatch.setattr(routes_module, "redis_client", fake)

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    TestSession = sessionmaker(bind=test_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    main_module.app.dependency_overrides[main_module.get_db] = override_get_db

    test_client = TestClient(main_module.app)

    # Seed a payer and payee account for the payment endpoint to reference.
    seed_session = TestSession()
    payer = Account(owner="alice")
    payee = Account(owner="merchant_bob")
    seed_session.add_all([payer, payee])
    seed_session.commit()
    test_client.payer_id = payer.id
    test_client.payee_id = payee.id
    seed_session.close()

    yield test_client

    main_module.app.dependency_overrides.clear()


def _payment_body(client, amount=500):
    return {
        "payer_account_id": client.payer_id,
        "payee_account_id": client.payee_id,
        "amount": amount,
    }


def test_retry_with_same_key_does_not_recapture(client):
    headers = {"Idempotency-Key": "order-42"}
    body = _payment_body(client)

    first = client.post("/payments", json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "captured"
    txn_id = first.json()["transaction_id"]

    retry = client.post("/payments", json=body, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["transaction_id"] == txn_id, "retry created a second transaction"

    # Confirm business logic really did not rerun: exactly one capture
    # posting (2 ledger entries), not two.
    detail = client.get(f"/payments/{txn_id}")
    capture_entries = [e for e in detail.json()["ledger_entries"] if e["memo"] == "capture"]
    assert len(capture_entries) == 2, "retry re-ran capture and double-posted ledger entries"


def test_same_key_different_body_returns_422(client):
    headers = {"Idempotency-Key": "order-42"}
    client.post("/payments", json=_payment_body(client, amount=500), headers=headers)

    conflict = client.post("/payments", json=_payment_body(client, amount=999), headers=headers)
    assert conflict.status_code == 422


def test_different_keys_both_execute(client):
    r1 = client.post(
        "/payments", json=_payment_body(client, amount=1), headers={"Idempotency-Key": "order-1"}
    )
    r2 = client.post(
        "/payments", json=_payment_body(client, amount=1), headers={"Idempotency-Key": "order-2"}
    )
    assert r1.json()["transaction_id"] != r2.json()["transaction_id"]
    assert r1.status_code == r2.status_code == 200


def test_missing_idempotency_key_header_is_rejected(client):
    resp = client.post("/payments", json=_payment_body(client))
    assert resp.status_code == 422
