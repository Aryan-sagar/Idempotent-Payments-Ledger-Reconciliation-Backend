# Payments Ledger

A production-oriented payments ledger built with **FastAPI, PostgreSQL, Redis, SQLAlchemy, and Alembic**.

The project focuses on the backend engineering problems that matter in payment systems: **idempotency, double-entry accounting, transaction state management, database consistency, and concurrency safety**.

---

## Architecture

```text
                    ┌─────────────────┐
                    │    FastAPI      │
                    │   Payment API   │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
       ┌──────▼──────┐               ┌──────▼──────┐
       │    Redis    │               │ PostgreSQL  │
       │             │               │             │
       │ Idempotency │               │ Transactions│
       │ Locks/Cache │               │ Accounts    │
       └─────────────┘               │ Ledger      │
                                     └─────────────┘
````

---

## Tech Stack

* **Python**
* **FastAPI**
* **SQLAlchemy**
* **PostgreSQL 16**
* **Redis 7**
* **Alembic**
* **Pydantic**
* **Docker / Docker Compose**
* **Pytest**

---

## Core Features

### Idempotent Payments

Payment requests use an idempotency key to prevent accidental duplicate execution.

The implementation uses a two-layer strategy:

```text
Request
   │
   ▼
Redis SET NX
   │
   ├── Already processing → reject concurrent request
   │
   └── New request
          │
          ▼
   PostgreSQL idempotency record
          │
          ▼
      Execute payment
          │
          ▼
   Store response snapshot
```

Redis provides the fast atomic lock while PostgreSQL acts as the durable source of truth.

The request body is hashed so that reusing an idempotency key with a different request is rejected.

---

## Double-Entry Ledger

The ledger follows double-entry accounting.

Every money movement creates a balanced pair:

```text
DEBIT   Account A     ₹100
CREDIT  Account B     ₹100
```

The fundamental invariant is:

```text
Total Debits = Total Credits
```

Ledger entries are **append-only**.

Corrections such as refunds are represented by new offsetting entries rather than modifying historical ledger records.

---

## Payment Lifecycle

The payment state machine currently supports:

```text
created
   │
   ├── authorized
   │       │
   │       └── captured
   │              │
   │              ├── settled
   │              │
   │              └── refunded
   │
   └── failed
```

A settled payment can also transition to:

```text
settled → refunded
```

Invalid state transitions are rejected.

---

## Ledger Flow

### Capture

Money moves from the payer into platform escrow:

```text
DEBIT   Payer
CREDIT  Platform Escrow
```

### Settlement

Money moves from escrow to the payee:

```text
DEBIT   Platform Escrow
CREDIT  Payee
```

### Refund

Money moves back toward the payer:

```text
DEBIT   Current Holder
CREDIT  Payer
```

Historical ledger entries remain untouched.

---

## Concurrency Safety

The system uses database-level concurrency controls to protect against lost updates.

Transaction state transitions use optimistic locking:

```text
UPDATE transactions
SET
    status = ...,
    version = version + 1
WHERE
    id = ...
    AND version = expected_version
```

If another request has already modified the transaction, the update affects zero rows and the operation is rejected.

Account balance updates use row-level locking to prevent concurrent balance corruption.

---

## Database Schema

```text
accounts
├── id
├── owner
├── balance_cache
└── created_at

transactions
├── id
├── status
├── idempotency_key
├── version
├── amount
├── payer_account_id
├── payee_account_id
├── created_at
└── updated_at

ledger_entries
├── id
├── account_id
├── transaction_id
├── amount
├── direction
├── memo
└── created_at

idempotency_keys
├── key
├── request_hash
├── response_snapshot
├── expires_at
└── created_at
```

`balance_cache` is treated as a derived/cached value. The ledger remains the source of truth.

---

## Project Structure

```text
payments-ledger/
│
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
├── requirements.txt
├── requirements-dev.txt
│
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── redis_client.py
│   ├── idempotency.py
│   ├── ledger.py
│   ├── schemas.py
│   ├── routes_payments.py
│   └── main.py
│
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│
└── tests/
    ├── __init__.py
    ├── test_idempotency.py
    └── test_ledger.py
```

---

## Database Migrations

Alembic is used for schema versioning.

Current migration history includes:

```text
0001 - Initial ledger schema

0002 - Payment metadata
       ├── transaction amount
       ├── payer account
       ├── payee account
       └── ledger memo
```

Apply migrations with:

```bash
alembic upgrade head
```

Check migration consistency with:

```bash
alembic check
```

---

## Running Locally

### 1. Create virtual environment

```bash
python -m venv .venv
```

Activate it on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

For development/testing:

```bash
pip install -r requirements-dev.txt
```

### 3. Start infrastructure

```bash
docker compose up -d db redis
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start FastAPI

```bash
uvicorn app.main:app --reload
```

API:

```text
http://127.0.0.1:8000
```

Swagger documentation:

```text
http://127.0.0.1:8000/docs
```

---

## Environment Variables

Create a `.env` file:

```env
DATABASE_URL=postgresql+psycopg2://ledger_user:ledger_pass@localhost:5432/ledger_db
REDIS_URL=redis://localhost:6379/0
```

Do not commit `.env` to version control.

---

## Testing

Run the complete test suite:

```bash
pytest -v
```

Current test coverage validates:

* idempotent payment retries
* duplicate request prevention
* idempotency-key conflicts
* concurrent idempotency protection
* payment creation
* payment state transitions
* settlement
* refunds
* double-entry accounting
* balance correctness
* append-only ledger behavior
* concurrency regression behavior

Current checkpoint:

```text
15+ tests passed
0 failed
```

---

## Design Principles

### 1. PostgreSQL is the source of truth

Redis improves performance and provides distributed locking, but durable payment state lives in PostgreSQL.

### 2. Ledger entries are immutable

Historical accounting records are never edited or deleted.

### 3. Every money movement balances

Every posted operation must preserve:

```text
Σ debits = Σ credits
```

### 4. Retries must be safe

A client retry must never accidentally execute the same payment twice.

### 5. Concurrency must be explicit

Payment systems cannot rely on application-level assumptions when multiple requests can execute simultaneously.

### 6. Database transactions protect invariants

State changes and corresponding ledger movements are committed atomically whenever they represent one business operation.

---

## Current Status

### Completed

* [x] PostgreSQL infrastructure
* [x] Redis infrastructure
* [x] Application configuration
* [x] SQLAlchemy database layer
* [x] Database models
* [x] Alembic migrations
* [x] FastAPI application
* [x] Health endpoint
* [x] Idempotency layer
* [x] Redis idempotency locking
* [x] PostgreSQL durable idempotency records
* [x] Payment creation
* [x] Payment state machine
* [x] Double-entry ledger
* [x] Escrow account
* [x] Settlement
* [x] Refunds
* [x] Optimistic locking
* [x] Balance concurrency protection
* [x] Integration tests
* [x] Concurrency regression tests

### Planned

* [ ] Complete GET payment endpoint
* [ ] Complete settlement/refund API routes
* [ ] Production Dockerfile
* [ ] Full Docker Compose application stack
* [ ] Improved exception handling
* [ ] Additional database indexes
* [ ] Dependency cleanup
* [ ] Production configuration
* [ ] API documentation
* [ ] Observability / logging
* [ ] Performance testing

---

## Why This Project?

This project is intentionally designed around the difficult parts of payment infrastructure rather than simple CRUD operations.

It demonstrates practical backend concepts including:

* distributed idempotency
* transactional integrity
* double-entry accounting
* state machines
* optimistic concurrency control
* row-level locking
* append-only financial records
* Redis/PostgreSQL coordination
* schema migrations
* integration testing
* concurrency testing

```



