"""ShippingService — Async shipping with status polling and time-dependent transitions.

Tricky patterns:
1. Async completion: POST returns 202, status progresses over time
2. Polling required: caller must poll GET until terminal state
3. Time-dependent transitions: PENDING → PROCESSING → SHIPPED (with delays)
4. Occasional failures: some shipments end in FAILED state
5. Cancel only before SHIPPED: cancellation has ordering constraint
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="ShippingService")

# ── OTel ────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.tracing import setup_tracing

tracer = setup_tracing("shipping-service", app)

# ── Config ──────────────────────────────────────────────

PROCESSING_DELAY = float(os.environ.get("SHIPPING_PROCESSING_DELAY", "2.0"))  # seconds
SHIPPING_DELAY = float(os.environ.get("SHIPPING_SHIPPING_DELAY", "3.0"))  # seconds
FAILURE_RATE = float(os.environ.get("SHIPPING_FAILURE_RATE", "0.15"))  # 15% fail

# ── Database ────────────────────────────────────────────

DB_PATH = os.environ.get("SHIPPING_DB", ":memory:")
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def _init_db():
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS shipments (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)


_init_db()


@contextmanager
def _tx():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


# ── Background status progression ───────────────────────

def _progress_shipment(shipment_id: str):
    """Background thread that transitions shipment status over time.

    PENDING → (delay) → PROCESSING → (delay) → SHIPPED or FAILED
    """
    time.sleep(PROCESSING_DELAY)

    # Transition to PROCESSING
    _conn.execute(
        "UPDATE shipments SET status = 'PROCESSING', updated_at = ? WHERE id = ? AND status = 'PENDING'",
        (datetime.now(timezone.utc).isoformat(), shipment_id),
    )
    _conn.commit()

    time.sleep(SHIPPING_DELAY)

    # Transition to SHIPPED or FAILED
    final_status = "FAILED" if random.random() < FAILURE_RATE else "SHIPPED"
    _conn.execute(
        "UPDATE shipments SET status = ?, updated_at = ? WHERE id = ? AND status = 'PROCESSING'",
        (final_status, datetime.now(timezone.utc).isoformat(), shipment_id),
    )
    _conn.commit()


# ── Models ──────────────────────────────────────────────

class ShipmentRequest(BaseModel):
    order_id: str
    address: str
    items: List[str] = []


# ── Endpoints ───────────────────────────────────────────

@app.post("/shipments", status_code=202)
def create_shipment(req: ShipmentRequest):
    """Create shipment — returns 202 Accepted (async processing).

    Tricky: response is 202, not 201. Status starts as PENDING.
    Caller MUST poll GET /shipments/{id} to track progress.
    """
    shipment_id = f"shp-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    with _tx():
        _conn.execute(
            "INSERT INTO shipments (id, order_id, address, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'PENDING', ?, ?)",
            (shipment_id, req.order_id, req.address, now, now),
        )

    # Start background progression
    t = threading.Thread(target=_progress_shipment, args=(shipment_id,), daemon=True)
    t.start()

    return {
        "shipment_id": shipment_id,
        "order_id": req.order_id,
        "status": "PENDING",
        "message": "Shipment created. Poll GET /shipments/{id} for status updates.",
    }


@app.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str):
    """Poll shipment status.

    Tricky: status transitions happen asynchronously.
    PENDING → PROCESSING → SHIPPED (or FAILED)
    Caller must poll until terminal state (SHIPPED, FAILED, CANCELLED).
    """
    row = _conn.execute(
        "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Shipment {shipment_id} not found")

    result = dict(row)
    result["terminal"] = result["status"] in ("SHIPPED", "FAILED", "CANCELLED")
    return result


@app.delete("/shipments/{shipment_id}")
def cancel_shipment(shipment_id: str):
    """Cancel shipment — only allowed before SHIPPED.

    Tricky: ordering constraint. Cannot cancel after SHIPPED.
    PENDING or PROCESSING → CANCELLED (ok)
    SHIPPED → 409 (too late)
    FAILED → 409 (already terminal)
    """
    with _tx():
        row = _conn.execute(
            "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Shipment {shipment_id} not found")

        if row["status"] in ("SHIPPED", "FAILED", "CANCELLED"):
            raise HTTPException(
                409,
                detail={
                    "error": "CANNOT_CANCEL",
                    "current_status": row["status"],
                    "message": f"Cannot cancel shipment in {row['status']} status",
                },
            )

        _conn.execute(
            "UPDATE shipments SET status = 'CANCELLED', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), shipment_id),
        )

    return {"shipment_id": shipment_id, "status": "CANCELLED"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
