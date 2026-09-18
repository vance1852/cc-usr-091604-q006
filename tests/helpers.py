"""测试共享夹具:场地拓扑与可推进时钟。

场地拓扑(五人制场拆分为全场/左半/右半,用于验证分区占用关系):
- east 东区五人制场
  - east:full  全场   shares=(L,R) 容量20 项目:足球/橄榄球
  - east:left  左半场 shares=(L,)   容量10 项目:足球
  - east:right 右半场 shares=(R,)   容量10 项目:足球/飞盘
- west 西区综合馆
  - west:hall  室内场 shares=(self) 容量30 项目:篮球/羽毛球

2026-09-19 是周六,2026-09-20 是周日,2026-09-21 是周一。
"""

from datetime import datetime, timezone

from app.service import VenueService

UTC = timezone.utc


class Clock:
    """可手动推进的时钟,注入 VenueService(now=...)。"""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t


def dt(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def make_service(db_path: str = ":memory:", now=None) -> VenueService:
    svc = VenueService(db_path=db_path, now=now)
    svc.register_venue("东区五人制场", zone="东区", venue_id="east")
    svc.register_zone("east", "全场", capacity=20, sports=("足球", "橄榄球"),
                      shares=("L", "R"), zone_id="east:full",
                      buffer_minutes=15, price_per_hour=200)
    svc.register_zone("east", "左半场", capacity=10, sports=("足球",),
                      shares=("L",), zone_id="east:left",
                      buffer_minutes=15, price_per_hour=120)
    svc.register_zone("east", "右半场", capacity=10, sports=("足球", "飞盘"),
                      shares=("R",), zone_id="east:right",
                      buffer_minutes=15, price_per_hour=120)
    svc.register_venue("西区综合馆", zone="西区", venue_id="west")
    svc.register_zone("west", "室内场", capacity=30, sports=("篮球", "羽毛球"),
                      zone_id="west:hall", buffer_minutes=10, price_per_hour=300)
    return svc


def confirmed_booking(svc: VenueService, zone: str = "east:full",
                      start=None, end=None, customer: str = "青训A",
                      sport: str = "足球", party: int = 12,
                      payment_id: str = "pay-1", operator: str = "运营员小王"):
    """走完整流程:申请 → 支付 → 审核确认。返回 (booking, confirmation)。"""
    start = start or dt(9, 19, 10)
    end = end or dt(9, 19, 12)
    booking = svc.create_booking("east", zone, sport, start, end, customer, party)
    svc.pay(booking.booking_id, payment_id=payment_id, amount=booking.quoted_price)
    confirmation = svc.review(booking.booking_id, operator=operator)
    return booking, confirmation
