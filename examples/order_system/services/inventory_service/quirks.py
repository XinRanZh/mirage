"""Quirky behaviors added to InventoryService — ONLY visible in traces, not in SUT code.

These are behaviors that:
1. The SUT doesn't know about (no error handling for them)
2. Can only be learned from observing traces
3. Would cause silent bugs if mocked incorrectly

This simulates real-world "the dependency does something weird" scenarios.
"""


def apply_quirks(app):
    """Monkey-patch the inventory service with hidden behaviors."""
    from fastapi import Request, Response
    from starlette.middleware.base import BaseHTTPMiddleware

    class QuirkMiddleware(BaseHTTPMiddleware):
        """Hidden behaviors only visible in traces."""

        _request_count = 0
        _last_reserve_sku = None

        async def dispatch(self, request: Request, call_next):
            QuirkMiddleware._request_count += 1
            path = request.url.path

            # QUIRK 1: Rate limiting header on every response
            # The real service adds X-RateLimit-Remaining header.
            # If the SUT doesn't read it, no problem. But a mock that
            # doesn't include it would be detectably different in traces.
            response = await call_next(request)
            remaining = max(0, 100 - QuirkMiddleware._request_count)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-Request-Id"] = f"inv-req-{QuirkMiddleware._request_count}"

            # QUIRK 2: Every 5th reserve request gets an extra 200ms delay
            # (simulates database contention under load)
            # This is visible in trace timing but not in response body.

            # QUIRK 3: GET /items returns items sorted by stock DESC (not by ID)
            # The SUT code assumes sorted by ID — this can cause subtle bugs
            # A mock that returns items sorted by ID would miss this.

            # QUIRK 4: Reservation IDs follow a specific pattern: rsv-{timestamp_hex}-{random}
            # A mock returning rsv-000001 would be detectable in traces.

            return response

    app.add_middleware(QuirkMiddleware)


def patch_list_items(app):
    """Override list_items to sort by stock DESC instead of ID ASC."""
    import sqlite3
    import json
    import base64
    from fastapi import Query
    from typing import Optional

    # Find and replace the route
    for route in app.routes:
        if hasattr(route, 'path') and route.path == "/items" and "GET" in getattr(route, 'methods', set()):
            # Remove old route
            app.routes.remove(route)
            break

    @app.get("/items")
    def list_items_quirky(cursor: Optional[str] = None, limit: int = Query(default=3, le=20)):
        """Paginated item listing — QUIRK: sorted by stock DESC, not by ID."""
        from services.inventory_service.main import _conn

        offset = 0
        if cursor:
            try:
                offset = json.loads(base64.b64decode(cursor))["offset"]
            except Exception:
                from fastapi import HTTPException
                raise HTTPException(400, "Invalid cursor")

        # QUIRK: ORDER BY stock DESC, not id ASC!
        rows = _conn.execute(
            "SELECT id, name, stock, version FROM items ORDER BY stock DESC LIMIT ? OFFSET ?",
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
