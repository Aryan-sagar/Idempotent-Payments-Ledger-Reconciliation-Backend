
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
   
    lock_key = f"idem:lock:{key}"
    # SET NX EX: atomically claim the key only if nobody else holds it.
    # This one call is what makes two truly concurrent requests safe.
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
    # Release the "in progress" lock's meaning by overwriting it with the
    # final response, cached for fast replay without hitting Postgres.
    redis_client.set(f"idem:response:{key}", payload, ex=RECORD_TTL_HOURS * 3600)
