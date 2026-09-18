"""场地资源服务:登记、封闭、预约、审核、改期、取消与审计。

并发与幂等设计:
- 所有写操作在 threading.RLock + BEGIN IMMEDIATE 事务内串行执行,
  并发预约同一时段时只有一个能确认成功,不会产生双重占用。
- 支付按 payment_id 幂等,确认按 (booking_id, version) 幂等,
  重复回调/重复确认返回原记录;状态全部落 SQLite,重启不丢。
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone

from app import models as m
from app.storage import Storage

MINUTES_PER_WEEK = 7 * 24 * 60


class VenueError(Exception):
    """服务基础异常。"""


class NotFoundError(VenueError):
    """实体不存在。"""


class StateError(VenueError):
    """当前状态不允许该操作。"""


class ConflictError(VenueError):
    """时段/资源冲突,携带可用性报告供冲突解释。"""

    def __init__(self, report: m.AvailabilityReport):
        self.report = report
        super().__init__("; ".join(report.reasons()) or "资源冲突")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class VenueService:
    """场地资源服务入口。"""

    def __init__(self, db_path: str = ":memory:", now=None):
        self._storage = Storage(db_path)
        self._lock = threading.RLock()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self._storage.close()

    # ---- 基础 ----
    def health(self) -> dict[str, str]:
        return {"service": "venue", "status": "ok"}

    @contextmanager
    def _tx(self):
        with self._lock:
            self._storage.begin()
            try:
                yield
            except Exception:
                self._storage.rollback()
                raise
            else:
                self._storage.commit()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _audit(self, actor, action, entity_type, entity_id, venue_id, detail=None) -> m.AuditEvent:
        event = m.AuditEvent(
            event_id=self._new_id("evt"), ts=_utc(self._now()), actor=actor,
            action=action, entity_type=entity_type, entity_id=entity_id,
            venue_id=venue_id, detail=detail or {},
        )
        self._storage.save_audit(event)
        return event

    # ---- 登记:场地 / 分区 / 维护窗口 ----
    def register_venue(self, name: str, zone: str = "", venue_id: str | None = None,
                       actor: str = "system") -> m.Venue:
        with self._tx():
            venue = m.Venue(name=name, zone=zone, venue_id=venue_id or name)
            self._storage.save_venue(venue)
            self._audit(actor, "venue.registered", "venue", venue.venue_id, venue.venue_id,
                        {"name": name, "zone": zone})
            return venue

    def register_zone(self, venue_id: str, name: str, capacity: int, sports,
                      shares=None, zone_id: str | None = None, buffer_minutes: int = 15,
                      price_per_hour: float = 0.0, actor: str = "system") -> m.Zone:
        with self._tx():
            self._require_venue(venue_id)
            zid = zone_id or f"{venue_id}:{name}"
            zone = m.Zone(
                venue_id=venue_id, zone_id=zid, name=name, capacity=capacity,
                sports=tuple(sports), shares=tuple(shares) if shares else (zid,),
                buffer_minutes=buffer_minutes, price_per_hour=price_per_hour,
            )
            self._storage.save_zone(zone)
            self._audit(actor, "zone.registered", "zone", zid, venue_id,
                        {"capacity": capacity, "sports": list(zone.sports),
                         "shares": list(zone.shares), "buffer_minutes": buffer_minutes})
            return zone

    def register_maintenance_window(self, venue_id: str, label: str, *, zone_id=None,
                                    weekday: int | None = None, start_time: str | None = None,
                                    duration: timedelta | None = None,
                                    start: datetime | None = None, end: datetime | None = None,
                                    actor: str = "system") -> m.MaintenanceWindow:
        """登记维护窗口。

        周期窗口:weekday(0=周一)+ start_time("HH:MM")+ duration,
        时长可跨午夜、跨周界(如周日 22:00 养护 4 小时至周一 02:00)。
        一次性窗口:传 start/end 绝对时间。
        """
        with self._tx():
            self._require_venue(venue_id)
            if zone_id is not None:
                self._require_zone(venue_id, zone_id)
            if weekday is not None:
                if start_time is None or duration is None:
                    raise StateError("周期维护窗口需要 weekday、start_time 与 duration")
                hh, mm = (int(x) for x in start_time.split(":"))
                start_mow = weekday * 1440 + hh * 60 + mm
                dur_min = int(duration.total_seconds() // 60)
                if not 0 <= start_mow < MINUTES_PER_WEEK:
                    raise StateError("start_time/weekday 超出一周范围")
                if not 0 < dur_min <= MINUTES_PER_WEEK:
                    raise StateError("维护窗口时长需在 0~7 天之间")
                window = m.MaintenanceWindow(
                    window_id=self._new_id("mw"), venue_id=venue_id, zone_id=zone_id,
                    label=label, kind="weekly", start_mow=start_mow,
                    duration_minutes=dur_min, start=None, end=None,
                    created_by=actor, created_at=_utc(self._now()),
                )
            else:
                if start is None or end is None:
                    raise StateError("一次性维护窗口需要 start 与 end")
                start, end = _utc(start), _utc(end)
                if end <= start:
                    raise StateError("维护窗口结束时间必须晚于开始时间")
                window = m.MaintenanceWindow(
                    window_id=self._new_id("mw"), venue_id=venue_id, zone_id=zone_id,
                    label=label, kind="once", start_mow=None, duration_minutes=None,
                    start=start, end=end,
                    created_by=actor, created_at=_utc(self._now()),
                )
            self._storage.save_window(window)
            self._audit(actor, "maintenance.registered", "maintenance_window",
                        window.window_id, venue_id,
                        {"label": label, "kind": window.kind, "zone_id": zone_id})
            return window

    # ---- 临时封闭 ----
    def publish_closure(self, venue_id: str, zone_ids, start: datetime, end: datetime,
                        reason: str, actor: str) -> m.Closure:
        """维护人员发布封闭。zone_ids 为空表示整场封闭。

        封闭期间不得新建预约;与封闭重叠的已支付/已确认预约
        被标记为 AFFECTED,只能改期或按规则取消。
        """
        with self._tx():
            self._require_venue(venue_id)
            start, end = _utc(start), _utc(end)
            if end <= start:
                raise StateError("封闭结束时间必须晚于开始时间")
            closure = m.Closure(
                closure_id=self._new_id("cl"), venue_id=venue_id,
                zone_ids=tuple(zone_ids or ()), start=start, end=end, reason=reason,
                status=m.ClosureStatus.ACTIVE, published_by=actor,
                published_at=_utc(self._now()),
            )
            self._storage.save_closure(closure)
            affected = self._mark_affected(closure)
            self._audit(actor, "closure.published", "closure", closure.closure_id, venue_id,
                        {"reason": reason, "zone_ids": list(closure.zone_ids),
                         "slot": [start.isoformat(), end.isoformat()],
                         "affected_bookings": affected})
            return closure

    def end_closure(self, closure_id: str, actor: str,
                    ended_at: datetime | None = None) -> m.Closure:
        """维护人员提前结束封闭;不再受影响的预约自动恢复为已确认。"""
        with self._tx():
            closure = self._require_closure(closure_id)
            if closure.status != m.ClosureStatus.ACTIVE:
                raise StateError("封闭已结束,不能重复结束")
            ended = _utc(ended_at or self._now())
            self._storage.finish_closure(closure_id, actor, ended)
            self._audit(actor, "closure.ended", "closure", closure_id, closure.venue_id,
                        {"ended_at": ended.isoformat()})
            self._restore_unaffected(closure.venue_id)
            return self._require_closure(closure_id)

    def _mark_affected(self, closure: m.Closure) -> list[str]:
        zones = self._zones_of(closure.venue_id)
        blocked = self._blocked_shares(closure.zone_ids, zones)
        affected = []
        for b in self._storage.list_bookings(
            closure.venue_id, (m.BookingStatus.PAID, m.BookingStatus.CONFIRMED)
        ):
            shares = self._zone_shares(zones, b.zone_id)
            if blocked is not None and not (shares & blocked):
                continue
            if _overlap(b.start, b.hold_until, closure.start, closure.effective_end):
                self._storage.update_booking_status(b.booking_id, m.BookingStatus.AFFECTED)
                self._audit("system", "booking.affected", "booking", b.booking_id,
                            closure.venue_id,
                            {"closure_id": closure.closure_id, "reason": closure.reason})
                affected.append(b.booking_id)
        return affected

    def _restore_unaffected(self, venue_id: str) -> list[str]:
        zones = self._zones_of(venue_id)
        restored = []
        for b in self._storage.list_bookings(venue_id, (m.BookingStatus.AFFECTED,)):
            zone = zones.get(b.zone_id)
            if zone is None:
                continue
            report = self._check_availability(venue_id, zone, b.start, b.end,
                                              exclude_booking_id=b.booking_id)
            if report.available:
                # 恢复到受影响前的状态:有确认单的回到已确认,否则回到已支付待审
                target = (m.BookingStatus.CONFIRMED
                          if self._storage.latest_confirmation(b.booking_id)
                          else m.BookingStatus.PAID)
                self._storage.update_booking_status(b.booking_id, target)
                self._audit("system", "booking.restored", "booking", b.booking_id, venue_id,
                            {"restored_to": target.value,
                             "reason": "封闭提前结束,原时段恢复可用"})
                restored.append(b.booking_id)
        return restored

    # ---- 预约流程 ----
    def create_booking(self, venue_id: str, zone_id: str, sport: str,
                       start: datetime, end: datetime, customer: str,
                       party_size: int = 1, actor: str | None = None) -> m.Booking:
        """提交预约申请(待支付)。封闭/维护/占用冲突时拒绝并给出原因。"""
        with self._tx():
            zone = self._require_zone(venue_id, zone_id)
            start, end = _utc(start), _utc(end)
            if end <= start:
                raise StateError("结束时间必须晚于开始时间")
            if party_size < 1:
                raise StateError("人数必须为正数")
            report = self._check_availability(venue_id, zone, start, end)
            self._validate_request(zone, sport, party_size, report)
            if not report.available:
                self._audit(actor or customer, "booking.create_rejected", "booking", "-",
                            venue_id, {"reasons": report.reasons()})
                raise ConflictError(report)
            hours = (end - start).total_seconds() / 3600
            booking = m.Booking(
                booking_id=self._new_id("bk"), venue_id=venue_id, zone_id=zone_id,
                sport=sport, start=start, end=end, customer=customer,
                party_size=party_size, status=m.BookingStatus.PENDING_PAYMENT,
                quoted_price=round(zone.price_per_hour * hours, 2),
                buffer_minutes=zone.buffer_minutes,
                created_by=actor or customer, created_at=_utc(self._now()),
            )
            self._storage.save_booking(booking)
            self._audit(booking.created_by, "booking.created", "booking",
                        booking.booking_id, venue_id,
                        {"slot": [start.isoformat(), end.isoformat()], "sport": sport,
                         "party_size": party_size, "quoted_price": booking.quoted_price})
            return booking

    def pay(self, booking_id: str, payment_id: str, amount: float | None = None,
            paid_at: datetime | None = None) -> m.Payment:
        """支付回调。按 payment_id 幂等:重复回调返回原记录,不重复入账。"""
        with self._tx():
            existing = self._storage.payment_by_id(payment_id)
            if existing is not None:
                if existing.booking_id != booking_id:
                    raise StateError(f"支付单号 {payment_id} 已绑定其他预约")
                return existing
            booking = self._require_booking(booking_id)
            if booking.status != m.BookingStatus.PENDING_PAYMENT:
                raise StateError(f"预约状态 {booking.status.value} 不允许支付")
            if amount is not None and round(amount, 2) != round(booking.quoted_price, 2):
                raise StateError(
                    f"支付金额 {amount} 与订单应付 {booking.quoted_price} 不符")
            zone = self._require_zone(booking.venue_id, booking.zone_id)
            report = self._check_availability(booking.venue_id, zone, booking.start,
                                              booking.end, exclude_booking_id=booking_id)
            if not report.available:
                raise ConflictError(report)
            payment = m.Payment(payment_id=payment_id, booking_id=booking_id,
                                amount=booking.quoted_price,
                                paid_at=_utc(paid_at or self._now()))
            self._storage.save_payment(payment)
            self._storage.update_booking_status(booking_id, m.BookingStatus.PAID)
            self._audit("payment-gateway", "booking.paid", "booking", booking_id,
                        booking.venue_id,
                        {"payment_id": payment_id, "amount": payment.amount})
            return payment

    def review(self, booking_id: str, operator: str, approve: bool = True,
               note: str = ""):
        """运营员审核。通过则生成带版本的确认单;重复确认幂等返回原确认单。"""
        with self._tx():
            booking = self._require_booking(booking_id)
            if not approve:
                if booking.status not in (m.BookingStatus.PENDING_PAYMENT, m.BookingStatus.PAID):
                    raise StateError(f"预约状态 {booking.status.value} 不能审核拒绝")
                self._storage.update_booking_status(booking_id, m.BookingStatus.REJECTED)
                payment = self._storage.payment_for_booking(booking_id)
                if payment is not None:
                    self._issue_refund(booking, payment, payment.amount,
                                       "审核未通过,全额退款")
                self._audit(operator, "booking.rejected", "booking", booking_id,
                            booking.venue_id, {"note": note})
                return self._require_booking(booking_id)
            if booking.status == m.BookingStatus.CONFIRMED:
                return self._storage.latest_confirmation(booking_id)
            if booking.status != m.BookingStatus.PAID:
                raise StateError(f"预约状态 {booking.status.value} 不能确认")
            zone = self._require_zone(booking.venue_id, booking.zone_id)
            report = self._check_availability(booking.venue_id, zone, booking.start,
                                              booking.end, exclude_booking_id=booking_id)
            if not report.available:
                self._audit(operator, "booking.confirm_blocked", "booking", booking_id,
                            booking.venue_id, {"reasons": report.reasons()})
                raise ConflictError(report)
            confirmation = self._confirm(booking, operator, note)
            self._audit(operator, "booking.confirmed", "booking", booking_id,
                        booking.venue_id, {"version": confirmation.version})
            return confirmation

    def reschedule(self, booking_id: str, new_start: datetime, new_end: datetime,
                   actor: str) -> m.Confirmation:
        """改期:仅已确认/受影响预约可改期,生成新版本确认单,原时段释放。"""
        with self._tx():
            booking = self._require_booking(booking_id)
            if booking.status not in (m.BookingStatus.CONFIRMED, m.BookingStatus.AFFECTED):
                raise StateError(f"预约状态 {booking.status.value} 不能改期")
            new_start, new_end = _utc(new_start), _utc(new_end)
            if new_end <= new_start:
                raise StateError("结束时间必须晚于开始时间")
            zone = self._require_zone(booking.venue_id, booking.zone_id)
            report = self._check_availability(booking.venue_id, zone, new_start, new_end,
                                              exclude_booking_id=booking_id)
            if not report.available:
                raise ConflictError(report)
            old_slot = (booking.start, booking.end)
            self._storage.update_booking_times(booking_id, new_start, new_end)
            self._storage.update_booking_status(booking_id, m.BookingStatus.CONFIRMED)
            confirmation = self._confirm(
                booking, actor, note=f"改期自 {old_slot[0].isoformat()}")
            self._audit(actor, "booking.rescheduled", "booking", booking_id,
                        booking.venue_id,
                        {"old_slot": [old_slot[0].isoformat(), old_slot[1].isoformat()],
                         "new_slot": [new_start.isoformat(), new_end.isoformat()],
                         "version": confirmation.version})
            return confirmation

    def cancel(self, booking_id: str, actor: str, reason: str = "customer") -> m.Refund | None:
        """按规则取消并记录退款依据。

        退款规则:封闭/维护导致 → 全额;开场前 24 小时以上 → 全额;
        4~24 小时 → 50%;4 小时内 → 不退。
        """
        with self._tx():
            booking = self._require_booking(booking_id)
            if booking.status not in (m.BookingStatus.PENDING_PAYMENT, m.BookingStatus.PAID,
                                      m.BookingStatus.CONFIRMED, m.BookingStatus.AFFECTED):
                raise StateError(f"预约状态 {booking.status.value} 不能取消")
            refund = None
            basis = "未支付,取消不涉及退款"
            payment = self._storage.payment_for_booking(booking_id)
            if payment is not None:
                if reason == "closure" or booking.status == m.BookingStatus.AFFECTED:
                    ratio, basis = 1.0, "因场地封闭/维护取消,全额退款"
                else:
                    hours = (booking.start - _utc(self._now())).total_seconds() / 3600
                    if hours >= 24:
                        ratio, basis = 1.0, "开场前24小时以上取消,全额退款"
                    elif hours >= 4:
                        ratio, basis = 0.5, "开场前4-24小时取消,退还50%"
                    else:
                        ratio, basis = 0.0, "开场前4小时内取消,不予退款"
                refund = self._issue_refund(booking, payment,
                                            round(payment.amount * ratio, 2), basis)
            self._storage.update_booking_status(booking_id, m.BookingStatus.CANCELLED)
            self._audit(actor, "booking.cancelled", "booking", booking_id, booking.venue_id,
                        {"reason": reason, "basis": basis,
                         "refund": refund.amount if refund else 0.0})
            return refund

    # ---- 查询接口 ----
    def calendar(self, venue_id: str, day: date, tz: timezone = timezone.utc) -> list[m.CalendarEntry]:
        """某日日历:预约、封闭、维护窗口。跨午夜条目按绝对区间匹配,
        在起始日与结束日都会出现。"""
        with self._lock:
            self._require_venue(venue_id)
            t0 = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
            t1 = t0 + timedelta(days=1)
            entries: list[m.CalendarEntry] = []
            for b in self._storage.list_bookings(venue_id, m.OCCUPYING_STATUSES):
                if _overlap(t0, t1, b.start, b.hold_until):
                    entries.append(m.CalendarEntry(
                        kind="booking", ref_id=b.booking_id,
                        label=f"{b.customer}·{b.sport}", venue_id=venue_id,
                        zone_id=b.zone_id, start=b.start, end=b.end,
                        status=b.status.value))
            for c in self._storage.list_closures(venue_id):
                if _overlap(t0, t1, c.start, c.effective_end):
                    scope = "整场" if not c.zone_ids else "/".join(c.zone_ids)
                    entries.append(m.CalendarEntry(
                        kind="closure", ref_id=c.closure_id,
                        label=f"封闭({scope}):{c.reason}", venue_id=venue_id,
                        zone_id=None, start=c.start, end=c.effective_end,
                        status=c.status.value))
            for w in self._storage.list_windows(venue_id):
                for ws, we in self._window_instances(w, t0, t1):
                    entries.append(m.CalendarEntry(
                        kind="maintenance", ref_id=w.window_id, label=w.label,
                        venue_id=venue_id, zone_id=w.zone_id, start=ws, end=we,
                        status="PLANNED"))
            entries.sort(key=lambda e: (e.start, e.kind))
            return entries

    def explain(self, venue_id: str, zone_id: str, sport: str,
                start: datetime, end: datetime, party_size: int = 1) -> m.AvailabilityReport:
        """冲突解释:返回该时段的全部不可用原因。"""
        with self._lock:
            zone = self._require_zone(venue_id, zone_id)
            report = self._check_availability(venue_id, zone, start, end)
            self._validate_request(zone, sport, party_size, report)
            return report

    def suggest_alternatives(self, sport: str, start: datetime, end: datetime,
                             party_size: int = 1, venue_id: str | None = None) -> list[m.Suggestion]:
        """替代场地建议,逐条说明容量与项目兼容性。"""
        with self._lock:
            venue_ids = ([venue_id] if venue_id
                         else [v.venue_id for v in self._storage.list_venues()])
            suggestions: list[m.Suggestion] = []
            for vid in venue_ids:
                for zone in self._storage.list_zones(vid):
                    if sport not in zone.sports or zone.capacity < party_size:
                        continue
                    report = self._check_availability(vid, zone, start, end)
                    if report.available:
                        suggestions.append(m.Suggestion(
                            venue_id=vid, zone_id=zone.zone_id, zone_name=zone.name,
                            capacity=zone.capacity, sports=zone.sports,
                            buffer_minutes=zone.buffer_minutes,
                            reason=(f"容量{zone.capacity}≥需求{party_size};"
                                    f"支持项目「{sport}」;"
                                    f"该时段空闲(含{zone.buffer_minutes}分钟清场缓冲)")))
            return suggestions

    def audit_trail(self, venue_id: str | None = None,
                    start: datetime | None = None,
                    end: datetime | None = None) -> list[m.AuditEvent]:
        """审计接口:某时间段内的全部决策来源。"""
        with self._lock:
            return self._storage.list_audit(
                venue_id=venue_id,
                start=_utc(start) if start else None,
                end=_utc(end) if end else None)

    def booking_history(self, booking_id: str) -> list[m.AuditEvent]:
        """单个预约的完整决策链。"""
        with self._lock:
            return self._storage.list_audit(entity_id=booking_id)

    def get_booking(self, booking_id: str) -> m.Booking:
        with self._lock:
            return self._require_booking(booking_id)

    def get_closure(self, closure_id: str) -> m.Closure:
        with self._lock:
            return self._require_closure(closure_id)

    def list_bookings(self, venue_id: str | None = None, statuses=None) -> list[m.Booking]:
        with self._lock:
            return self._storage.list_bookings(venue_id, statuses)

    def confirmation_of(self, booking_id: str) -> m.Confirmation | None:
        with self._lock:
            return self._storage.latest_confirmation(booking_id)

    # ---- 内部:可用性计算 ----
    def _check_availability(self, venue_id: str, zone: m.Zone, start: datetime,
                            end: datetime, exclude_booking_id: str | None = None
                            ) -> m.AvailabilityReport:
        start, end = _utc(start), _utc(end)
        hold = end + timedelta(minutes=zone.buffer_minutes)
        report = m.AvailabilityReport(venue_id=venue_id, zone_id=zone.zone_id,
                                      start=start, end=end)
        zones = self._zones_of(venue_id)
        my_shares = set(zone.shares)

        for b in self._storage.list_bookings(venue_id, m.OCCUPYING_STATUSES):
            if b.booking_id == exclude_booking_id:
                continue
            if not my_shares & self._zone_shares(zones, b.zone_id):
                continue
            if _overlap(start, hold, b.start, b.hold_until):
                report.conflicts.append(m.ConflictDetail(
                    kind="booking", ref_id=b.booking_id,
                    label=f"{b.customer} 的预约", start=b.start, end=b.hold_until,
                    detail=(f"时段与 {b.customer} 的「{b.sport}」预约冲突"
                            f"(对方状态 {b.status.value},含{zone.buffer_minutes}分钟清场缓冲)")))

        for c in self._storage.list_closures(venue_id):
            if c.status != m.ClosureStatus.ACTIVE:
                continue
            blocked = self._blocked_shares(c.zone_ids, zones)
            if blocked is not None and not (my_shares & blocked):
                continue
            if _overlap(start, hold, c.start, c.effective_end):
                scope = "整场" if not c.zone_ids else "/".join(c.zone_ids)
                report.conflicts.append(m.ConflictDetail(
                    kind="closure", ref_id=c.closure_id, label="临时封闭",
                    start=c.start, end=c.effective_end,
                    detail=f"场地封闭({scope}):{c.reason}"))

        for w in self._storage.list_windows(venue_id):
            for ws, we in self._window_instances(w, start, hold):
                if w.zone_id is not None and not (
                    my_shares & self._zone_shares(zones, w.zone_id)
                ):
                    continue
                report.conflicts.append(m.ConflictDetail(
                    kind="maintenance", ref_id=w.window_id, label=w.label,
                    start=ws, end=we,
                    detail=f"维护窗口「{w.label}」"
                           f" {ws:%m-%d %H:%M}~{we:%m-%d %H:%M} 不可用"))
        return report

    @staticmethod
    def _window_instances(w: m.MaintenanceWindow, t0: datetime, t1: datetime):
        """展开维护窗口在 [t0, t1) 内的具体实例。

        周期窗口按周展开,时长可越过午夜乃至周界(周日深夜到周一凌晨)。
        """
        if w.kind == "once":
            if _overlap(t0, t1, w.start, w.end):
                yield (w.start, w.end)
            return
        anchor = _utc(t0)
        monday = (anchor - timedelta(days=anchor.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        monday -= timedelta(days=7)  # 往前一周,覆盖跨周溢出的实例
        while monday < t1:
            s = monday + timedelta(minutes=w.start_mow)
            e = s + timedelta(minutes=w.duration_minutes)
            if _overlap(t0, t1, s, e):
                yield (s, e)
            monday += timedelta(days=7)

    @staticmethod
    def _validate_request(zone: m.Zone, sport: str, party_size: int,
                          report: m.AvailabilityReport) -> None:
        if sport not in zone.sports:
            report.conflicts.append(m.ConflictDetail(
                kind="sport", ref_id=zone.zone_id, label="项目不兼容",
                start=None, end=None,
                detail=f"分区「{zone.name}」不支持项目「{sport}」,"
                       f"可承载:{'、'.join(zone.sports)}"))
        if party_size > zone.capacity:
            report.conflicts.append(m.ConflictDetail(
                kind="capacity", ref_id=zone.zone_id, label="容量不足",
                start=None, end=None,
                detail=f"人数 {party_size} 超出分区「{zone.name}」容量 {zone.capacity}"))

    def _confirm(self, booking: m.Booking, operator: str, note: str) -> m.Confirmation:
        latest = self._storage.latest_confirmation(booking.booking_id)
        confirmation = m.Confirmation(
            booking_id=booking.booking_id,
            version=(latest.version + 1) if latest else 1,
            operator=operator, created_at=_utc(self._now()), note=note)
        self._storage.save_confirmation(confirmation)
        self._storage.update_booking_status(booking.booking_id, m.BookingStatus.CONFIRMED)
        return confirmation

    def _issue_refund(self, booking: m.Booking, payment: m.Payment,
                      amount: float, basis: str) -> m.Refund:
        refund = m.Refund(refund_id=self._new_id("rf"), booking_id=booking.booking_id,
                          payment_id=payment.payment_id, amount=amount, basis=basis,
                          created_at=_utc(self._now()))
        self._storage.save_refund(refund)
        self._audit("system", "refund.issued", "booking", booking.booking_id,
                    booking.venue_id,
                    {"amount": amount, "basis": basis, "payment_id": payment.payment_id})
        return refund

    # ---- 内部:取数与校验 ----
    def _require_venue(self, venue_id: str) -> m.Venue:
        venue = self._storage.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"场地不存在:{venue_id}")
        return venue

    def _require_zone(self, venue_id: str, zone_id: str) -> m.Zone:
        zone = self._storage.get_zone(zone_id)
        if zone is None or zone.venue_id != venue_id:
            raise NotFoundError(f"分区不存在:{venue_id}/{zone_id}")
        return zone

    def _require_booking(self, booking_id: str) -> m.Booking:
        booking = self._storage.get_booking(booking_id)
        if booking is None:
            raise NotFoundError(f"预约不存在:{booking_id}")
        return booking

    def _require_closure(self, closure_id: str) -> m.Closure:
        closure = self._storage.get_closure(closure_id)
        if closure is None:
            raise NotFoundError(f"封闭不存在:{closure_id}")
        return closure

    def _zones_of(self, venue_id: str) -> dict[str, m.Zone]:
        return {z.zone_id: z for z in self._storage.list_zones(venue_id)}

    @staticmethod
    def _zone_shares(zones: dict[str, m.Zone], zone_id: str) -> set[str]:
        zone = zones.get(zone_id)
        return set(zone.shares) if zone else {zone_id}

    @staticmethod
    def _blocked_shares(zone_ids, zones: dict[str, m.Zone]) -> set[str] | None:
        """封闭分区对应的资源单元集合;None 表示整场封闭。"""
        if not zone_ids:
            return None
        blocked: set[str] = set()
        for zid in zone_ids:
            zone = zones.get(zid)
            blocked.update(zone.shares if zone else (zid,))
        return blocked
