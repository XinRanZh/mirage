"""InventoryService — Stock management with optimistic locking, pagination, phantom reads.

Tricky patterns:
1. Optimistic locking: reserve/confirm requires version match, stale version → 409
2. Cursor-based pagination: opaque cursor, data can shift between pages
3. Phantom reads: stock can change between check and reserve
"""

from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="InventoryService")

# ── OTel ────────────────────────────────────────────────
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.tracing import setup_tracing

tracer = setup_tracing("inventory-service", app)

# ── Database ────────────────────────────────────────────

DB_PATH = os.environ.get("INVENTORY_DB", ":memory:")
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")


def _init_db():
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
    """)
    # Seed data
    existing = _conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if existing == 0:
        items = [
            ("item-001", "Wireless Mouse", 100),
            ("item-002", "Mechanical Keyboard", 50),
            ("item-003", "USB-C Hub", 25),
            ("item-004", "Monitor Stand", 10),
            ("item-005", "Webcam HD", 75),
            ("item-006", "Desk Lamp", 200),
            ("item-007", "Cable Organizer", 500),
            ("item-008", "Laptop Stand", 30),
            ("item-009", "Mouse Pad XL", 150),
            ("item-010", "Screen Protector", 300),
        ]
        _conn.executemany(
            "INSERT INTO items (id, name, stock, version) VALUES (?, ?, ?, 1)",
            items,
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


# ── Models ──────────────────────────────────────────────

class ReserveRequest(BaseModel):
    quantity: int


class ConfirmRequest(BaseModel):
    version: int


# ── Endpoints ───────────────────────────────────────────

@app.get("/items")
def list_items(cursor: Optional[str] = None, limit: int = Query(default=3, le=20)):
    """Paginated item listing with opaque cursor (base64-encoded offset)."""
    offset = 0
    if cursor:
        try:
            offset = json.loads(base64.b64decode(cursor))["offset"]
        except Exception:
            raise HTTPException(400, "Invalid cursor")

    rows = _conn.execute(
        "SELECT id, name, stock, version FROM items ORDER BY id LIMIT ? OFFSET ?",
        (limit + 1, offset),
    ).fetchall()

    has_more = len(rows) > limit
    items = [dict(r) for r in rows[:limit]]

    next_cursor = None
    if has_more:
        next_cursor = base64.b64encode(
            json.dumps({"offset": offset + limit}).encode()
        ).decode()

    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@app.get("/items/{item_id}")
def get_item(item_id: str):
    row = _conn.execute(
        "SELECT id, name, stock, version FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Item {item_id} not found")
    return dict(row)


@app.post("/items/{item_id}/reserve", status_code=201)
def reserve_stock(item_id: str, req: ReserveRequest):
    """Reserve stock — returns reservation with version for optimistic locking.

    Tricky: between reading stock and reserving, stock may have changed (phantom read).
    The version is used later in /confirm to detect conflicts.
    """
    with _tx():
        row = _conn.execute(
            "SELECT stock, version FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Item {item_id} not found")

        if row["stock"] < req.quantity:
            raise HTTPException(
                409,
                detail={
                    "error": "INSUFFICIENT_STOCK",
                    "available": row["stock"],
                    "requested": req.quantity,
                },
            )

        # Decrement stock and bump version
        _conn.execute(
            "UPDATE items SET stock = stock - ?, version = version + 1 WHERE id = ?",
            (req.quantity, item_id),
        )

        reservation_id = f"rsv-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        _conn.execute(
            "INSERT INTO reservations (id, item_id, quantity, version, status, created_at) "
            "VALUES (?, ?, ?, ?, 'PENDING', ?)",
            (reservation_id, item_id, req.quantity, row["version"] + 1, now),
        )

    return {
        "reservation_id": reservation_id,
        "item_id": item_id,
        "quantity": req.quantity,
        "version": row["version"] + 1,
        "status": "PENDING",
    }


@app.post("/reservations/{reservation_id}/confirm")
def confirm_reservation(reservation_id: str, req: ConfirmRequest):
    """Confirm reservation — requires matching version (optimistic lock).

    Tricky: if another reservation changed the item version between reserve and confirm,
    the caller gets 409 and must re-reserve with fresh data.
    """
    with _tx():
        rsv = _conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if not rsv:
            raise HTTPException(404, f"Reservation {reservation_id} not found")
        if rsv["status"] != "PENDING":
            raise HTTPException(
                409,
                detail={
                    "error": "INVALID_STATUS",
                    "current_status": rsv["status"],
                    "message": f"Cannot confirm reservation in {rsv['status']} status",
                },
            )

        # Check version matches current item version
        item = _conn.execute(
            "SELECT version FROM items WHERE id = ?", (rsv["item_id"],)
        ).fetchone()

        if req.version != item["version"]:
            raise HTTPException(
                409,
                detail={
                    "error": "VERSION_CONFLICT",
                    "expected_version": req.version,
                    "current_version": item["version"],
                    "message": "Item was modified since reservation. Re-reserve with fresh data.",
                },
            )

        _conn.execute(
            "UPDATE reservations SET status = 'CONFIRMED' WHERE id = ?",
            (reservation_id,),
        )

    return {"reservation_id": reservation_id, "status": "CONFIRMED"}


@app.delete("/reservations/{reservation_id}")
def cancel_reservation(reservation_id: str):
    """Cancel reservation — compensating action, restores stock."""
    with _tx():
        rsv = _conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if not rsv:
            raise HTTPException(404, f"Reservation {reservation_id} not found")
        if rsv["status"] not in ("PENDING", "CONFIRMED"):
            raise HTTPException(
                409, detail={"error": "CANNOT_CANCEL", "current_status": rsv["status"]}
            )

        # Restore stock
        _conn.execute(
            "UPDATE items SET stock = stock + ?, version = version + 1 WHERE id = ?",
            (rsv["quantity"], rsv["item_id"]),
        )
        _conn.execute(
            "UPDATE reservations SET status = 'CANCELLED' WHERE id = ?",
            (reservation_id,),
        )

    return {"reservation_id": reservation_id, "status": "CANCELLED"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
