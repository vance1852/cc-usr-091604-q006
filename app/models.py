"""场地资源服务的领域模型。

涵盖场地、分区、维护窗口、临时封闭、预约、支付、确认单、
退款与审计事件等核心概念。所有时间均为带时区的 datetime,
服务内部统一按 UTC 处理;跨午夜时段用绝对区间自然表达,
不做"按天切分"的特殊逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class BookingStatus(str, Enum):
    """预约状态机。"""

    PENDING_PAYMENT = "PENDING_PAYMENT"  # 已申请,待支付
    PAID = "PAID"                        # 已支付,待运营审核
    CONFIRMED = "CONFIRMED"              # 已确认,正式占用场地
    AFFECTED = "AFFECTED"                # 受封闭/维护影响,只能改期或取消
    REJECTED = "REJECTED"                # 审核未通过
    CANCELLED = "CANCELLED"              # 已取消


#: 占用场地的预约状态:这些状态会阻塞同一资源单元上的其他预约。
#: PAID 也占用——支付成功即锁定场次,避免"两个班都付了钱同时到场"。
OCCUPYING_STATUSES: tuple[BookingStatus, ...] = (
    BookingStatus.PAID,
    BookingStatus.CONFIRMED,
    BookingStatus.AFFECTED,
)


class ClosureStatus(str, Enum):
    """临时封闭状态。"""

    ACTIVE = "ACTIVE"            # 封闭中
    ENDED_EARLY = "ENDED_EARLY"  # 已提前结束


@dataclass(frozen=True)
class Venue:
    """场地。保留起点代码的构造方式:Venue(name, zone)。"""

    name: str
    zone: str = ""       # 所属区域(如"东区")
    venue_id: str = ""

    def __post_init__(self) -> None:
        if not self.venue_id:
            object.__setattr__(self, "venue_id", self.name)


@dataclass(frozen=True)
class Zone:
    """场地分区。

    shares 是该分区占用的资源单元集合:同一场地内两个分区的
    shares 有交集时,时段冲突会互相阻塞。例如五人制场:
    全场 shares=("L","R"),左半场 ("L",),右半场 ("R",)。
    """

    venue_id: str
    zone_id: str
    name: str
    capacity: int
    sports: tuple[str, ...]
    shares: tuple[str, ...]
    buffer_minutes: int = 15      # 使用后的清场缓冲
    price_per_hour: float = 0.0


@dataclass(frozen=True)
class MaintenanceWindow:
    """维护窗口。kind="weekly" 按周重复(start_mow 为周一 00:00 起算的
    分钟数,可跨午夜、跨周溢出);kind="once" 为一次性绝对区间。"""

    window_id: str
    venue_id: str
    zone_id: str | None           # None 表示整个场地
    label: str
    kind: str                     # "weekly" | "once"
    start_mow: int | None
    duration_minutes: int | None
    start: datetime | None        # kind="once" 时使用
    end: datetime | None
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class Closure:
    """临时封闭。zone_ids 为空表示整场封闭。"""

    closure_id: str
    venue_id: str
    zone_ids: tuple[str, ...]
    start: datetime
    end: datetime
    reason: str
    status: ClosureStatus
    published_by: str
    published_at: datetime
    ended_by: str | None = None
    ended_at: datetime | None = None

    @property
    def effective_end(self) -> datetime:
        """实际封闭截止:提前结束时以结束时间为准。"""
        if self.status == ClosureStatus.ENDED_EARLY and self.ended_at is not None:
            return min(self.end, self.ended_at)
        return self.end


@dataclass(frozen=True)
class Booking:
    """预约申请。buffer_minutes 在创建时快照,hold_until 为含清场缓冲的占用截止。"""

    booking_id: str
    venue_id: str
    zone_id: str
    sport: str
    start: datetime
    end: datetime
    customer: str
    party_size: int
    status: BookingStatus
    quoted_price: float
    buffer_minutes: int
    created_by: str
    created_at: datetime

    @property
    def hold_until(self) -> datetime:
        """占用截止(结束时间 + 清场缓冲)。"""
        return self.end + timedelta(minutes=self.buffer_minutes)


@dataclass(frozen=True)
class Payment:
    """支付记录。payment_id 来自支付网关,全局唯一,重复回调幂等。"""

    payment_id: str
    booking_id: str
    amount: float
    paid_at: datetime


@dataclass(frozen=True)
class Confirmation:
    """确认单。(booking_id, version) 唯一;每次确认/改期版本递增。"""

    booking_id: str
    version: int
    operator: str
    created_at: datetime
    note: str = ""

    @property
    def confirmation_id(self) -> str:
        return f"{self.booking_id}#v{self.version}"


@dataclass(frozen=True)
class Refund:
    """退款记录,basis 为退款依据(规则文本)。"""

    refund_id: str
    booking_id: str
    payment_id: str
    amount: float
    basis: str
    created_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    """审计事件:每一次状态变更都留下决策来源。"""

    event_id: str
    ts: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    venue_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConflictDetail:
    """一条冲突/不可用原因。"""

    kind: str        # booking | closure | maintenance | capacity | sport
    ref_id: str
    label: str
    start: datetime | None
    end: datetime | None
    detail: str


@dataclass
class AvailabilityReport:
    """可用性报告:冲突解释接口的返回。"""

    venue_id: str
    zone_id: str
    start: datetime
    end: datetime
    conflicts: list[ConflictDetail] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.conflicts

    def reasons(self) -> list[str]:
        return [c.detail for c in self.conflicts]


@dataclass(frozen=True)
class Suggestion:
    """替代场地建议,reason 说明容量与项目兼容性。"""

    venue_id: str
    zone_id: str
    zone_name: str
    capacity: int
    sports: tuple[str, ...]
    buffer_minutes: int
    reason: str


@dataclass(frozen=True)
class CalendarEntry:
    """日历条目:预约 / 封闭 / 维护窗口。"""

    kind: str        # booking | closure | maintenance
    ref_id: str
    label: str
    venue_id: str
    zone_id: str | None
    start: datetime
    end: datetime
    status: str
