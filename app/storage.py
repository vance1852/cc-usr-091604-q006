"""SQLite 持久化层。

所有状态落库,服务重启后占用关系、支付记录、确认单版本与
审计日志不丢失。唯一约束(payment_id、booking_id+version)
是幂等性的最后一道防线:重复支付回调、重复确认不会产生
第二条记录。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app import models as m


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    venue_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    sports TEXT NOT NULL,
    shares TEXT NOT NULL,
    buffer_minutes INTEGER NOT NULL,
    price_per_hour REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_windows (
    window_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    zone_id TEXT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_mow INTEGER,
    duration_minutes INTEGER,
    start_ts TEXT,
    end_ts TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closures (
    closure_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    zone_ids TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    ended_by TEXT,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    customer TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    status TEXT NOT NULL,
    quoted_price REAL NOT NULL,
    buffer_minutes INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    amount REAL NOT NULL,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmations (
    booking_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    operator TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (booking_id, version)
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount REAL NOT NULL,
    basis TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Storage:
    """实体与数据库行之间的薄映射,不含业务规则。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ---- 事务 ----
    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    def close(self) -> None:
        self._conn.close()

    # ---- 场地与分区 ----
    def save_venue(self, v: m.Venue) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO venues(venue_id, name, zone) VALUES (?,?,?)",
            (v.venue_id, v.name, v.zone),
        )

    def get_venue(self, venue_id: str) -> m.Venue | None:
        row = self._conn.execute(
            "SELECT * FROM venues WHERE venue_id=?", (venue_id,)
        ).fetchone()
        if row is None:
            return None
        return m.Venue(name=row["name"], zone=row["zone"], venue_id=row["venue_id"])

    def list_venues(self) -> list[m.Venue]:
        rows = self._conn.execute("SELECT * FROM venues ORDER BY venue_id").fetchall()
        return [m.Venue(name=r["name"], zone=r["zone"], venue_id=r["venue_id"]) for r in rows]

    def save_zone(self, z: m.Zone) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO zones"
            "(zone_id, venue_id, name, capacity, sports, shares, buffer_minutes, price_per_hour)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                z.zone_id, z.venue_id, z.name, z.capacity,
                json.dumps(list(z.sports), ensure_ascii=False),
                json.dumps(list(z.shares), ensure_ascii=False),
                z.buffer_minutes, z.price_per_hour,
            ),
        )

    def _to_zone(self, r: sqlite3.Row) -> m.Zone:
        return m.Zone(
            venue_id=r["venue_id"], zone_id=r["zone_id"], name=r["name"],
            capacity=r["capacity"], sports=tuple(json.loads(r["sports"])),
            shares=tuple(json.loads(r["shares"])),
            buffer_minutes=r["buffer_minutes"], price_per_hour=r["price_per_hour"],
        )

    def get_zone(self, zone_id: str) -> m.Zone | None:
        row = self._conn.execute("SELECT * FROM zones WHERE zone_id=?", (zone_id,)).fetchone()
        return self._to_zone(row) if row else None

    def list_zones(self, venue_id: str | None = None) -> list[m.Zone]:
        if venue_id is None:
            rows = self._conn.execute("SELECT * FROM zones ORDER BY zone_id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM zones WHERE venue_id=? ORDER BY zone_id", (venue_id,)
            ).fetchall()
        return [self._to_zone(r) for r in rows]

    # ---- 维护窗口 ----
    def save_window(self, w: m.MaintenanceWindow) -> None:
        self._conn.execute(
            "INSERT INTO maintenance_windows"
            "(window_id, venue_id, zone_id, label, kind, start_mow, duration_minutes,"
            " start_ts, end_ts, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                w.window_id, w.venue_id, w.zone_id, w.label, w.kind,
                w.start_mow, w.duration_minutes,
                _iso(w.start) if w.start else None,
                _iso(w.end) if w.end else None,
                w.created_by, _iso(w.created_at),
            ),
        )

    def _to_window(self, r: sqlite3.Row) -> m.MaintenanceWindow:
        return m.MaintenanceWindow(
            window_id=r["window_id"], venue_id=r["venue_id"], zone_id=r["zone_id"],
            label=r["label"], kind=r["kind"], start_mow=r["start_mow"],
            duration_minutes=r["duration_minutes"],
            start=_dt(r["start_ts"]) if r["start_ts"] else None,
            end=_dt(r["end_ts"]) if r["end_ts"] else None,
            created_by=r["created_by"], created_at=_dt(r["created_at"]),
        )

    def list_windows(self, venue_id: str) -> list[m.MaintenanceWindow]:
        rows = self._conn.execute(
            "SELECT * FROM maintenance_windows WHERE venue_id=? ORDER BY window_id",
            (venue_id,),
        ).fetchall()
        return [self._to_window(r) for r in rows]

    # ---- 临时封闭 ----
    def save_closure(self, c: m.Closure) -> None:
        self._conn.execute(
            "INSERT INTO closures"
            "(closure_id, venue_id, zone_ids, start_ts, end_ts, reason, status,"
            " published_by, published_at, ended_by, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                c.closure_id, c.venue_id, json.dumps(list(c.zone_ids), ensure_ascii=False),
                _iso(c.start), _iso(c.end), c.reason, c.status.value,
                c.published_by, _iso(c.published_at),
                c.ended_by, _iso(c.ended_at) if c.ended_at else None,
            ),
        )

    def finish_closure(self, closure_id: str, ended_by: str, ended_at: datetime) -> None:
        self._conn.execute(
            "UPDATE closures SET status=?, ended_by=?, ended_at=? WHERE closure_id=?",
            (m.ClosureStatus.ENDED_EARLY.value, ended_by, _iso(ended_at), closure_id),
        )

    def _to_closure(self, r: sqlite3.Row) -> m.Closure:
        return m.Closure(
            closure_id=r["closure_id"], venue_id=r["venue_id"],
            zone_ids=tuple(json.loads(r["zone_ids"])),
            start=_dt(r["start_ts"]), end=_dt(r["end_ts"]), reason=r["reason"],
            status=m.ClosureStatus(r["status"]),
            published_by=r["published_by"], published_at=_dt(r["published_at"]),
            ended_by=r["ended_by"],
            ended_at=_dt(r["ended_at"]) if r["ended_at"] else None,
        )

    def get_closure(self, closure_id: str) -> m.Closure | None:
        row = self._conn.execute(
            "SELECT * FROM closures WHERE closure_id=?", (closure_id,)
        ).fetchone()
        return self._to_closure(row) if row else None

    def list_closures(self, venue_id: str) -> list[m.Closure]:
        rows = self._conn.execute(
            "SELECT * FROM closures WHERE venue_id=? ORDER BY start_ts", (venue_id,)
        ).fetchall()
        return [self._to_closure(r) for r in rows]

    # ---- 预约 ----
    def save_booking(self, b: m.Booking) -> None:
        self._conn.execute(
            "INSERT INTO bookings"
            "(booking_id, venue_id, zone_id, sport, start_ts, end_ts, customer, party_size,"
            " status, quoted_price, buffer_minutes, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                b.booking_id, b.venue_id, b.zone_id, b.sport, _iso(b.start), _iso(b.end),
                b.customer, b.party_size, b.status.value, b.quoted_price,
                b.buffer_minutes, b.created_by, _iso(b.created_at),
            ),
        )

    def update_booking_status(self, booking_id: str, status: m.BookingStatus) -> None:
        self._conn.execute(
            "UPDATE bookings SET status=? WHERE booking_id=?", (status.value, booking_id)
        )

    def update_booking_times(self, booking_id: str, start: datetime, end: datetime) -> None:
        self._conn.execute(
            "UPDATE bookings SET start_ts=?, end_ts=? WHERE booking_id=?",
            (_iso(start), _iso(end), booking_id),
        )

    def _to_booking(self, r: sqlite3.Row) -> m.Booking:
        return m.Booking(
            booking_id=r["booking_id"], venue_id=r["venue_id"], zone_id=r["zone_id"],
            sport=r["sport"], start=_dt(r["start_ts"]), end=_dt(r["end_ts"]),
            customer=r["customer"], party_size=r["party_size"],
            status=m.BookingStatus(r["status"]), quoted_price=r["quoted_price"],
            buffer_minutes=r["buffer_minutes"], created_by=r["created_by"],
            created_at=_dt(r["created_at"]),
        )

    def get_booking(self, booking_id: str) -> m.Booking | None:
        row = self._conn.execute(
            "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
        ).fetchone()
        return self._to_booking(row) if row else None

    def list_bookings(
        self,
        venue_id: str | None = None,
        statuses: tuple | list | None = None,
    ) -> list[m.Booking]:
        sql = "SELECT * FROM bookings WHERE 1=1"
        args: list = []
        if venue_id is not None:
            sql += " AND venue_id=?"
            args.append(venue_id)
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({marks})"
            args.extend(getattr(s, "value", s) for s in statuses)
        sql += " ORDER BY start_ts"
        rows = self._conn.execute(sql, args).fetchall()
        return [self._to_booking(r) for r in rows]

    # ---- 支付 ----
    def save_payment(self, p: m.Payment) -> None:
        self._conn.execute(
            "INSERT INTO payments(payment_id, booking_id, amount, paid_at) VALUES (?,?,?,?)",
            (p.payment_id, p.booking_id, p.amount, _iso(p.paid_at)),
        )

    def _to_payment(self, r: sqlite3.Row) -> m.Payment:
        return m.Payment(
            payment_id=r["payment_id"], booking_id=r["booking_id"],
            amount=r["amount"], paid_at=_dt(r["paid_at"]),
        )

    def payment_by_id(self, payment_id: str) -> m.Payment | None:
        row = self._conn.execute(
            "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
        ).fetchone()
        return self._to_payment(row) if row else None

    def payment_for_booking(self, booking_id: str) -> m.Payment | None:
        row = self._conn.execute(
            "SELECT * FROM payments WHERE booking_id=? ORDER BY paid_at LIMIT 1", (booking_id,)
        ).fetchone()
        return self._to_payment(row) if row else None

    # ---- 确认单 ----
    def save_confirmation(self, c: m.Confirmation) -> None:
        self._conn.execute(
            "INSERT INTO confirmations(booking_id, version, operator, note, created_at)"
            " VALUES (?,?,?,?,?)",
            (c.booking_id, c.version, c.operator, c.note, _iso(c.created_at)),
        )

    def _to_confirmation(self, r: sqlite3.Row) -> m.Confirmation:
        return m.Confirmation(
            booking_id=r["booking_id"], version=r["version"], operator=r["operator"],
            note=r["note"], created_at=_dt(r["created_at"]),
        )

    def latest_confirmation(self, booking_id: str) -> m.Confirmation | None:
        row = self._conn.execute(
            "SELECT * FROM confirmations WHERE booking_id=? ORDER BY version DESC LIMIT 1",
            (booking_id,),
        ).fetchone()
        return self._to_confirmation(row) if row else None

    def confirmations_for(self, booking_id: str) -> list[m.Confirmation]:
        rows = self._conn.execute(
            "SELECT * FROM confirmations WHERE booking_id=? ORDER BY version", (booking_id,)
        ).fetchall()
        return [self._to_confirmation(r) for r in rows]

    # ---- 退款 ----
    def save_refund(self, r: m.Refund) -> None:
        self._conn.execute(
            "INSERT INTO refunds(refund_id, booking_id, payment_id, amount, basis, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (r.refund_id, r.booking_id, r.payment_id, r.amount, r.basis, _iso(r.created_at)),
        )

    def refunds_for(self, booking_id: str) -> list[m.Refund]:
        rows = self._conn.execute(
            "SELECT * FROM refunds WHERE booking_id=? ORDER BY created_at", (booking_id,)
        ).fetchall()
        return [
            m.Refund(
                refund_id=r["refund_id"], booking_id=r["booking_id"],
                payment_id=r["payment_id"], amount=r["amount"], basis=r["basis"],
                created_at=_dt(r["created_at"]),
            )
            for r in rows
        ]

    # ---- 审计 ----
    def save_audit(self, e: m.AuditEvent) -> None:
        self._conn.execute(
            "INSERT INTO audit_events"
            "(event_id, ts, actor, action, entity_type, entity_id, venue_id, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                e.event_id, _iso(e.ts), e.actor, e.action, e.entity_type,
                e.entity_id, e.venue_id, json.dumps(e.detail, ensure_ascii=False),
            ),
        )

    def list_audit(
        self,
        venue_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        entity_id: str | None = None,
    ) -> list[m.AuditEvent]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        args: list = []
        if venue_id is not None:
            sql += " AND venue_id=?"
            args.append(venue_id)
        if entity_id is not None:
            sql += " AND entity_id=?"
            args.append(entity_id)
        if start is not None:
            sql += " AND ts>=?"
            args.append(_iso(start))
        if end is not None:
            sql += " AND ts<?"
            args.append(_iso(end))
        sql += " ORDER BY ts, event_id"
        rows = self._conn.execute(sql, args).fetchall()
        return [
            m.AuditEvent(
                event_id=r["event_id"], ts=_dt(r["ts"]), actor=r["actor"],
                action=r["action"], entity_type=r["entity_type"],
                entity_id=r["entity_id"], venue_id=r["venue_id"],
                detail=json.loads(r["detail"]),
            )
            for r in rows
        ]
