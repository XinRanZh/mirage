"""PaymentService — Payment processing with auth tokens, transient failures, idempotency.

Tricky patterns:
1. Auth token lifecycle: tokens expire, 401 on expired → caller must refresh + retry
2. Transient 503: random failures under simulated load → caller must retry with backoff
3. Idempotency key: duplicate charge detection via request header
"""

from __future__ import annotations

import os
import random
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel

app = FastAPI(title="PaymentService")

# ── OTel ────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.tracing import setup_tracing

tracer = setup_tracing("payment-service", app)

# ── Config ──────────────────────────────────────────────

TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "30"))  # Short TTL for testing
TRANSIENT_FAILURE_RATE = float(os.environ.get("TRANSIENT_FAILURE_RATE", "0.3"))  # 30% 503 rate

# ── Database ────────────────────────────────────────────

DB_PATH = os.environ.get("PAYMENT_DB", ":memory:")
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def _init_db():
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS charges (
            id TEXT PRIMARY KEY,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            card_last4 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'SUCCEEDED',
            idempotency_key TEXT UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            client_secret TEXT NOT NULL
        );
    """)
    # Seed a test client
    existing = _conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    if existing == 0:
        _conn.execute(
            "INSERT INTO clients (client_id, client_secret) VALUES (?, ?)",
            ("order-service", "secret-order-svc-key"),
        )
        _conn.commit()


_init_db()


@contextmanager
def _tx():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


# ── Helpers ─────────────────────────────────────────────

def _validate_token(authorization: Optional[str]) -> str:
    """Validate bearer token, return client_id or raise 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            detail={"error": "MISSING_TOKEN", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]
    row = _conn.execute(
        "SELECT client_id, expires_at FROM tokens WHERE token = ?", (token,)
    ).fetchone()

    if not row:
        raise HTTPException(
            401,
            detail={"error": "INVALID_TOKEN", "message": "Token not found"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        # Clean up expired token
        _conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
        _conn.commit()
        raise HTTPException(
            401,
            detail={"error": "TOKEN_EXPIRED", "message": "Token has expired. Refresh required."},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return row["client_id"]


def _maybe_transient_failure():
    """Simulate transient 503 under load."""
    if random.random() < TRANSIENT_FAILURE_RATE:
        raise HTTPException(
            503,
            detail={
                "error": "SERVICE_UNAVAILABLE",
                "message": "Payment gateway temporarily unavailable. Retry after backoff.",
                "retry_after": 1,
            },
            headers={"Retry-After": "1"},
        )


# ── Models ──────────────────────────────────────────────

class TokenRequest(BaseModel):
    client_id: str
    client_secret: str


class ChargeRequest(BaseModel):
    amount: float
    currency: str = "USD"
    card_last4: str
    description: str = ""


class RefundRequest(BaseModel):
    reason: str = ""


# ── Endpoints ───────────────────────────────────────────

@app.post("/auth/token")
def create_token(req: TokenRequest):
    """Issue auth token. Tokens expire after TOKEN_TTL_SECONDS.

    Tricky: short TTL means tokens expire during multi-step flows.
    Caller must handle 401 on subsequent calls and refresh.
    """
    row = _conn.execute(
        "SELECT * FROM clients WHERE client_id = ? AND client_secret = ?",
        (req.client_id, req.client_secret),
    ).fetchone()
    if not row:
        raise HTTPException(401, detail={"error": "INVALID_CREDENTIALS"})

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=TOKEN_TTL_SECONDS)

    with _tx():
        _conn.execute(
            "INSERT INTO tokens (token, client_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, req.client_id, now.isoformat(), expires_at.isoformat()),
        )

    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "expires_at": expires_at.isoformat(),
    }


@app.post("/charges", status_code=201)
def create_charge(
    req: ChargeRequest,
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Create a charge. Requires valid bearer token.

    Tricky patterns:
    1. Expired token → 401 (caller must refresh + retry)
    2. Transient 503 (caller must retry with backoff)
    3. Duplicate idempotency key → return previous result (no double-charge)
    """
    _validate_token(authorization)

    # Check idempotency: if same key was used before, return cached result
    if idempotency_key:
        existing = _conn.execute(
            "SELECT * FROM charges WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            return {
                "charge_id": existing["id"],
                "amount": existing["amount"],
                "currency": existing["currency"],
                "card_last4": existing["card_last4"],
                "status": existing["status"],
                "idempotency_key": idempotency_key,
                "deduplicated": True,
            }

    # Simulate transient failure
    _maybe_transient_failure()

    # Process charge
    charge_id = f"ch-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    # Simulate some charges failing based on card
    status = "SUCCEEDED"
    if req.card_last4 == "0000":
        status = "DECLINED"

    with _tx():
        _conn.execute(
            "INSERT INTO charges (id, amount, currency, card_last4, status, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (charge_id, req.amount, req.currency, req.card_last4, status, idempotency_key, now),
        )

    result = {
        "charge_id": charge_id,
        "amount": req.amount,
        "currency": req.currency,
        "card_last4": req.card_last4,
        "status": status,
        "idempotency_key": idempotency_key,
        "deduplicated": False,
    }

    if status == "DECLINED":
        raise HTTPException(
            422,
            detail={"error": "CHARGE_DECLINED", "charge_id": charge_id, **result},
        )

    return result


@app.get("/charges/{charge_id}")
def get_charge(
    charge_id: str,
    authorization: Optional[str] = Header(default=None),
):
    """Get charge details."""
    _validate_token(authorization)

    row = _conn.execute("SELECT * FROM charges WHERE id = ?", (charge_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Charge {charge_id} not found")
    return dict(row)


@app.post("/charges/{charge_id}/refund")
def refund_charge(
    charge_id: str,
    req: RefundRequest,
    authorization: Optional[str] = Header(default=None),
):
    """Refund a charge — compensating action.

    Tricky: can only refund SUCCEEDED charges. Double-refund → 409.
    """
    _validate_token(authorization)

    with _tx():
        row = _conn.execute(
            "SELECT * FROM charges WHERE id = ?", (charge_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Charge {charge_id} not found")

        if row["status"] == "REFUNDED":
            raise HTTPException(
                409,
                detail={"error": "ALREADY_REFUNDED", "charge_id": charge_id},
            )
        if row["status"] != "SUCCEEDED":
            raise HTTPException(
                409,
                detail={
                    "error": "CANNOT_REFUND",
                    "status": row["status"],
                    "message": f"Cannot refund charge in {row['status']} status",
                },
            )

        _conn.execute(
            "UPDATE charges SET status = 'REFUNDED' WHERE id = ?", (charge_id,)
        )

    return {"charge_id": charge_id, "status": "REFUNDED", "reason": req.reason}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
