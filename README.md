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

Account balance updates are done as a single atomic SQL statement (`balance_cache = balance_cache +/- amount`) rather than a Python read-modify-write, so the read and write happen inside one database-engine step instead of two round trips that concurrent requests could interleave. See [Known Failure Modes](#known-failure-modes--fixes) below for the earlier version of this that didn't do that, and why it mattered.

---

## Known Failure Modes & Fixes

This project went through a deliberate pass of asking "where does this break under concurrent load or a mid-transaction crash," rather than stopping once the happy path worked. These are the failure modes found, confirmed by tracing through the actual code, and fixed:

| # | Failure mode | Why it mattered | Fix |
|---|---|---|---|
| 1 | **`Account.balance_cache` lost-update race.** The original code read a balance into Python, mutated it, and wrote it back (`db.get()` → `acc.balance_cache -= amount` → commit). Two concurrent postings to the *same* account — the escrow account is shared by every transaction in the system, so this is normal load, not an edge case — could both read the same starting value and one commit would silently discard the other's update. Nothing raised an exception; the ledger entries stayed correct and append-only, but the cached balance just became wrong. | Silent financial data corruption with no error signal, in the most contended row in the schema. | `post_ledger_pair` now issues `UPDATE accounts SET balance_cache = balance_cache +/- :amount WHERE id = :id` as one SQL statement, so the read and write are atomic at the database engine level. Proven by `tests/test_concurrency_balance_race.py`, which runs concurrent transfers from multiple threads and asserts no update is lost. |
| 2 | **Escrow account duplication.** A migration named `make_account_owner_unique` had an empty body — no constraint was ever created. `get_or_create_escrow_account()`'s existence check had no protection against two concurrent callers both finding "no escrow account" and both inserting one. `.first()` with no deterministic ordering would then arbitrarily pick between two "the same" account. | Money could be split across two rows that the system treats as one account. | Added `uq_accounts_owner` via a real migration, `unique=True` on the model, and a race-safe create path that catches the `IntegrityError` from losing a concurrent insert race and uses the winner's row instead of erroring. |
| 3 | **No crash-recovery path for a stuck transaction.** A transaction transitions `created → authorized` (committed on its own) then `authorized → captured` + ledger postings (committed together). A crash between those two commits left a transaction permanently stuck in `authorized`; retrying with the same idempotency key tried to `INSERT` a second `Transaction` row with the same key, hit the unique constraint, and surfaced as an unhandled 500 instead of a safe replay. | A single mid-flight crash converted "the client should just retry" into "this payment can never be retried again." | `create_and_capture_payment` now looks up an existing row by idempotency key before creating one, and — if found — resumes the state machine from wherever it stopped instead of attempting a second insert. |
| 4 | **Idempotency lock TTL vs. request duration.** The Redis "in progress" lock expires after 30s. If business logic legitimately takes longer than that (DB contention, slow query), a retry arriving in that window no longer sees the lock as held and re-enters the "crashed prior attempt" recovery branch concurrently with the still-running original attempt. | A slow request, not just a crashed one, could trigger a duplicate-processing attempt. | Mitigated by fix #3 above: the resumable state machine means a second concurrent call for the same key converges on the same transaction (or safely loses an insert race) instead of double-processing. |
| 5 | Redis was written as a "fast-path" response cache (`store_idempotent_response`) but never read anywhere — `check_idempotency` always went to Postgres regardless. | The two-layer Redis/Postgres strategy described in this README wasn't actually wired up on the read side. | `check_idempotency` now checks the Redis cache first (as a hash-validated envelope, so a key reused with a different body is still rejected) before touching Postgres. |

None of the above raised an exception or failed a test before being found — they're the class of bug that only shows up as a discrepancy days later, which is exactly what `app/reconciliation.py` (see below) exists to catch independently of whatever the application layer believes.

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
* concurrency regression behavior on transaction state (`test_ledger.py`)
* concurrency regression behavior on account balances (`test_concurrency_balance_race.py`)
* reconciliation invariant checks: global balance, per-account drift, duplicate accounts (`test_reconciliation.py`)

---

## Reconciliation

`app/reconciliation.py` independently re-derives the ledger's invariants instead of trusting the application layer's bookkeeping:

* **Global balance** — `SUM(debits) == SUM(credits)` across every ledger entry ever posted. This is the one invariant double-entry accounting exists to guarantee.
* **Per-account drift** — recomputes each account's balance from its `ledger_entries` and compares it against the cached `balance_cache` column, surfacing exactly the kind of silent drift the balance-race bug (see above) used to cause.
* **Duplicate accounts** — flags any `owner` value with more than one account row, independently of whether the unique constraint is actually in place in a given database.

Run it standalone against the configured database:

```bash
python -m app.reconciliation
```

Exits `0` with `CLEAN` if nothing is wrong, `1` with an itemized discrepancy report otherwise — suitable for a scheduled job or a CI/deploy gate.

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
* [x] Optimistic locking (transaction state)
* [x] Atomic balance updates (fixed a real lost-update race — see Known Failure Modes)
* [x] GET payment endpoint
* [x] Settlement/refund API routes
* [x] Integration tests
* [x] Concurrency regression tests (transaction state *and* account balances)
* [x] Race-safe escrow account creation + real unique constraint
* [x] Crash-recoverable payment creation (resumable state machine, no stuck transactions)
* [x] Reconciliation module (global balance, per-account drift, duplicate-account detection)
* [x] AccountNotFound / 404 handling on bad account ids in ledger postings

### Planned

* [ ] Production Dockerfile (currently empty)
* [ ] Full Docker Compose application stack (currently only `db`/`redis` are wired for local dev)
* [ ] Webhook simulation on payment lifecycle events
* [ ] Rate limiting on the payments API
* [ ] Scheduled reconciliation job (cron/worker running `app/reconciliation.py` on an interval, alerting on non-`CLEAN` results, rather than a manual CLI run)
* [ ] Outbox pattern for anything that needs to notify an external system on commit, so notification and ledger state can't diverge across a crash
* [ ] Additional database indexes (e.g. `transactions.status` for reconciliation queries at scale)
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



