"""场地资源服务。

覆盖体育中心五人制足球场等场地的运营全流程：

* 场地与分区登记：分区之间可形成“整场 ─ 子分区”的层级占用关系；
* 分区承载项目与人数容量；
* 周期性维护窗口（可跨午夜）与运营/维护人员发布的临时封闭；
* 预约申请 → 支付回调 → 运营员审核 → 带版本的确认单 → 改期 / 取消退款；
* 冲突计算：跨午夜时段、缓冲清场时间、同一场地不同分区的占用关系；
* 幂等保证：重复支付回调、重复确认、服务重启都不会产生双重占用；
* 日历、冲突解释、替代场地建议、改期与审计（决策来源）接口。

约定：所有时段使用半开区间 ``[start, end)``；``datetime`` 可带时区，
但同一次比较必须使用同一种（朴素或感知）时间。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Iterable, Optional

SNAPSHOT_VERSION = 1

# 取消退款的时间阶梯（相对活动开始时刻）。
FULL_REFUND_LEAD_HOURS = 48.0
HALF_REFUND_LEAD_HOURS = 24.0


# --------------------------------------------------------------------------- #
# 异常与冲突解释
# --------------------------------------------------------------------------- #


class VenueServiceError(Exception):
    """场地服务领域错误基类。"""


class NotFoundError(VenueServiceError):
    """引用的资源不存在。"""


class BookingStateError(VenueServiceError):
    """预约当前状态不允许该操作。"""


class PaymentMismatchError(VenueServiceError):
    """同一支付单号携带了与首次回调不一致的内容。"""


class IdempotencyConflict(VenueServiceError):
    """同一幂等键携带了与首次请求不一致的参数。"""


@dataclass
class ConflictReason:
    """一条结构化的冲突/不可预约原因，用于“冲突解释”接口。"""

    code: str  # CLOSURE / MAINTENANCE / BOOKING / ACTIVITY_UNSUPPORTED / ...
    message: str
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    zone_id: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    detail: dict = field(default_factory=dict)


class BookingConflictError(VenueServiceError):
    """预约无法成立，携带全部结构化原因。"""

    def __init__(self, reasons: list[ConflictReason]):
        self.reasons = reasons
        super().__init__("；".join(r.message for r in reasons))


# --------------------------------------------------------------------------- #
# 领域对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Venue:
    """一块物理场地，归属某个区域。"""

    name: str
    zone: str


@dataclass
class Zone:
    """场地分区。

    ``parent_id`` 指向包含本分区的上级复合分区（例如“整场”包含 A、B 两个
    半场）。同级子分区互不占用，上级分区与任一下级分区互相占用。
    """

    id: str
    venue_id: str
    name: str
    capacity: int  # 可容纳人数
    activities: frozenset[str]  # 可承载项目
    parent_id: Optional[str] = None
    buffer_minutes: int = 0  # 活动结束后的清场缓冲


class BookingStatus:
    REQUESTED = "REQUESTED"  # 已申请，待支付
    PAID = "PAID"  # 支付回调已到，待运营审核
    CONFIRMED = "CONFIRMED"  # 已审核，持有占用
    CANCELLED = "CANCELLED"  # 已取消，占用释放


@dataclass
class Confirmation:
    """带版本的确认单；每次改期生成新版本。"""

    booking_id: str
    version: int
    zone_id: str
    start: datetime
    end: datetime
    activity: str
    headcount: int
    confirmed_by: str
    confirmed_at: datetime
    revision: str

    def to_dict(self) -> dict:
        return {
            "booking_id": self.booking_id,
            "version": self.version,
            "zone_id": self.zone_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "activity": self.activity,
            "headcount": self.headcount,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at.isoformat(),
            "revision": self.revision,
        }


@dataclass
class RefundRecord:
    """取消时记录的退款依据。"""

    code: str  # CLOSURE_FULL / SELF_FULL / SELF_HALF / SELF_NONE / UNCONFIRMED_FULL
    ratio: float
    amount: float
    basis: str  # 退款依据的文字说明
    caused_by_closure: bool
    closure_id: Optional[str]
    decided_by: str
    decided_at: datetime


@dataclass
class Booking:
    id: str
    request_id: str  # 申请幂等键
    zone_id: str
    activity: str
    headcount: int
    start: datetime
    end: datetime
    organizer: str
    amount: float
    created_at: datetime
    status: str = BookingStatus.REQUESTED
    payment_id: Optional[str] = None
    paid_at: Optional[datetime] = None
    history: list[Confirmation] = field(default_factory=list)
    cancelled_at: Optional[datetime] = None
    cancel_actor: Optional[str] = None
    refund: Optional[RefundRecord] = None

    @property
    def confirmed(self) -> bool:
        return self.status == BookingStatus.CONFIRMED

    def latest_confirmation(self) -> Optional[Confirmation]:
        return self.history[-1] if self.history else None


@dataclass
class Payment:
    payment_id: str  # 支付渠道流水号，天然幂等键
    booking_id: str
    amount: float
    paid_at: datetime


@dataclass
class MaintenanceRule:
    """周期性维护窗口。

    例如每周六 22:00、持续 480 分钟，即跨午夜至周日 06:00。
    """

    id: str
    zone_id: str
    weekday: int  # 0=周一 ... 6=周日
    start_time: time
    duration_minutes: int
    reason: str
    created_by: str
    created_at: datetime
    active: bool = True


@dataclass
class Closure:
    """临时封闭，可由维护人员提前结束。"""

    id: str
    zone_id: str
    start: datetime
    end: datetime
    reason: str
    published_by: str
    published_at: datetime
    ended_by: Optional[str] = None
    ended_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.ended_at is None

    def effective_end(self) -> datetime:
        """提前结束后，实际封闭只到 ended_at。"""
        return self.ended_at or self.end


@dataclass
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    payload: dict


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    return s1 < e2 and s2 < e1


class VenueService:
    """场地资源与预约运营服务。

    传 ``path`` 时每次状态变更都会原子写入快照；用同一路径重新构造服务即
    “重启”，已持久化的幂等键与确认单保证不会双重占用。
    """

    def __init__(
        self,
        path: Optional[str] = None,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.path = path
        self.now = clock
        self._lock = threading.RLock()

        self.venues: dict[str, Venue] = {}
        self.zones: dict[str, Zone] = {}
        self.maintenance: dict[str, MaintenanceRule] = {}
        self.closures: dict[str, Closure] = {}
        self.bookings: dict[str, Booking] = {}
        self.payments: dict[str, Payment] = {}
        self.events: list[AuditEvent] = []
        self._counters: dict[str, int] = {}
        self._request_index: dict[str, str] = {}  # request_id -> booking_id

        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            self._load()

    def health(self) -> dict[str, str]:
        return {"service": "venue", "status": "ok"}

    # ------------------------------------------------------------------ #
    # 登记：场地 / 分区 / 维护窗口
    # ------------------------------------------------------------------ #

    def register_venue(self, venue_id: str, name: str, area: str) -> Venue:
        with self._lock:
            if venue_id in self.venues:
                raise VenueServiceError(f"场地已存在：{venue_id}")
            venue = Venue(name=name, zone=area)
            self.venues[venue_id] = venue
            self._audit("系统", "VENUE_REGISTER", "venue", venue_id, name=name, area=area)
            self._save()
            return venue

    def register_zone(
        self,
        zone_id: str,
        venue_id: str,
        name: str,
        capacity: int,
        activities: Iterable[str],
        parent_id: Optional[str] = None,
        buffer_minutes: int = 0,
    ) -> Zone:
        with self._lock:
            if zone_id in self.zones:
                raise VenueServiceError(f"分区已存在：{zone_id}")
            if venue_id not in self.venues:
                raise NotFoundError(f"场地不存在：{venue_id}")
            if parent_id is not None:
                parent = self.zones.get(parent_id)
                if parent is None:
                    raise NotFoundError(f"上级分区不存在：{parent_id}")
                if parent.venue_id != venue_id:
                    raise VenueServiceError("上级分区必须属于同一场地")
            zone = Zone(
                id=zone_id,
                venue_id=venue_id,
                name=name,
                capacity=capacity,
                activities=frozenset(activities),
                parent_id=parent_id,
                buffer_minutes=buffer_minutes,
            )
            self.zones[zone_id] = zone
            self._audit(
                "系统",
                "ZONE_REGISTER",
                "zone",
                zone_id,
                venue_id=venue_id,
                name=name,
                capacity=capacity,
                activities=sorted(zone.activities),
                parent_id=parent_id,
                buffer_minutes=buffer_minutes,
            )
            self._save()
            return zone

    def register_maintenance(
        self,
        zone_id: str,
        weekday: int,
        start_time: time,
        duration_minutes: int,
        reason: str,
        actor: str,
    ) -> MaintenanceRule:
        """登记周期维护窗口；``duration_minutes`` 可以超过 1440 的余数跨午夜。"""
        with self._lock:
            self._require_zone(zone_id)
            if not 0 <= weekday <= 6:
                raise VenueServiceError("weekday 取值 0（周一）到 6（周日）")
            if duration_minutes <= 0:
                raise VenueServiceError("维护时长必须为正")
            rule = MaintenanceRule(
                id=self._next_id("maint"),
                zone_id=zone_id,
                weekday=weekday,
                start_time=start_time,
                duration_minutes=duration_minutes,
                reason=reason,
                created_by=actor,
                created_at=self.now(),
            )
            self.maintenance[rule.id] = rule
            self._audit(
                actor,
                "MAINTENANCE_REGISTER",
                "maintenance",
                rule.id,
                zone_id=zone_id,
                weekday=weekday,
                start_time=start_time.strftime("%H:%M"),
                duration_minutes=duration_minutes,
                reason=reason,
            )
            self._save()
            return rule

    # ------------------------------------------------------------------ #
    # 临时封闭：发布 / 提前结束
    # ------------------------------------------------------------------ #

    def publish_closure(
        self,
        zone_id: str,
        start: datetime,
        end: datetime,
        reason: str,
        actor: str,
    ) -> dict:
        """发布临时封闭。

        封闭立即生效并阻止之后的新建预约；发布时已存在的付费/确认活动不会
        被悄悄删除，而是在返回值和审计中列明，由运营员走改期或取消退款。
        """
        with self._lock:
            self._require_zone(zone_id)
            if end <= start:
                raise VenueServiceError("封闭结束时间必须晚于开始时间")
            closure = Closure(
                id=self._next_id("closure"),
                zone_id=zone_id,
                start=start,
                end=end,
                reason=reason,
                published_by=actor,
                published_at=self.now(),
            )
            self.closures[closure.id] = closure

            affected_confirmed: list[str] = []
            affected_pending: list[str] = []
            for b in self.bookings.values():
                if b.status == BookingStatus.CANCELLED:
                    continue
                if not self._zone_overlaps_id(zone_id, b.zone_id):
                    continue
                buffered_end = b.end + timedelta(
                    minutes=self.zones[b.zone_id].buffer_minutes
                )
                if not _overlaps(start, end, b.start, buffered_end):
                    continue
                if b.status == BookingStatus.CONFIRMED:
                    affected_confirmed.append(b.id)
                else:
                    affected_pending.append(b.id)

            self._audit(
                actor,
                "CLOSURE_PUBLISH",
                "closure",
                closure.id,
                zone_id=zone_id,
                start=_fmt(start),
                end=_fmt(end),
                reason=reason,
                affected_confirmed=affected_confirmed,
                affected_pending=affected_pending,
            )
            self._save()
            return {
                "closure": closure,
                "affected_confirmed": affected_confirmed,
                "affected_pending": affected_pending,
            }

    def end_closure(
        self, closure_id: str, actor: str, at: Optional[datetime] = None
    ) -> Closure:
        """提前结束封闭；结束时刻之后该分区恢复可约。"""
        with self._lock:
            closure = self.closures.get(closure_id)
            if closure is None:
                raise NotFoundError(f"封闭不存在：{closure_id}")
            if closure.ended_at is not None:
                raise BookingStateError(f"封闭已结束：{closure_id}")
            ended_at = at or self.now()
            if ended_at <= closure.start:
                raise VenueServiceError("提前结束时间不能早于封闭开始时间")
            closure.ended_at = ended_at
            closure.ended_by = actor
            self._audit(
                actor,
                "CLOSURE_END_EARLY",
                "closure",
                closure.id,
                ended_at=_fmt(ended_at),
                planned_end=_fmt(closure.end),
            )
            self._save()
            return closure

    # ------------------------------------------------------------------ #
    # 预约流程：申请 → 支付回调 → 审核确认
    # ------------------------------------------------------------------ #

    def request_booking(
        self,
        zone_id: str,
        activity: str,
        headcount: int,
        start: datetime,
        end: datetime,
        organizer: str,
        amount: float,
        request_id: Optional[str] = None,
    ) -> Booking:
        """提交预约申请。

        ``request_id`` 为申请幂等键：重复提交返回原预约，不会产生第二条。
        封闭/维护/容量/项目不符在申请时即被拒绝；确认时还会在锁内复查。
        """
        with self._lock:
            request_id = request_id or f"req-{uuid.uuid4().hex}"
            existing_id = self._request_index.get(request_id)
            if existing_id is not None:
                existing = self.bookings[existing_id]
                signature = (zone_id, activity, headcount, start, end, organizer, amount)
                current = (
                    existing.zone_id,
                    existing.activity,
                    existing.headcount,
                    existing.start,
                    existing.end,
                    existing.organizer,
                    existing.amount,
                )
                if signature != current:
                    raise IdempotencyConflict(
                        f"幂等键 {request_id} 已用于预约 {existing_id}，参数不一致"
                    )
                return existing

            self._validate_bookable(zone_id, activity, headcount, start, end)

            booking = Booking(
                id=self._next_id("booking"),
                request_id=request_id,
                zone_id=zone_id,
                activity=activity,
                headcount=headcount,
                start=start,
                end=end,
                organizer=organizer,
                amount=amount,
                created_at=self.now(),
            )
            self.bookings[booking.id] = booking
            self._request_index[request_id] = booking.id
            self._audit(
                organizer,
                "BOOKING_REQUEST",
                "booking",
                booking.id,
                request_id=request_id,
                zone_id=zone_id,
                activity=activity,
                headcount=headcount,
                start=_fmt(start),
                end=_fmt(end),
                amount=amount,
            )
            self._save()
            return booking

    def record_payment(
        self, payment_id: str, booking_id: str, amount: float
    ) -> Payment:
        """登记支付回调，以支付流水号幂等。

        重复回调直接返回首笔记录，不改变任何状态；同一流水号但金额/预约
        不一致则抛 :class:`PaymentMismatchError`，防止串单。
        """
        with self._lock:
            existing = self.payments.get(payment_id)
            if existing is not None:
                if existing.booking_id != booking_id or existing.amount != amount:
                    raise PaymentMismatchError(
                        f"支付单 {payment_id} 已绑定预约 {existing.booking_id} "
                        f"金额 {existing.amount}，与回调不一致"
                    )
                return existing

            booking = self.bookings.get(booking_id)
            if booking is None:
                raise NotFoundError(f"预约不存在：{booking_id}")
            if booking.status not in (BookingStatus.REQUESTED, BookingStatus.PAID):
                raise BookingStateError(
                    f"预约 {booking_id} 当前状态 {booking.status}，不可登记支付"
                )

            payment = Payment(
                payment_id=payment_id,
                booking_id=booking_id,
                amount=amount,
                paid_at=self.now(),
            )
            self.payments[payment_id] = payment
            booking.status = BookingStatus.PAID
            booking.payment_id = payment_id
            booking.paid_at = payment.paid_at
            self._audit(
                "支付渠道",
                "PAYMENT_RECEIVED",
                "booking",
                booking_id,
                payment_id=payment_id,
                amount=amount,
            )
            self._save()
            return payment

    def confirm_booking(self, booking_id: str, operator: str) -> Confirmation:
        """运营员审核通过，生成 v1 确认单并持有场地占用。

        全部复查在同一把锁内完成：并发审核同一时段只有一笔成功，其余拿到
        冲突解释。对已确认预约重复调用返回当前确认单，不产生第二份占用。
        """
        with self._lock:
            booking = self._require_booking(booking_id)
            if booking.status == BookingStatus.CONFIRMED:
                return booking.latest_confirmation()
            if booking.status != BookingStatus.PAID:
                raise BookingStateError(
                    f"预约 {booking_id} 尚未支付（当前 {booking.status}），不能审核"
                )

            reasons = self._blockers(
                booking.zone_id, booking.start, booking.end,
                exclude_booking_id=booking_id,
            )
            if reasons:
                raise BookingConflictError(reasons)

            confirmation = self._issue_confirmation(booking, operator)
            booking.status = BookingStatus.CONFIRMED
            self._audit(
                operator,
                "BOOKING_CONFIRM",
                "booking",
                booking.id,
                version=confirmation.version,
                revision=confirmation.revision,
                zone_id=booking.zone_id,
                start=_fmt(booking.start),
                end=_fmt(booking.end),
            )
            self._save()
            return confirmation

    def reschedule_booking(
        self,
        booking_id: str,
        new_start: datetime,
        new_end: datetime,
        operator: str,
    ) -> Confirmation:
        """为已确认活动改期，生成下一版本确认单。

        封闭/维护/其他占用中的新时段一律拒绝；成功后旧时段立即释放。
        """
        with self._lock:
            booking = self._require_booking(booking_id)
            if booking.status != BookingStatus.CONFIRMED:
                raise BookingStateError(
                    f"预约 {booking_id} 未确认（{booking.status}），不能改期"
                )
            if new_end <= new_start:
                raise VenueServiceError("改期结束时间必须晚于开始时间")

            reasons = self._blockers(
                booking.zone_id, new_start, new_end, exclude_booking_id=booking.id
            )
            if reasons:
                raise BookingConflictError(reasons)

            old_start, old_end = booking.start, booking.end
            booking.start = new_start
            booking.end = new_end
            confirmation = self._issue_confirmation(booking, operator)
            self._audit(
                operator,
                "BOOKING_RESCHEDULE",
                "booking",
                booking.id,
                version=confirmation.version,
                revision=confirmation.revision,
                old_start=_fmt(old_start),
                old_end=_fmt(old_end),
                new_start=_fmt(new_start),
                new_end=_fmt(new_end),
            )
            self._save()
            return confirmation

    def cancel_booking(
        self,
        booking_id: str,
        actor: str,
        *,
        caused_by_closure: bool = False,
        closure_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Optional[RefundRecord]:
        """取消预约并记录退款依据，占用立即释放。

        * 封闭导致无法履约：任意提前期内全额退款；
        * 活动开始前 ≥48h：全额；24h–48h：半额；<24h：不退；
        * 尚未审核通过的付费预约：全额；未支付：无退款。
        重复取消幂等，返回首次记录的退款依据。
        """
        with self._lock:
            booking = self._require_booking(booking_id)
            if booking.status == BookingStatus.CANCELLED:
                return booking.refund

            at = self.now()
            refund: Optional[RefundRecord]

            if booking.status == BookingStatus.REQUESTED:
                refund = None
                basis = "预约尚未支付，取消后无退款"
            elif booking.status == BookingStatus.PAID:
                refund = self._refund(
                    booking, 1.0, "UNCONFIRMED_FULL",
                    "预约在运营审核前取消，已支付费用全额退款",
                    actor, at, False, None,
                )
                basis = refund.basis
            else:  # CONFIRMED
                if not caused_by_closure and at >= booking.start:
                    raise BookingStateError(
                        f"预约 {booking_id} 已开始，普通取消不适用，需走争议流程"
                    )
                if caused_by_closure:
                    if closure_id is None or closure_id not in self.closures:
                        raise NotFoundError("封闭导致的取消必须提供有效的 closure_id")
                    closure = self.closures[closure_id]
                    refund = self._refund(
                        booking, 1.0, "CLOSURE_FULL",
                        f"因封闭 {closure_id}（{closure.reason}，"
                        f"{_fmt(closure.start)}–{_fmt(closure.effective_end())}）"
                        f"导致活动无法履约，按规则全额退款",
                        actor, at, True, closure_id,
                    )
                else:
                    lead_hours = (booking.start - at).total_seconds() / 3600.0
                    if lead_hours >= FULL_REFUND_LEAD_HOURS:
                        ratio, code = 1.0, "SELF_FULL"
                        rule_text = (
                            f"活动开始前 {lead_hours:.1f} 小时（≥48h）"
                            "由申请人取消，按规则全额退款"
                        )
                    elif lead_hours >= HALF_REFUND_LEAD_HOURS:
                        ratio, code = 0.5, "SELF_HALF"
                        rule_text = (
                            f"活动开始前 {lead_hours:.1f} 小时（24h–48h 之间）"
                            "由申请人取消，按规则退还 50%"
                        )
                    else:
                        ratio, code = 0.0, "SELF_NONE"
                        rule_text = (
                            f"活动开始前 {lead_hours:.1f} 小时（<24h）"
                            "由申请人取消，按规则不予退款"
                        )
                    refund = self._refund(
                        booking, ratio, code, rule_text, actor, at, False, None
                    )
                basis = refund.basis

            booking.status = BookingStatus.CANCELLED
            booking.cancelled_at = at
            booking.cancel_actor = actor
            booking.refund = refund
            self._audit(
                actor,
                "BOOKING_CANCEL",
                "booking",
                booking.id,
                caused_by_closure=caused_by_closure,
                closure_id=closure_id,
                note=note,
                refund_code=refund.code if refund else "NO_PAYMENT",
                refund_ratio=refund.ratio if refund else None,
                refund_amount=refund.amount if refund else None,
                refund_basis=refund.basis if refund else basis,
            )
            self._save()
            return refund

    # ------------------------------------------------------------------ #
    # 查询：日历 / 冲突解释 / 替代建议 / 审计
    # ------------------------------------------------------------------ #

    def explain(
        self,
        zone_id: str,
        start: datetime,
        end: datetime,
        activity: Optional[str] = None,
        headcount: Optional[int] = None,
        exclude_booking_id: Optional[str] = None,
    ) -> list[ConflictReason]:
        """返回某分区某时段不可预约的全部结构化原因（没有则为空）。"""
        with self._lock:
            zone = self._require_zone(zone_id)
            reasons: list[ConflictReason] = []
            if end <= start:
                reasons.append(
                    ConflictReason(
                        "INVALID_TIME", f"结束时间 {_fmt(end)} 不晚于开始时间 {_fmt(start)}"
                    )
                )
            if activity is not None and activity not in zone.activities:
                reasons.append(
                    ConflictReason(
                        "ACTIVITY_UNSUPPORTED",
                        f"分区 {zone.name}（{zone_id}）不承载项目“{activity}”，"
                        f"可承载：{'、'.join(sorted(zone.activities))}",
                        "zone", zone_id, start, end,
                        {"supported": sorted(zone.activities), "requested": activity},
                    )
                )
            if headcount is not None and headcount > zone.capacity:
                reasons.append(
                    ConflictReason(
                        "CAPACITY_EXCEEDED",
                        f"分区 {zone.name} 容量 {zone.capacity} 人，"
                        f"无法容纳 {headcount} 人",
                        "zone", zone_id, start, end,
                        {"capacity": zone.capacity, "headcount": headcount},
                    )
                )
            reasons += self._blockers(
                zone_id, start, end, exclude_booking_id=exclude_booking_id
            )
            return reasons

    def suggest_alternatives(
        self,
        zone_id: str,
        start: datetime,
        end: datetime,
        activity: str,
        headcount: int,
    ) -> dict:
        """推荐替代分区，逐分区说明容量与项目兼容性。

        返回 ``{"viable": [...], "rejected": [...]}``；viable 中的分区
        项目兼容、容量足够且时段空闲（含清场缓冲与封闭/维护判断）。
        """
        with self._lock:
            self._require_zone(zone_id)
            viable, rejected = [], []
            for candidate in self.zones.values():
                if candidate.id == zone_id:
                    continue
                activity_ok = activity in candidate.activities
                capacity_ok = headcount <= candidate.capacity
                blockers = self._blockers(candidate.id, start, end)
                free = not blockers
                record = {
                    "zone_id": candidate.id,
                    "zone_name": candidate.name,
                    "venue_id": candidate.venue_id,
                    "venue_name": self.venues[candidate.venue_id].name,
                    "capacity": candidate.capacity,
                    "headcount": headcount,
                    "capacity_ok": capacity_ok,
                    "activity": activity,
                    "supported_activities": sorted(candidate.activities),
                    "activity_ok": activity_ok,
                    "same_venue": candidate.venue_id == self.zones[zone_id].venue_id,
                    "free": free,
                    "buffer_minutes": candidate.buffer_minutes,
                }
                if activity_ok and capacity_ok and free:
                    viable.append(record)
                else:
                    why = []
                    if not activity_ok:
                        why.append("项目不兼容")
                    if not capacity_ok:
                        why.append(f"容量不足（{candidate.capacity}<{headcount}）")
                    if not free:
                        why.extend(f"[{r.code}] {r.message}" for r in blockers)
                    record["reasons"] = why
                    rejected.append(record)

            viable.sort(key=lambda r: (not r["same_venue"], r["capacity"]))
            return {"viable": viable, "rejected": rejected}

    def calendar(
        self, start: datetime, end: datetime, zone_id: Optional[str] = None
    ) -> list[dict]:
        """日历视图：返回窗口内维护、封闭、预约（含清场缓冲）条目。"""
        with self._lock:
            if zone_id is not None:
                self._require_zone(zone_id)
                zone_ids = self._overlapping_zone_ids(zone_id)
            else:
                zone_ids = set(self.zones)

            entries: list[dict] = []

            seen_maint: set[tuple[str, datetime, datetime]] = set()
            for zid in zone_ids:
                for rule, s, e in self._maintenance_hits(zid, start, end):
                    key = (rule.id, s, e)
                    if key in seen_maint:
                        continue
                    seen_maint.add(key)
                    entries.append({
                        "type": "maintenance",
                        "source_id": rule.id,
                        "zone_id": rule.zone_id,
                        "start": s,
                        "end": e,
                        "title": f"维护：{rule.reason}",
                        "crosses_midnight": s.date() != e.date(),
                        "derived_from": {
                            "rule_id": rule.id,
                            "weekday": rule.weekday,
                            "start_time": rule.start_time.strftime("%H:%M"),
                            "duration_minutes": rule.duration_minutes,
                        },
                    })

            for closure in self.closures.values():
                if closure.zone_id not in zone_ids:
                    continue
                s, e = closure.start, closure.effective_end()
                if not _overlaps(start, end, s, e):
                    continue
                entries.append({
                    "type": "closure",
                    "source_id": closure.id,
                    "zone_id": closure.zone_id,
                    "start": max(s, start),
                    "end": min(e, end),
                    "title": f"临时封闭：{closure.reason}",
                    "status": "active" if closure.is_active else "ended_early",
                    "planned_end": closure.end,
                })

            for booking in self.bookings.values():
                if booking.status == BookingStatus.CANCELLED:
                    continue
                if booking.zone_id not in zone_ids:
                    continue
                if not _overlaps(start, end, booking.start, booking.end):
                    continue
                buffer_end = booking.end + timedelta(
                    minutes=self.zones[booking.zone_id].buffer_minutes
                )
                latest = booking.latest_confirmation()
                entries.append({
                    "type": "booking",
                    "source_id": booking.id,
                    "zone_id": booking.zone_id,
                    "start": booking.start,
                    "end": booking.end,
                    "buffer_end": buffer_end,
                    "title": f"{booking.activity}：{booking.organizer}",
                    "status": booking.status,
                    "version": latest.version if latest else None,
                    "revision": latest.revision if latest else None,
                })

            entries.sort(key=lambda x: (x["start"], x["end"], x["type"]))
            return entries

    def decision_sources(
        self, start: datetime, end: datetime, zone_id: Optional[str] = None
    ) -> dict:
        """管理员审计：列出某时间段内所有“决策来源”。

        包括：产生占用的预约及其确认单版本、支付、取消与退款依据、临时封闭
        及其发布/提前结束动作、周期维护规则及其在窗口内推导出的具体实例。
        """
        with self._lock:
            if zone_id is not None:
                self._require_zone(zone_id)
                zone_ids = self._overlapping_zone_ids(zone_id)
            else:
                zone_ids = set(self.zones)

            bookings_out, payments_out = [], []
            for booking in self.bookings.values():
                if booking.zone_id not in zone_ids:
                    continue
                if not _overlaps(start, end, booking.start, booking.end):
                    continue
                payments_out.append(booking.payment_id) if booking.payment_id else None
                bookings_out.append({
                    "kind": "booking",
                    "booking_id": booking.id,
                    "request_id": booking.request_id,
                    "zone_id": booking.zone_id,
                    "activity": booking.activity,
                    "headcount": booking.headcount,
                    "start": booking.start.isoformat(),
                    "end": booking.end.isoformat(),
                    "organizer": booking.organizer,
                    "status": booking.status,
                    "amount": booking.amount,
                    "payment_id": booking.payment_id,
                    "confirmations": [c.to_dict() for c in booking.history],
                    "refund": None
                    if booking.refund is None
                    else {
                        "code": booking.refund.code,
                        "ratio": booking.refund.ratio,
                        "amount": booking.refund.amount,
                        "basis": booking.refund.basis,
                        "caused_by_closure": booking.refund.caused_by_closure,
                        "closure_id": booking.refund.closure_id,
                        "decided_by": booking.refund.decided_by,
                        "decided_at": booking.refund.decided_at.isoformat(),
                    },
                })

            closures_out = []
            for closure in self.closures.values():
                if closure.zone_id not in zone_ids:
                    continue
                s, e = closure.start, closure.effective_end()
                if not _overlaps(start, end, s, e):
                    continue
                closures_out.append({
                    "kind": "closure",
                    "closure_id": closure.id,
                    "zone_id": closure.zone_id,
                    "start": s.isoformat(),
                    "end": e.isoformat(),
                    "planned_end": closure.end.isoformat(),
                    "ended_early": closure.ended_at is not None,
                    "reason": closure.reason,
                    "published_by": closure.published_by,
                    "published_at": closure.published_at.isoformat(),
                    "ended_by": closure.ended_by,
                    "ended_at": closure.ended_at.isoformat() if closure.ended_at else None,
                })

            maintenance_out = []
            seen_maint: set[tuple[str, datetime, datetime]] = set()
            for zid in zone_ids:
                for rule, s, e in self._maintenance_hits(zid, start, end):
                    key = (rule.id, s, e)
                    if key in seen_maint:
                        continue
                    seen_maint.add(key)
                    maintenance_out.append({
                        "kind": "maintenance_occurrence",
                        "rule_id": rule.id,
                        "zone_id": rule.zone_id,
                        "start": s.isoformat(),
                        "end": e.isoformat(),
                        "crosses_midnight": s.date() != e.date(),
                        "reason": rule.reason,
                        "derived_from": {
                            "weekday": rule.weekday,
                            "start_time": rule.start_time.strftime("%H:%M"),
                            "duration_minutes": rule.duration_minutes,
                            "registered_by": rule.created_by,
                            "registered_at": rule.created_at.isoformat(),
                        },
                    })

            relevant_targets = {
                ("booking", b["booking_id"]) for b in bookings_out
            } | {
                ("closure", c["closure_id"]) for c in closures_out
            } | {
                ("maintenance", m["rule_id"]) for m in maintenance_out
            } | {
                ("zone", zid) for zid in zone_ids
            }

            audit_out = []
            for ev in self.events:
                key = (ev.target_type, ev.target_id)
                related = key in relevant_targets
                if not related and ev.payload.get("zone_id") in zone_ids:
                    related = True
                if related:
                    audit_out.append({
                        "seq": ev.seq,
                        "at": ev.at.isoformat(),
                        "actor": ev.actor,
                        "action": ev.action,
                        "target_type": ev.target_type,
                        "target_id": ev.target_id,
                        "payload": ev.payload,
                    })

            return {
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "zone_ids": sorted(zone_ids),
                "bookings": bookings_out,
                "payments": [
                    {
                        "payment_id": p.payment_id,
                        "booking_id": p.booking_id,
                        "amount": p.amount,
                        "paid_at": p.paid_at.isoformat(),
                    }
                    for p in self.payments.values()
                    if p.payment_id in payments_out
                ],
                "closures": closures_out,
                "maintenance": maintenance_out,
                "audit_events": audit_out,
            }

    def audit(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        zone_id: Optional[str] = None,
        booking_id: Optional[str] = None,
    ) -> list[dict]:
        """按事件发生时间检索不可变审计流水。"""
        with self._lock:
            result = []
            for ev in self.events:
                if start is not None and ev.at < start:
                    continue
                if end is not None and ev.at > end:
                    continue
                if booking_id is not None and ev.target_id != booking_id:
                    continue
                if zone_id is not None:
                    payload_zone = ev.payload.get("zone_id")
                    related = payload_zone in self._overlapping_zone_ids(zone_id)
                    if not related and ev.target_type == "zone":
                        related = ev.target_id in self._overlapping_zone_ids(zone_id)
                    if not related:
                        continue
                result.append({
                    "seq": ev.seq,
                    "at": ev.at.isoformat(),
                    "actor": ev.actor,
                    "action": ev.action,
                    "target_type": ev.target_type,
                    "target_id": ev.target_id,
                    "payload": ev.payload,
                })
            return result

    def get_booking(self, booking_id: str) -> Booking:
        with self._lock:
            return self._require_booking(booking_id)

    def get_confirmation(self, booking_id: str, version: Optional[int] = None) -> Confirmation:
        with self._lock:
            booking = self._require_booking(booking_id)
            if not booking.history:
                raise NotFoundError(f"预约 {booking_id} 尚无确认单")
            if version is None:
                return booking.latest_confirmation()
            for confirmation in booking.history:
                if confirmation.version == version:
                    return confirmation
            raise NotFoundError(f"预约 {booking_id} 不存在 v{version} 确认单")

    # ------------------------------------------------------------------ #
    # 内部：占用关系与冲突引擎
    # ------------------------------------------------------------------ #

    def _validate_bookable(
        self, zone_id: str, activity: str, headcount: int,
        start: datetime, end: datetime,
        exclude_booking_id: Optional[str] = None,
    ) -> None:
        reasons = self.explain(
            zone_id, start, end, activity=activity, headcount=headcount,
            exclude_booking_id=exclude_booking_id,
        )
        if reasons:
            raise BookingConflictError(reasons)

    def _blockers(
        self,
        zone_id: str,
        start: datetime,
        end: datetime,
        exclude_booking_id: Optional[str] = None,
    ) -> list[ConflictReason]:
        """计算占用类冲突：临时封闭、维护实例、已确认预约（含清场缓冲）。

        缓冲双向生效：既有活动结束后保留其清场缓冲；待入场活动自身的缓冲
        也不得侵入后续封闭/维护/活动，因此比较时把候选窗口右端延长
        ``buffer_minutes``。
        """
        reasons: list[ConflictReason] = []
        related = self._overlapping_zone_ids(zone_id)
        buffered_end = end + timedelta(minutes=self.zones[zone_id].buffer_minutes)

        for closure in self.closures.values():
            if closure.zone_id not in related:
                continue
            s, e = closure.start, closure.effective_end()
            if _overlaps(start, buffered_end, s, e):
                reasons.append(ConflictReason(
                    "CLOSURE",
                    f"{self._relation_text(zone_id, closure.zone_id)}："
                    f"{_fmt(s)}–{_fmt(e)} 临时封闭（{closure.reason}），封闭期间不可新建预约",
                    "closure", closure.id, closure.zone_id, s, e,
                    {"ended_early": closure.ended_at is not None},
                ))

        for rule, s, e in self._maintenance_hits(zone_id, start, buffered_end):
            reasons.append(ConflictReason(
                "MAINTENANCE",
                f"{self._relation_text(zone_id, rule.zone_id)}："
                f"周期养护 {_fmt(s)}–{_fmt(e)}（{rule.reason}，规则 {rule.id}）",
                "maintenance", rule.id, rule.zone_id, s, e,
                {"rule_id": rule.id, "crosses_midnight": s.date() != e.date()},
            ))

        for booking in self.bookings.values():
            if booking.id == exclude_booking_id:
                continue
            if booking.status != BookingStatus.CONFIRMED:
                continue
            if booking.zone_id not in related:
                continue
            buffer_minutes = self.zones[booking.zone_id].buffer_minutes
            existing_end = booking.end + timedelta(minutes=buffer_minutes)
            if _overlaps(start, buffered_end, booking.start, existing_end):
                tail = (
                    f"，另需 {buffer_minutes} 分钟清场缓冲（占用至 {_fmt(existing_end)}）"
                    if buffer_minutes else ""
                )
                latest = booking.latest_confirmation()
                reasons.append(ConflictReason(
                    "BOOKING",
                    f"{self._relation_text(zone_id, booking.zone_id)}："
                    f"预约 {booking.id}（{booking.organizer}·{booking.activity}）"
                    f"已占用 {_fmt(booking.start)}–{_fmt(booking.end)}{tail}",
                    "booking", booking.id, booking.zone_id,
                    booking.start, buffered_end,
                    {
                        "buffer_minutes": buffer_minutes,
                        "buffered_end": buffered_end.isoformat(),
                        "confirmation_version": latest.version if latest else None,
                    },
                ))
        return reasons

    def _maintenance_hits(
        self, zone_id: str, start: datetime, end: datetime
    ) -> list[tuple[MaintenanceRule, datetime, datetime]]:
        """展开与 [start, end) 相交的周期维护实例，正确覆盖跨午夜时段。"""
        related = self._overlapping_zone_ids(zone_id)
        rules = [
            rule for rule in self.maintenance.values()
            if rule.active and rule.zone_id in related
        ]
        if not rules:
            return []

        max_span_days = max(rule.duration_minutes for rule in rules) // 1440 + 2
        day = (start - timedelta(days=max_span_days)).date()
        hits: list[tuple[MaintenanceRule, datetime, datetime]] = []
        while day <= end.date():
            for rule in rules:
                if day.weekday() != rule.weekday:
                    continue
                occ_start = datetime.combine(day, rule.start_time)
                if start.tzinfo is not None:
                    occ_start = occ_start.replace(tzinfo=start.tzinfo)
                occ_end = occ_start + timedelta(minutes=rule.duration_minutes)
                if _overlaps(start, end, occ_start, occ_end):
                    hits.append((rule, occ_start, occ_end))
            day += timedelta(days=1)
        return hits

    # ------------------------------------------------------------------ #
    # 内部：分区层级关系
    # ------------------------------------------------------------------ #

    def _ancestor_ids(self, zone: Zone) -> set[str]:
        ids = {zone.id}
        current = zone
        seen: set[str] = set()
        while current.parent_id and current.parent_id not in seen:
            seen.add(current.parent_id)
            parent = self.zones.get(current.parent_id)
            if parent is None:
                break
            ids.add(parent.id)
            current = parent
        return ids

    def _zone_overlaps_id(self, zone_a: str, zone_b: str) -> bool:
        a = self.zones.get(zone_a)
        b = self.zones.get(zone_b)
        if a is None or b is None:
            return False
        if a.venue_id != b.venue_id:
            return False
        if a.id == b.id:
            return True
        return b.id in self._ancestor_ids(a) or a.id in self._ancestor_ids(b)

    def _overlapping_zone_ids(self, zone_id: str) -> set[str]:
        zone = self._require_zone(zone_id)
        return {other.id for other in self.zones.values() if self._zone_overlaps_id(zone.id, other.id)}

    def _relation_text(self, target_zone_id: str, other_zone_id: str) -> str:
        """用人类语言说明两个分区的占用关系。"""
        if target_zone_id == other_zone_id:
            return f"同一分区 {self.zones[target_zone_id].name}"
        target = self.zones[target_zone_id]
        other = self.zones[other_zone_id]
        if other_zone_id in self._ancestor_ids(target):
            return f"上级分区 {other.name}（{other_zone_id}）包含本分区，占用关系联动"
        if target_zone_id in self._ancestor_ids(other):
            return f"本分区 {target.name} 包含下级分区 {other.name}（{other_zone_id}），占用关系联动"
        return f"同一场地关联分区 {other.name}"

    # ------------------------------------------------------------------ #
    # 内部：杂项与持久化
    # ------------------------------------------------------------------ #

    def _require_zone(self, zone_id: str) -> Zone:
        zone = self.zones.get(zone_id)
        if zone is None:
            raise NotFoundError(f"分区不存在：{zone_id}")
        return zone

    def _require_booking(self, booking_id: str) -> Booking:
        booking = self.bookings.get(booking_id)
        if booking is None:
            raise NotFoundError(f"预约不存在：{booking_id}")
        return booking

    def _issue_confirmation(self, booking: Booking, operator: str) -> Confirmation:
        version = len(booking.history) + 1
        confirmed_at = self.now()
        revision_src = (
            f"{booking.id}|{version}|{booking.zone_id}|"
            f"{booking.start.isoformat()}|{booking.end.isoformat()}"
        )
        revision = hashlib.sha1(revision_src.encode("utf-8")).hexdigest()[:12]
        confirmation = Confirmation(
            booking_id=booking.id,
            version=version,
            zone_id=booking.zone_id,
            start=booking.start,
            end=booking.end,
            activity=booking.activity,
            headcount=booking.headcount,
            confirmed_by=operator,
            confirmed_at=confirmed_at,
            revision=revision,
        )
        booking.history.append(confirmation)
        return confirmation

    def _refund(
        self,
        booking: Booking,
        ratio: float,
        code: str,
        basis: str,
        actor: str,
        at: datetime,
        caused_by_closure: bool,
        closure_id: Optional[str],
    ) -> RefundRecord:
        return RefundRecord(
            code=code,
            ratio=ratio,
            amount=round(booking.amount * ratio, 2),
            basis=basis,
            caused_by_closure=caused_by_closure,
            closure_id=closure_id,
            decided_by=actor,
            decided_at=at,
        )

    def _next_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n:04d}"

    def _audit(
        self, actor: str, action: str, target_type: str, target_id: str, **payload
    ) -> None:
        seq = self._next_id("event")
        self.events.append(AuditEvent(
            seq=seq,
            at=self.now(),
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
        ))

    def _save(self) -> None:
        if not self.path:
            return
        data = {
            "version": SNAPSHOT_VERSION,
            "counters": self._counters,
            "venues": {
                vid: {"name": v.name, "zone": v.zone} for vid, v in self.venues.items()
            },
            "zones": [
                {
                    "id": z.id,
                    "venue_id": z.venue_id,
                    "name": z.name,
                    "capacity": z.capacity,
                    "activities": sorted(z.activities),
                    "parent_id": z.parent_id,
                    "buffer_minutes": z.buffer_minutes,
                }
                for z in self.zones.values()
            ],
            "maintenance": [
                {
                    "id": r.id,
                    "zone_id": r.zone_id,
                    "weekday": r.weekday,
                    "start_time": r.start_time.strftime("%H:%M"),
                    "duration_minutes": r.duration_minutes,
                    "reason": r.reason,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat(),
                    "active": r.active,
                }
                for r in self.maintenance.values()
            ],
            "closures": [
                {
                    "id": c.id,
                    "zone_id": c.zone_id,
                    "start": c.start.isoformat(),
                    "end": c.end.isoformat(),
                    "reason": c.reason,
                    "published_by": c.published_by,
                    "published_at": c.published_at.isoformat(),
                    "ended_by": c.ended_by,
                    "ended_at": c.ended_at.isoformat() if c.ended_at else None,
                }
                for c in self.closures.values()
            ],
            "payments": [
                {
                    "payment_id": p.payment_id,
                    "booking_id": p.booking_id,
                    "amount": p.amount,
                    "paid_at": p.paid_at.isoformat(),
                }
                for p in self.payments.values()
            ],
            "bookings": [self._dump_booking(b) for b in self.bookings.values()],
            "events": [
                {
                    "seq": e.seq,
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "action": e.action,
                    "target_type": e.target_type,
                    "target_id": e.target_id,
                    "payload": e.payload,
                }
                for e in self.events
            ],
        }
        directory = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".venue-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _dump_booking(self, b: Booking) -> dict:
        return {
            "id": b.id,
            "request_id": b.request_id,
            "zone_id": b.zone_id,
            "activity": b.activity,
            "headcount": b.headcount,
            "start": b.start.isoformat(),
            "end": b.end.isoformat(),
            "organizer": b.organizer,
            "amount": b.amount,
            "created_at": b.created_at.isoformat(),
            "status": b.status,
            "payment_id": b.payment_id,
            "paid_at": b.paid_at.isoformat() if b.paid_at else None,
            "history": [c.to_dict() for c in b.history],
            "cancelled_at": b.cancelled_at.isoformat() if b.cancelled_at else None,
            "cancel_actor": b.cancel_actor,
            "refund": None
            if b.refund is None
            else {
                "code": b.refund.code,
                "ratio": b.refund.ratio,
                "amount": b.refund.amount,
                "basis": b.refund.basis,
                "caused_by_closure": b.refund.caused_by_closure,
                "closure_id": b.refund.closure_id,
                "decided_by": b.refund.decided_by,
                "decided_at": b.refund.decided_at.isoformat(),
            },
        }

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != SNAPSHOT_VERSION:
            raise VenueServiceError(
                f"快照版本不兼容：{data.get('version')} != {SNAPSHOT_VERSION}"
            )

        self._counters = dict(data.get("counters", {}))
        self.venues = {
            vid: Venue(name=v["name"], zone=v["zone"])
            for vid, v in data.get("venues", {}).items()
        }
        self.zones = {
            z["id"]: Zone(
                id=z["id"],
                venue_id=z["venue_id"],
                name=z["name"],
                capacity=z["capacity"],
                activities=frozenset(z["activities"]),
                parent_id=z.get("parent_id"),
                buffer_minutes=z.get("buffer_minutes", 0),
            )
            for z in data.get("zones", [])
        }
        self.maintenance = {
            r["id"]: MaintenanceRule(
                id=r["id"],
                zone_id=r["zone_id"],
                weekday=r["weekday"],
                start_time=time.fromisoformat(r["start_time"]),
                duration_minutes=r["duration_minutes"],
                reason=r["reason"],
                created_by=r["created_by"],
                created_at=datetime.fromisoformat(r["created_at"]),
                active=r.get("active", True),
            )
            for r in data.get("maintenance", [])
        }
        self.closures = {
            c["id"]: Closure(
                id=c["id"],
                zone_id=c["zone_id"],
                start=datetime.fromisoformat(c["start"]),
                end=datetime.fromisoformat(c["end"]),
                reason=c["reason"],
                published_by=c["published_by"],
                published_at=datetime.fromisoformat(c["published_at"]),
                ended_by=c.get("ended_by"),
                ended_at=datetime.fromisoformat(c["ended_at"]) if c.get("ended_at") else None,
            )
            for c in data.get("closures", [])
        }
        self.payments = {
            p["payment_id"]: Payment(
                payment_id=p["payment_id"],
                booking_id=p["booking_id"],
                amount=p["amount"],
                paid_at=datetime.fromisoformat(p["paid_at"]),
            )
            for p in data.get("payments", [])
        }
        self.bookings = {}
        for raw in data.get("bookings", []):
            history = [
                Confirmation(
                    booking_id=raw["id"],
                    version=h["version"],
                    zone_id=h["zone_id"],
                    start=datetime.fromisoformat(h["start"]),
                    end=datetime.fromisoformat(h["end"]),
                    activity=h["activity"],
                    headcount=h["headcount"],
                    confirmed_by=h["confirmed_by"],
                    confirmed_at=datetime.fromisoformat(h["confirmed_at"]),
                    revision=h["revision"],
                )
                for h in raw.get("history", [])
            ]
            refund_raw = raw.get("refund")
            refund = None
            if refund_raw:
                refund = RefundRecord(
                    code=refund_raw["code"],
                    ratio=refund_raw["ratio"],
                    amount=refund_raw["amount"],
                    basis=refund_raw["basis"],
                    caused_by_closure=refund_raw["caused_by_closure"],
                    closure_id=refund_raw.get("closure_id"),
                    decided_by=refund_raw["decided_by"],
                    decided_at=datetime.fromisoformat(refund_raw["decided_at"]),
                )
            booking = Booking(
                id=raw["id"],
                request_id=raw["request_id"],
                zone_id=raw["zone_id"],
                activity=raw["activity"],
                headcount=raw["headcount"],
                start=datetime.fromisoformat(raw["start"]),
                end=datetime.fromisoformat(raw["end"]),
                organizer=raw["organizer"],
                amount=raw["amount"],
                created_at=datetime.fromisoformat(raw["created_at"]),
                status=raw["status"],
                payment_id=raw.get("payment_id"),
                paid_at=datetime.fromisoformat(raw["paid_at"]) if raw.get("paid_at") else None,
                history=history,
                cancelled_at=datetime.fromisoformat(raw["cancelled_at"]) if raw.get("cancelled_at") else None,
                cancel_actor=raw.get("cancel_actor"),
                refund=refund,
            )
            self.bookings[booking.id] = booking
            self._request_index[booking.request_id] = booking.id

        self.events = [
            AuditEvent(
                seq=e["seq"],
                at=datetime.fromisoformat(e["at"]),
                actor=e["actor"],
                action=e["action"],
                target_type=e["target_type"],
                target_id=e["target_id"],
                payload=e["payload"],
            )
            for e in data.get("events", [])
        ]
