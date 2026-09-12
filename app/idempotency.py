import datetime
import hashlib
import json
from typing import Optional

from sqlalchemy.orm import Session

from app.models import IdempotencyKey

LOCK_TTL_SECONDS = 30       # how long a Redis "in progress" claim is honored
RECORD_TTL_HOURS = 24       # how long a completed response can be replayed
 

class IdempotencyConflict(Exception):
    """Same idempotency key reused with a different request body/method/path."""


class IdempotencyInProgress(Exception):
    """Same idempotency key is currently being processed by another request."""


def compute_request_hash(method: str, path: str, body: bytes) -> str:
    """Fingerprint a request so a replayed key can be checked against the
    original request, not just trusted blindly."""
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(path.encode())
    h.update(body)
    return h.hexdigest()


def check_idempotency(
    db: Session, redis_client, key: str, request_hash: str
) -> Optional[dict]:

    # Fast path: a finished response cached in Redis avoids a Postgres round
    # trip entirely. It's an envelope (request_hash + response), not just
    # the raw response, so a key reused with a different body is still
    # caught here instead of only being caught by the slower Postgres path
    # below (previously this cache was written by store_idempotent_response
    # but never read anywhere, making the "Redis fast path" the README
    # describes dead code).
    cached_envelope = redis_client.get(f"idem:response:{key}")
    if cached_envelope is not None:
        envelope = json.loads(cached_envelope)
        if envelope["request_hash"] != request_hash:
            raise IdempotencyConflict(
                f"Idempotency key '{key}' was already used with a different request"
            )
        return envelope["response"]

    lock_key = f"idem:lock:{key}"
    # SET NX EX: atomically claim the key only if nobody else holds it.
    # This one call is what makes two truly concurrent requests safe.
    #
    # Caveat this does NOT cover: if business logic takes longer than
    # LOCK_TTL_SECONDS to finish, the lock expires while the Postgres row is
    # still unfinished. A retry arriving in that window reaches the
    # "crashed prior attempt" branch below and takes ownership again,
    # concurrently with the still-running original. That's a real race
    # against slow requests, not just crashed ones -- the safety net for it
    # is that create_and_capture_payment() is written to be resumable
    # rather than to insert a second row (see app/ledger.py).
    claimed = redis_client.set(lock_key, request_hash, nx=True, ex=LOCK_TTL_SECONDS)

    existing = db.get(IdempotencyKey, key)

    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                f"Idempotency key '{key}' was already used with a different request"
            )
        if existing.response_snapshot is not None:
            # The original request already finished successfully. Replay it.
            return json.loads(existing.response_snapshot)
        # A DB row exists but has no snapshot yet -> some attempt is (or
        # was) in flight. If we didn't get the Redis lock, someone else is
        # actively working on it right now.
        if not claimed:
            raise IdempotencyInProgress(
                f"Request with idempotency key '{key}' is already being processed"
            )
        # We hold the Redis lock but the DB row is still unfinished — this
        # is the "crashed prior attempt" recovery case. Take ownership and
        # let business logic run again.
        return None

    if not claimed:
        
        raise IdempotencyInProgress(
            f"Request with idempotency key '{key}' is already being processed"
        )

    
    db.add(
        IdempotencyKey(
            key=key,
            request_hash=request_hash,
            response_snapshot=None,
            expires_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=RECORD_TTL_HOURS),
        )
    )
    db.commit()
    return None


def store_idempotent_response(
    db: Session, redis_client, key: str, response_body: dict
) -> None:

    payload = json.dumps(response_body)
    row = db.get(IdempotencyKey, key)
    if row is not None:
        row.response_snapshot = payload
        db.commit()
        request_hash = row.request_hash
    else:
        request_hash = None

    # Cache an envelope, not just the raw response, so a replayed key with a
    # different body is still caught on the Redis fast path in
    # check_idempotency() without needing to fall through to Postgres.
    envelope = json.dumps({"request_hash": request_hash, "response": response_body})
    redis_client.set(f"idem:response:{key}", envelope, ex=RECORD_TTL_HOURS * 3600)
