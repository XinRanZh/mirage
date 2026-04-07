"""OrderService — The Caller/SUT that orchestrates orders through downstream dependencies.

This is the System Under Test. It demonstrates complex caller logic:
1. Saga orchestration: reserve → pay → ship → confirm (with compensating actions)
2. Token refresh: handles 401 from PaymentService by refreshing token
3. Retry with backoff: handles 503 from PaymentService
4. Polling: waits for ShippingService async completion
5. Version conflict handling: retries on 409 from InventoryService
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="OrderService")

# ── Request log (for demo watcher) ──────────────────────
import threading as _threading
_request_log: list = []
_request_log_lock = _threading.Lock()

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

class _RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        t0 = time.monotonic()
        method = request.method
        path = request.url.path
        # Skip log endpoint itself and polling from watcher
        if path == "/__sut__/log":
            return await call_next(request)
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as e:
            status = 500
            raise
        finally:
            dur = round((time.monotonic() - t0) * 1000, 1)
            with _request_log_lock:
                _request_log.append({
                    "method": method, "path": path,
                    "status": status, "duration_ms": dur,
                })
                if len(_request_log) > 200:
                    _request_log[:] = _request_log[-100:]
        return response

app.add_middleware(_RequestLogMiddleware)

@app.get("/__sut__/log")
def _sut_log():
    return {"calls": _request_log[-50:], "count": len(_request_log)}

# ── OTel ────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.tracing import setup_tracing

tracer = setup_tracing("order-service", app)

# ── Config ──────────────────────────────────────────────

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://localhost:8001")
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://localhost:8002")
SHIPPING_URL = os.environ.get("SHIPPING_URL", "http://localhost:8003")

PAYMENT_CLIENT_ID = os.environ.get("PAYMENT_CLIENT_ID", "order-service")
PAYMENT_CLIENT_SECRET = os.environ.get("PAYMENT_CLIENT_SECRET", "secret-order-svc-key")

MAX_RETRIES = 3
POLL_INTERVAL = 1.0  # seconds
POLL_TIMEOUT = 15.0  # seconds

# ── State ───────────────────────────────────────────────

_orders: dict[str, dict] = {}
_token_cache: dict[str, str] = {"token": None}
_order_counter = 0


# ── Models ──────────────────────────────────────────────

class OrderRequest(BaseModel):
    item_id: str
    quantity: int
    card_last4: str
    shipping_address: str


# ── Helper: Token Management ────────────────────────────

async def _get_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    """Get or refresh payment auth token.

    Tricky pattern: token can expire mid-flow, so we cache but
    also refresh on 401.
    """
    if _token_cache["token"] and not force_refresh:
        return _token_cache["token"]

    with tracer.start_as_current_span("refresh-payment-token"):
        resp = await client.post(
            f"{PAYMENT_URL}/auth/token",
            json={"client_id": PAYMENT_CLIENT_ID, "client_secret": PAYMENT_CLIENT_SECRET},
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["token"]
        return data["token"]


# ── Helper: Charge with Retry ───────────────────────────

async def _charge_with_retry(
    client: httpx.AsyncClient,
    amount: float,
    card_last4: str,
    idempotency_key: str,
) -> dict:
    """Charge payment with retry on 401 (token refresh) and 503 (transient).

    Tricky patterns exercised:
    - 401 → refresh token → retry (token lifecycle)
    - 503 → exponential backoff → retry (transient failure)
    - Idempotency key ensures no double-charge on retry
    """
    for attempt in range(MAX_RETRIES + 1):
        token = await _get_token(client)

        with tracer.start_as_current_span(
            "charge-attempt", attributes={"attempt": attempt}
        ):
            resp = await client.post(
                f"{PAYMENT_URL}/charges",
                json={
                    "amount": amount,
                    "card_last4": card_last4,
                    "currency": "USD",
                    "description": f"Order charge (attempt {attempt})",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": idempotency_key,
                },
            )

            if resp.status_code == 401:
                # Token expired — refresh and retry
                await _get_token(client, force_refresh=True)
                continue

            if resp.status_code == 503:
                # Transient failure — backoff and retry
                if attempt < MAX_RETRIES:
                    backoff = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s
                    await asyncio.sleep(backoff)
                    continue
                raise HTTPException(502, detail={
                    "error": "PAYMENT_UNAVAILABLE",
                    "message": "Payment service unavailable after retries",
                })

            if resp.status_code == 422:
                # Charge declined
                raise HTTPException(422, detail=resp.json().get("detail", {}))

            resp.raise_for_status()
            return resp.json()

    raise HTTPException(502, detail={"error": "PAYMENT_RETRIES_EXHAUSTED"})


# ── Helper: Poll Shipment ───────────────────────────────

async def _poll_shipment(client: httpx.AsyncClient, shipment_id: str) -> dict:
    """Poll shipping status until terminal state.

    Tricky pattern: async completion requires polling with timeout.
    PENDING → PROCESSING → SHIPPED/FAILED
    """
    start = time.monotonic()

    while time.monotonic() - start < POLL_TIMEOUT:
        with tracer.start_as_current_span("poll-shipment"):
            resp = await client.get(f"{SHIPPING_URL}/shipments/{shipment_id}")
            resp.raise_for_status()
            data = resp.json()

            if data.get("terminal", False):
                return data

        await asyncio.sleep(POLL_INTERVAL)

    raise HTTPException(504, detail={
        "error": "SHIPPING_TIMEOUT",
        "shipment_id": shipment_id,
        "message": "Shipment did not reach terminal state within timeout",
    })


# ── Helper: Compensating Actions ────────────────────────

async def _compensate_reservation(client: httpx.AsyncClient, reservation_id: str):
    """Cancel inventory reservation (compensating action)."""
    with tracer.start_as_current_span("compensate-reservation"):
        try:
            resp = await client.delete(f"{INVENTORY_URL}/reservations/{reservation_id}")
            resp.raise_for_status()
        except Exception:
            pass  # Best-effort compensation


async def _compensate_charge(client: httpx.AsyncClient, charge_id: str, token: str):
    """Refund payment charge (compensating action)."""
    with tracer.start_as_current_span("compensate-charge"):
        try:
            resp = await client.post(
                f"{PAYMENT_URL}/charges/{charge_id}/refund",
                json={"reason": "order_cancelled"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
        except Exception:
            pass  # Best-effort compensation


async def _compensate_shipment(client: httpx.AsyncClient, shipment_id: str):
    """Cancel shipment (compensating action)."""
    with tracer.start_as_current_span("compensate-shipment"):
        try:
            resp = await client.delete(f"{SHIPPING_URL}/shipments/{shipment_id}")
            resp.raise_for_status()
        except Exception:
            pass  # Best-effort compensation


# ── Main Endpoint ───────────────────────────────────────

@app.post("/orders", status_code=201)
async def create_order(req: OrderRequest):
    """Create order — full saga orchestration.

    Flow:
    1. Reserve inventory (handle 409 version conflict by retrying)
    2. Charge payment (handle 401 token refresh + 503 transient retry)
    3. Create shipment (async, 202)
    4. Poll shipment until terminal
    5. Confirm inventory reservation (with version check)

    On failure at any step: compensate all previous successful steps.
    """
    global _order_counter
    _order_counter += 1
    order_id = f"ord-{_order_counter:06d}"
    idempotency_key = f"{order_id}-charge"

    order = {
        "order_id": order_id,
        "item_id": req.item_id,
        "quantity": req.quantity,
        "status": "CREATED",
        "reservation_id": None,
        "charge_id": None,
        "shipment_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "events": [],
    }
    _orders[order_id] = order

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── Step 1: Reserve inventory ──────────────────
        reservation_id = None
        reservation_version = None

        for attempt in range(MAX_RETRIES + 1):
            with tracer.start_as_current_span(
                "reserve-inventory", attributes={"attempt": attempt}
            ):
                resp = await client.post(
                    f"{INVENTORY_URL}/items/{req.item_id}/reserve",
                    json={"quantity": req.quantity},
                )

                if resp.status_code == 201:
                    data = resp.json()
                    reservation_id = data["reservation_id"]
                    reservation_version = data["version"]
                    order["reservation_id"] = reservation_id
                    order["events"].append({"step": "reserve", "status": "ok"})
                    break
                elif resp.status_code == 409:
                    detail = resp.json().get("detail", {})
                    if detail.get("error") == "INSUFFICIENT_STOCK":
                        order["status"] = "FAILED"
                        order["events"].append({"step": "reserve", "status": "insufficient_stock"})
                        raise HTTPException(409, detail={
                            "error": "INSUFFICIENT_STOCK",
                            "order_id": order_id,
                        })
                    # Version conflict — retry
                    continue
                else:
                    resp.raise_for_status()

        if not reservation_id:
            order["status"] = "FAILED"
            raise HTTPException(500, detail={"error": "RESERVATION_FAILED"})

        # ── Step 2: Charge payment ─────────────────────
        try:
            charge_data = await _charge_with_retry(
                client,
                amount=req.quantity * 29.99,  # fixed price for simplicity
                card_last4=req.card_last4,
                idempotency_key=idempotency_key,
            )
            order["charge_id"] = charge_data["charge_id"]
            order["events"].append({"step": "charge", "status": "ok"})
        except HTTPException:
            # Compensate: release reservation
            await _compensate_reservation(client, reservation_id)
            order["status"] = "FAILED"
            order["events"].append({"step": "charge", "status": "failed"})
            raise

        # ── Step 3: Create shipment ────────────────────
        with tracer.start_as_current_span("create-shipment"):
            resp = await client.post(
                f"{SHIPPING_URL}/shipments",
                json={
                    "order_id": order_id,
                    "address": req.shipping_address,
                    "items": [req.item_id],
                },
            )
            if resp.status_code != 202:
                # Compensate: refund + release reservation
                token = await _get_token(client)
                await _compensate_charge(client, order["charge_id"], token)
                await _compensate_reservation(client, reservation_id)
                order["status"] = "FAILED"
                order["events"].append({"step": "ship", "status": "failed"})
                raise HTTPException(502, detail={"error": "SHIPPING_FAILED"})

            ship_data = resp.json()
            order["shipment_id"] = ship_data["shipment_id"]
            order["events"].append({"step": "ship", "status": "accepted"})

        # ── Step 4: Poll shipment status ───────────────
        try:
            ship_result = await _poll_shipment(client, order["shipment_id"])
            order["events"].append({
                "step": "ship_poll",
                "status": ship_result["status"],
            })

            if ship_result["status"] == "FAILED":
                # Compensate: refund + release reservation
                token = await _get_token(client)
                await _compensate_charge(client, order["charge_id"], token)
                await _compensate_reservation(client, reservation_id)
                order["status"] = "FAILED"
                raise HTTPException(502, detail={
                    "error": "SHIPPING_FAILED",
                    "order_id": order_id,
                    "shipment_status": "FAILED",
                })
        except HTTPException:
            raise
        except Exception:
            # Timeout or error — compensate
            token = await _get_token(client)
            await _compensate_charge(client, order["charge_id"], token)
            await _compensate_reservation(client, reservation_id)
            await _compensate_shipment(client, order["shipment_id"])
            order["status"] = "FAILED"
            raise

        # ── Step 5: Confirm inventory reservation ──────
        with tracer.start_as_current_span("confirm-reservation"):
            resp = await client.post(
                f"{INVENTORY_URL}/reservations/{reservation_id}/confirm",
                json={"version": reservation_version},
            )

            if resp.status_code == 409:
                # Version conflict — item changed since reservation
                # In production, we'd re-reserve. Here we accept and log.
                order["events"].append({
                    "step": "confirm",
                    "status": "version_conflict",
                    "detail": resp.json(),
                })
            elif resp.status_code == 200:
                order["events"].append({"step": "confirm", "status": "ok"})
            else:
                order["events"].append({"step": "confirm", "status": f"error_{resp.status_code}"})

        # ── Done ───────────────────────────────────────
        order["status"] = "COMPLETED"
        return order


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in _orders:
        raise HTTPException(404, f"Order {order_id} not found")
    return _orders[order_id]


@app.get("/orders")
def list_orders():
    return {"orders": list(_orders.values()), "count": len(_orders)}


# ── Single-dependency demo endpoints ────────────────────
# These proxy to exactly one dependency, useful for demos.

@app.get("/demo/inventory/{item_id}")
async def demo_check_inventory(item_id: str):
    """Check stock for an item. Calls only InventoryService."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        with tracer.start_as_current_span("demo-check-inventory"):
            resp = await client.get(f"{INVENTORY_URL}/items/{item_id}")
            if resp.status_code == 404:
                raise HTTPException(404, f"Item {item_id} not found")
            resp.raise_for_status()
            return resp.json()


@app.post("/demo/payment/token")
async def demo_get_payment_token():
    """Get a payment auth token. Calls only PaymentService."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        with tracer.start_as_current_span("demo-get-token"):
            token = await _get_token(client, force_refresh=True)
            return {"token": token}


@app.post("/demo/shipping")
async def demo_create_shipment(address: str = "123 Demo St"):
    """Create a shipment. Calls only ShippingService."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        with tracer.start_as_current_span("demo-create-shipment"):
            resp = await client.post(
                f"{SHIPPING_URL}/shipments",
                json={"order_id": "demo-001", "address": address, "items": ["demo-item"]},
            )
            resp.raise_for_status()
            return resp.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
