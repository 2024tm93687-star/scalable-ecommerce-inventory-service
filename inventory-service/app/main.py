import asyncio
import csv
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Body, HTTPException, Query, Request
from pydantic import BaseModel, Field

from common.eci_common import create_service_app, get_db, rows_to_dicts, transaction, utc_now_iso


SERVICE_NAME = "inventory-service"
DB_PATH = os.getenv("DATABASE_PATH", "/tmp/eci/inventory/inventory.db")
SEED_DIR = os.getenv("SEED_DIR", "/app/data/eci-seed")
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "5"))
REAPER_INTERVAL_SECONDS = int(os.getenv("REAPER_INTERVAL_SECONDS", "30"))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inventory (
    inventory_id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    on_hand INTEGER NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    low_stock_threshold INTEGER NOT NULL DEFAULT 5,
    UNIQUE(product_id, warehouse)
);
CREATE TABLE IF NOT EXISTS inventory_movements (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    order_id INTEGER,
    type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_key TEXT NOT NULL UNIQUE,
    order_id INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS reservation_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY(reservation_id) REFERENCES reservations(reservation_id) ON DELETE CASCADE
);
"""


class ReserveItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1)


class ReserveRequest(BaseModel):
    order_id: Optional[int] = None
    idempotency_key: str = Field(min_length=8, max_length=120)
    ttl_minutes: int = Field(default=15, ge=1, le=120)
    items: list[ReserveItem]


class ReleaseRequest(BaseModel):
    reservation_key: Optional[str] = None
    order_id: Optional[int] = None
    reason: str = "MANUAL_RELEASE"


class ShipRequest(BaseModel):
    reservation_key: str
    order_id: int


app, logger, metrics = create_service_app(SERVICE_NAME)
reaper_task: Optional[asyncio.Task] = None


def init_db() -> None:
    with get_db(DB_PATH) as conn:
        conn.executescript(SCHEMA_SQL)
        count = conn.execute("SELECT COUNT(*) AS count FROM inventory").fetchone()["count"]
        if count:
            return
        products_file = os.path.join(SEED_DIR, "eci_products_indian.csv")
        inventory_file = os.path.join(SEED_DIR, "eci_inventory_indian.csv")
        if not (os.path.exists(products_file) and os.path.exists(inventory_file)):
            return
        product_map: dict[int, str] = {}
        with open(products_file, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                product_map[int(row["product_id"])] = row["sku"]
        with open(inventory_file, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            conn.executemany(
                """
                INSERT INTO inventory(inventory_id, product_id, sku, warehouse, on_hand, reserved, updated_at, low_stock_threshold)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(row["inventory_id"]),
                        int(row["product_id"]),
                        product_map[int(row["product_id"])],
                        row["warehouse"],
                        int(row["on_hand"]),
                        int(row["reserved"]),
                        row["updated_at"],
                        LOW_STOCK_THRESHOLD,
                    )
                    for row in reader
                ],
            )


def parse_ts(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def get_available_locations(conn: sqlite3.Connection, sku: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT *,(on_hand - reserved) AS available FROM inventory WHERE sku = ? ORDER BY available DESC, warehouse ASC",
        (sku,),
    ).fetchall()
    return rows_to_dicts(rows)


def allocate_item(conn: sqlite3.Connection, sku: str, quantity: int) -> list[dict[str, Any]]:
    locations = get_available_locations(conn, sku)
    if not locations:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "message": f"Inventory not found for {sku}"})
    single_warehouse = next((row for row in locations if row["available"] >= quantity), None)
    if single_warehouse:
        return [{"product_id": single_warehouse["product_id"], "sku": sku, "warehouse": single_warehouse["warehouse"], "quantity": quantity}]
    allocations: list[dict[str, Any]] = []
    remaining = quantity
    for row in locations:
        if row["available"] <= 0:
            continue
        take = min(remaining, row["available"])
        allocations.append({"product_id": row["product_id"], "sku": sku, "warehouse": row["warehouse"], "quantity": take})
        remaining -= take
        if remaining == 0:
            break
    if remaining > 0:
        metrics.stockouts_total.inc()
        raise HTTPException(status_code=409, detail={"code": "INSUFFICIENT_STOCK", "message": f"Insufficient stock for {sku}"})
    return allocations


def create_or_get_reservation(payload: ReserveRequest) -> dict[str, Any]:
    started = time.perf_counter()
    with get_db(DB_PATH) as conn:
        with transaction(conn):
            existing = conn.execute(
                "SELECT * FROM reservations WHERE idempotency_key = ?",
                (payload.idempotency_key,),
            ).fetchone()
            if existing:
                reservation_id = existing["reservation_id"]
                items = conn.execute(
                    "SELECT product_id, sku, warehouse, quantity FROM reservation_allocations WHERE reservation_id = ? ORDER BY allocation_id",
                    (reservation_id,),
                ).fetchall()
                return {
                    "reservation_key": existing["reservation_key"],
                    "order_id": existing["order_id"],
                    "status": existing["status"],
                    "expires_at": existing["expires_at"],
                    "allocations": rows_to_dicts(items),
                    "idempotent_replay": True,
                }
            allocations: list[dict[str, Any]] = []
            for item in payload.items:
                allocations.extend(allocate_item(conn, item.sku, item.quantity))
            for allocation in allocations:
                updated = conn.execute(
                    """
                    UPDATE inventory
                    SET reserved = reserved + ?, updated_at = ?
                    WHERE product_id = ? AND warehouse = ? AND (on_hand - reserved) >= ?
                    """,
                    (
                        allocation["quantity"],
                        utc_now_iso(),
                        allocation["product_id"],
                        allocation["warehouse"],
                        allocation["quantity"],
                    ),
                ).rowcount
                if updated != 1:
                    raise HTTPException(status_code=409, detail={"code": "RESERVATION_CONFLICT", "message": f"Unable to reserve {allocation['sku']}"})
                conn.execute(
                    """
                    INSERT INTO inventory_movements(product_id, sku, warehouse, order_id, type, quantity, created_at)
                    VALUES(?, ?, ?, ?, 'RESERVE', ?, ?)
                    """,
                    (
                        allocation["product_id"],
                        allocation["sku"],
                        allocation["warehouse"],
                        payload.order_id,
                        allocation["quantity"],
                        utc_now_iso(),
                    ),
                )
                current = conn.execute(
                    "SELECT on_hand, reserved, low_stock_threshold FROM inventory WHERE product_id = ? AND warehouse = ?",
                    (allocation["product_id"], allocation["warehouse"]),
                ).fetchone()
                if current and (current["on_hand"] - current["reserved"]) < current["low_stock_threshold"]:
                    logger.warning(
                        "Low stock alert",
                        extra={
                            "service_name": SERVICE_NAME,
                            "extra_data": {
                                "sku": allocation["sku"],
                                "warehouse": allocation["warehouse"],
                                "available": current["on_hand"] - current["reserved"],
                            },
                        },
                    )
            reservation_key = f"rsv_{secrets.token_hex(8)}"
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=payload.ttl_minutes)).replace(microsecond=0).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO reservations(reservation_key, order_id, idempotency_key, status, expires_at, created_at)
                VALUES(?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (reservation_key, payload.order_id, payload.idempotency_key, expires_at, utc_now_iso()),
            )
            reservation_id = cursor.lastrowid
            conn.executemany(
                """
                INSERT INTO reservation_allocations(reservation_id, product_id, sku, warehouse, quantity)
                VALUES(?, ?, ?, ?, ?)
                """,
                [
                    (reservation_id, allocation["product_id"], allocation["sku"], allocation["warehouse"], allocation["quantity"])
                    for allocation in allocations
                ],
            )
            metrics.inventory_reserve_latency_ms.observe((time.perf_counter() - started) * 1000)
            return {
                "reservation_key": reservation_key,
                "order_id": payload.order_id,
                "status": "ACTIVE",
                "expires_at": expires_at,
                "allocations": allocations,
                "idempotent_replay": False,
            }


def release_reservation(*, reservation_key: Optional[str], order_id: Optional[int], reason: str) -> dict[str, Any]:
    with get_db(DB_PATH) as conn:
        with transaction(conn):
            if reservation_key:
                reservation = conn.execute("SELECT * FROM reservations WHERE reservation_key = ?", (reservation_key,)).fetchone()
            elif order_id is not None:
                reservation = conn.execute(
                    "SELECT * FROM reservations WHERE order_id = ? AND status = 'ACTIVE' ORDER BY reservation_id DESC LIMIT 1",
                    (order_id,),
                ).fetchone()
            else:
                raise HTTPException(status_code=400, detail={"code": "INVALID_RELEASE", "message": "Reservation key or order id is required"})
            if not reservation:
                raise HTTPException(status_code=404, detail={"code": "RESERVATION_NOT_FOUND", "message": "Reservation not found"})
            if reservation["status"] != "ACTIVE":
                return {"reservation_key": reservation["reservation_key"], "status": reservation["status"], "released": False}
            allocations = conn.execute(
                "SELECT * FROM reservation_allocations WHERE reservation_id = ?",
                (reservation["reservation_id"],),
            ).fetchall()
            for allocation in allocations:
                conn.execute(
                    "UPDATE inventory SET reserved = reserved - ?, updated_at = ? WHERE product_id = ? AND warehouse = ?",
                    (allocation["quantity"], utc_now_iso(), allocation["product_id"], allocation["warehouse"]),
                )
                conn.execute(
                    """
                    INSERT INTO inventory_movements(product_id, sku, warehouse, order_id, type, quantity, created_at)
                    VALUES(?, ?, ?, ?, 'RELEASE', ?, ?)
                    """,
                    (
                        allocation["product_id"],
                        allocation["sku"],
                        allocation["warehouse"],
                        reservation["order_id"],
                        allocation["quantity"],
                        utc_now_iso(),
                    ),
                )
            conn.execute(
                "UPDATE reservations SET status = ?, confirmed_at = COALESCE(confirmed_at, ?) WHERE reservation_id = ?",
                (reason, utc_now_iso(), reservation["reservation_id"]),
            )
            return {
                "reservation_key": reservation["reservation_key"],
                "status": reason,
                "released": True,
                "allocations": rows_to_dicts(allocations),
            }


def ship_reservation(payload: ShipRequest) -> dict[str, Any]:
    with get_db(DB_PATH) as conn:
        with transaction(conn):
            reservation = conn.execute(
                "SELECT * FROM reservations WHERE reservation_key = ?",
                (payload.reservation_key,),
            ).fetchone()
            if not reservation:
                raise HTTPException(status_code=404, detail={"code": "RESERVATION_NOT_FOUND", "message": "Reservation not found"})
            allocations = conn.execute(
                "SELECT * FROM reservation_allocations WHERE reservation_id = ?",
                (reservation["reservation_id"],),
            ).fetchall()
            for allocation in allocations:
                conn.execute(
                    """
                    UPDATE inventory
                    SET on_hand = on_hand - ?, reserved = reserved - ?, updated_at = ?
                    WHERE product_id = ? AND warehouse = ? AND reserved >= ? AND on_hand >= ?
                    """,
                    (
                        allocation["quantity"],
                        allocation["quantity"],
                        utc_now_iso(),
                        allocation["product_id"],
                        allocation["warehouse"],
                        allocation["quantity"],
                        allocation["quantity"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO inventory_movements(product_id, sku, warehouse, order_id, type, quantity, created_at)
                    VALUES(?, ?, ?, ?, 'SHIP', ?, ?)
                    """,
                    (
                        allocation["product_id"],
                        allocation["sku"],
                        allocation["warehouse"],
                        payload.order_id,
                        allocation["quantity"],
                        utc_now_iso(),
                    ),
                )
            conn.execute(
                "UPDATE reservations SET status = 'SHIPPED', confirmed_at = COALESCE(confirmed_at, ?) WHERE reservation_id = ?",
                (utc_now_iso(), reservation["reservation_id"]),
            )
            return {
                "reservation_key": reservation["reservation_key"],
                "status": "SHIPPED",
                "allocations": rows_to_dicts(allocations),
            }


async def release_expired_reservations() -> None:
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT reservation_key FROM reservations WHERE status = 'ACTIVE' AND expires_at < ?",
            (utc_now_iso(),),
        ).fetchall()
    for row in rows:
        try:
            release_reservation(reservation_key=row["reservation_key"], order_id=None, reason="EXPIRED")
        except Exception:
            pass


@app.on_event("startup")
async def startup_event() -> None:
    global reaper_task
    init_db()
    async def reaper_loop():
        while True:
            await release_expired_reservations()
            await asyncio.sleep(REAPER_INTERVAL_SECONDS)
    reaper_task = asyncio.create_task(reaper_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global reaper_task
    if reaper_task:
        reaper_task.cancel()


@app.get("/v1/inventory")
def list_inventory(
    sku: Optional[str] = None,
    warehouse: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    offset = (page - 1) * page_size
    query = "SELECT *, (on_hand - reserved) AS available FROM inventory WHERE 1=1"
    params: list[Any] = []
    if sku:
        query += " AND sku = ?"
        params.append(sku)
    if warehouse:
        query += " AND warehouse = ?"
        params.append(warehouse)
    with get_db(DB_PATH) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM ({query})", params).fetchone()["count"]
        rows = conn.execute(query + " ORDER BY sku, warehouse LIMIT ? OFFSET ?", [*params, page_size, offset]).fetchall()
    return {"page": page, "pageSize": page_size, "total": total, "items": rows_to_dicts(rows)}


@app.get("/v1/inventory/{sku}/availability")
def get_availability(sku: str):
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT warehouse, on_hand, reserved, (on_hand - reserved) AS available FROM inventory WHERE sku = ? ORDER BY warehouse",
            (sku,),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "SKU_NOT_FOUND", "message": "Inventory not found"})
    return {"sku": sku, "warehouses": rows_to_dicts(rows)}


@app.post("/v1/inventory/reserve", status_code=201)
def reserve_inventory(payload: ReserveRequest = Body(...), request: Request = None):
    correlation_id = getattr(request.state, "correlation_id", None) if request else None
    result = create_or_get_reservation(payload)
    logger.info(
        "Inventory reserved",
        extra={
            "service_name": SERVICE_NAME,
            "correlation_id": correlation_id,
            "extra_data": result,
        },
    )
    return result


@app.post("/v1/inventory/release")
def release_inventory(payload: ReleaseRequest = Body(...)):
    return release_reservation(reservation_key=payload.reservation_key, order_id=payload.order_id, reason=payload.reason)


@app.post("/v1/inventory/ship")
def ship_inventory(payload: ShipRequest = Body(...)):
    return ship_reservation(payload)


@app.get("/v1/reservations")
def list_reservations(
    status: Optional[str] = None,
    order_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    offset = (page - 1) * page_size
    query = "SELECT * FROM reservations WHERE 1=1"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if order_id is not None:
        query += " AND order_id = ?"
        params.append(order_id)
    with get_db(DB_PATH) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM ({query})", params).fetchone()["count"]
        rows = conn.execute(query + " ORDER BY reservation_id DESC LIMIT ? OFFSET ?", [*params, page_size, offset]).fetchall()
    return {"page": page, "pageSize": page_size, "total": total, "items": rows_to_dicts(rows)}


@app.get("/v1/movements")
def list_movements(
    sku: Optional[str] = None,
    order_id: Optional[int] = None,
    movement_type: Optional[str] = Query(default=None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    offset = (page - 1) * page_size
    query = "SELECT * FROM inventory_movements WHERE 1=1"
    params: list[Any] = []
    if sku:
        query += " AND sku = ?"
        params.append(sku)
    if order_id is not None:
        query += " AND order_id = ?"
        params.append(order_id)
    if movement_type:
        query += " AND type = ?"
        params.append(movement_type)
    with get_db(DB_PATH) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM ({query})", params).fetchone()["count"]
        rows = conn.execute(query + " ORDER BY movement_id DESC LIMIT ? OFFSET ?", [*params, page_size, offset]).fetchall()
    return {"page": page, "pageSize": page_size, "total": total, "items": rows_to_dicts(rows)}
