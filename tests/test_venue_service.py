"""场地资源服务的行为测试。

场景：城东体育中心的一块五人制足球场在周末开放前被发现“显示可约、实际
养护”，两个已付费青训班同时到场。测试围绕 2026-09-19（周六）/ 09-20（周日）
展开，验证跨午夜维护、缓冲清场、分区联动、封闭与退款、并发锁定与重启幂等。
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta

from app.venues import (
    BookingConflictError,
    BookingStateError,
    BookingStatus,
    IdempotencyConflict,
    PaymentMismatchError,
    VenueService,
)

SAT = datetime(2026, 9, 19)
SUN = datetime(2026, 9, 20)


def dt(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


class MutableClock:
    def __init__(self, start: datetime):
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def build_world(path: str | None = None, clock=None) -> VenueService:
    """登记场地、分区层级、容量与项目。

    层级：PITCH（整场）下挂 PITCH-A / PITCH-B 两个半场；
    另外有滨河中心的同规格 PITCH2（替代场地）与篮球场（项目不兼容）。
    """
    svc = VenueService(path, clock=clock or (lambda: datetime(2026, 9, 16, 9, 0)))
    svc.register_venue("V1", "城东体育中心", "东区")
    svc.register_venue("V2", "滨河体育中心", "滨河")

    svc.register_zone(
        "PITCH", "V1", "五人制足球场（整场）", 14,
        ["青少年足球训练", "成人五人制"], buffer_minutes=20,
    )
    svc.register_zone(
        "PITCH-A", "V1", "五人制场 A 半场", 7,
        ["青少年足球训练", "成人五人制"],
        parent_id="PITCH", buffer_minutes=15,
    )
    svc.register_zone(
        "PITCH-B", "V1", "五人制场 B 半场", 7,
        ["青少年足球训练", "成人五人制"],
        parent_id="PITCH", buffer_minutes=15,
    )
    svc.register_zone(
        "PITCH2", "V2", "滨河五人制场", 14,
        ["青少年足球训练", "成人五人制"], buffer_minutes=20,
    )
    svc.register_zone(
        "BBALL", "V2", "滨河篮球场", 20, ["篮球"], buffer_minutes=10,
    )
    return svc


def pay_and_confirm(svc: VenueService, booking, operator="运营员小李"):
    svc.record_payment(f"pay-{booking.id}", booking.id, booking.amount)
    return svc.confirm_booking(booking.id, operator)


class RegistrationTest(unittest.TestCase):
    def test_health_and_unsupported_activity_capacity(self):
        svc = build_world()
        self.assertEqual(svc.health()["status"], "ok")

        reasons = svc.explain(
            "PITCH-A", dt(SAT, 10), dt(SAT, 12),
            activity="篮球", headcount=7,
        )
        codes = {r.code for r in reasons}
        self.assertIn("ACTIVITY_UNSUPPORTED", codes)

        reasons = svc.explain(
            "PITCH-A", dt(SAT, 10), dt(SAT, 12),
            activity="青少年足球训练", headcount=12,
        )
        self.assertEqual({r.code for r in reasons}, {"CAPACITY_EXCEEDED"})

    def test_parent_child_zones_block_each_other_but_siblings_share(self):
        svc = build_world()
        booking = svc.request_booking(
            "PITCH-A", "青少年足球训练", 7,
            dt(SAT, 9), dt(SAT, 11), "猎豹青训", 600.0,
            request_id="req-cheetah",
        )
        pay_and_confirm(svc, booking)

        # 查询整场时，A 半场的占用表现为“下级分区”联动
        reasons = svc.explain("PITCH", dt(SAT, 10), dt(SAT, 11, 30))
        self.assertEqual({r.code for r in reasons}, {"BOOKING"})
        self.assertIn("下级分区", reasons[0].message)

        # 另一个半场是同级分区，可同时使用
        self.assertEqual(svc.explain("PITCH-B", dt(SAT, 10), dt(SAT, 11)), [])

        # 整场被占用时，子分区同样不能新建
        whole = svc.request_booking(
            "PITCH", "成人五人制", 12,
            dt(SAT, 13), dt(SAT, 15), "周末联赛", 800.0,
        )
        pay_and_confirm(svc, whole)
        reasons = svc.explain("PITCH-B", dt(SAT, 14), dt(SAT, 15))
        self.assertEqual({r.code for r in reasons}, {"BOOKING"})
        self.assertIn("上级分区", reasons[0].message)


class CrossMidnightAndBufferTest(unittest.TestCase):
    def test_cross_midnight_maintenance_window(self):
        svc = build_world()
        # 每周六 22:00 开始，持续 480 分钟 → 周日 06:00，跨午夜
        rule = svc.register_maintenance(
            "PITCH", 5, time(22, 0), 480, "草坪养护", "维护员老王",
        )
        self.assertEqual(rule.id, "maint-0001")

        # 周六 23:30 - 周日 00:30 整个落在养护内
        reasons = svc.explain("PITCH", dt(SAT, 23, 30), dt(SUN, 0, 30))
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0].code, "MAINTENANCE")
        self.assertTrue(reasons[0].detail["crosses_midnight"])

        # 周日 05:30-07:00 与养护尾部相交
        reasons = svc.explain("PITCH", dt(SUN, 5, 30), dt(SUN, 7))
        self.assertEqual({r.code for r in reasons}, {"MAINTENANCE"})

        # 半开区间：06:00 开始的活动不与养护冲突
        self.assertEqual(svc.explain("PITCH", dt(SUN, 6), dt(SUN, 8)), [])
        # 22:00 之前不冲突
        self.assertEqual(svc.explain("PITCH", dt(SAT, 20), dt(SAT, 21, 30)), [])

        # 维护登记在整场，子分区联动封闭
        reasons = svc.explain("PITCH-B", dt(SUN, 2), dt(SUN, 3))
        self.assertEqual({r.code for r in reasons}, {"MAINTENANCE"})

    def test_buffer_clearance_extends_occupancy(self):
        svc = build_world()
        booking = svc.request_booking(
            "PITCH-A", "青少年足球训练", 7,
            dt(SAT, 9), dt(SAT, 10), "猎豹青训", 300.0,
        )
        pay_and_confirm(svc, booking)

        # 10:00 整点开场仍在 15 分钟清场缓冲内
        reasons = svc.explain("PITCH-A", dt(SAT, 10), dt(SAT, 11))
        self.assertEqual({r.code for r in reasons}, {"BOOKING"})
        self.assertEqual(reasons[0].detail["buffer_minutes"], 15)

        # 缓冲结束（10:15）之后可约
        self.assertEqual(svc.explain("PITCH-A", dt(SAT, 10, 15), dt(SAT, 11)), [])

        # 缓冲也会计入跨午夜养护：19:00-21:40 的活动 + 20 分钟缓冲正好 22:00 结束
        svc.register_maintenance("PITCH", 5, time(22, 0), 480, "草坪养护", "老王")
        self.assertEqual(svc.explain("PITCH", dt(SAT, 19), dt(SAT, 21, 40)), [])
        reasons = svc.explain("PITCH", dt(SAT, 19), dt(SAT, 21, 45))
        self.assertEqual({r.code for r in reasons}, {"MAINTENANCE"})


class ClosureTest(unittest.TestCase):
    def test_publish_closure_blocks_requests_and_lists_affected(self):
        svc = build_world()
        # 两个青训班都已在封闭发布前完成支付（待审核），同时到场
        paid_bookings = []
        for name in ("猎豹青训", "雏鹰青训"):
            b = svc.request_booking(
                "PITCH", "青少年足球训练", 12,
                dt(SAT, 14), dt(SAT, 16), name, 800.0,
                request_id=f"req-{name}",
            )
            svc.record_payment(f"pay-{name}", b.id, 800.0)
            paid_bookings.append(b)

        result = svc.publish_closure(
            "PITCH", dt(SAT, 13), dt(SAT, 18),
            "紧急草坪养护（补播草籽）", "维护员老王",
        )
        self.assertEqual(set(result["affected_pending"]), {b.id for b in paid_bookings})
        self.assertEqual(result["affected_confirmed"], [])

        # 封闭期间不得新建预约
        with self.assertRaises(BookingConflictError) as ctx:
            svc.request_booking(
                "PITCH", "成人五人制", 10,
                dt(SAT, 15), dt(SAT, 17), "散客联队", 800.0,
            )
        self.assertEqual({r.code for r in ctx.exception.reasons}, {"CLOSURE"})

        # 待审核付费预约也无法确认
        with self.assertRaises(BookingConflictError):
            svc.confirm_booking(paid_bookings[0].id, "运营员小李")

    def test_confirmed_activity_during_closure_can_reschedule_or_cancel(self):
        clock = MutableClock(datetime(2026, 9, 16, 9, 0))
        svc = build_world(clock=clock)
        booking = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            dt(SAT, 14), dt(SAT, 16), "猎豹青训", 800.0,
        )
        pay_and_confirm(svc, booking)

        # 雏鹰班的半场活动从 16:30 开始，恰好避开整场 20 分钟清场缓冲（至 16:20）
        other = svc.request_booking(
            "PITCH-A", "青少年足球训练", 7,
            dt(SAT, 16, 30), dt(SAT, 18), "雏鹰青训", 600.0,
        )
        svc.record_payment("pay-chuying", other.id, 600.0)
        svc.confirm_booking(other.id, "运营员小李")

        result = svc.publish_closure(
            "PITCH", dt(SAT, 13), dt(SAT, 18),
            "紧急草坪养护（补播草籽）", "维护员老王",
        )
        self.assertEqual(set(result["affected_confirmed"]), {booking.id, other.id})

        # 已确认活动不能改到同样封闭的时段，但可改到周日上午
        with self.assertRaises(BookingConflictError):
            svc.reschedule_booking(booking.id, dt(SAT, 16, 30), dt(SAT, 17, 30), "运营员小李")
        v2 = svc.reschedule_booking(booking.id, dt(SUN, 9), dt(SUN, 11), "运营员小李")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.start, dt(SUN, 9))

        # 旧版本仍可查，revision 随版本内容变化
        v1 = svc.get_confirmation(booking.id, version=1)
        self.assertEqual(v1.version, 1)
        self.assertNotEqual(v1.revision, v2.revision)

        # 雏鹰班选择取消：封闭导致 → 全额退款并记录依据
        refund = svc.cancel_booking(
            other.id, "运营员小李",
            caused_by_closure=True, closure_id=result["closure"].id,
        )
        self.assertEqual(refund.code, "CLOSURE_FULL")
        self.assertEqual(refund.amount, 600.0)
        self.assertTrue(refund.caused_by_closure)
        self.assertIn("封闭", refund.basis)

        # 取消后其占用已释放：BOOKING 原因消失（封闭 CLOSURE 仍然存在）
        remaining = {
            r.code for r in svc.explain("PITCH-A", dt(SAT, 16, 30), dt(SAT, 18))
        }
        self.assertNotIn("BOOKING", remaining)
        self.assertIn("CLOSURE", remaining)

    def test_end_closure_early_reopens_zone(self):
        svc = build_world()
        closure = svc.publish_closure(
            "PITCH", dt(SAT, 14), dt(SAT, 18), "设备检修", "维护员老王",
        )["closure"]
        self.assertEqual(
            {r.code for r in svc.explain("PITCH", dt(SAT, 16), dt(SAT, 17))},
            {"CLOSURE"},
        )
        svc.end_closure(closure.id, "维护员老王", at=dt(SAT, 15, 30))

        # 提前结束后，15:30 之后恢复可约；15:00 仍在历史封闭内
        self.assertEqual(svc.explain("PITCH", dt(SAT, 16), dt(SAT, 17)), [])
        self.assertEqual(
            {r.code for r in svc.explain("PITCH", dt(SAT, 14, 30), dt(SAT, 15))},
            {"CLOSURE"},
        )


class RefundRuleTest(unittest.TestCase):
    def _confirmed(self, svc, start: datetime, amount=800.0):
        booking = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            start, start + timedelta(hours=2), "青训班", amount,
        )
        svc.record_payment(f"pay-{booking.id}", booking.id, amount)
        svc.confirm_booking(booking.id, "运营员小李")
        return booking

    def test_self_cancel_refund_ladder(self):
        clock = MutableClock(datetime(2026, 9, 16, 9, 0))
        svc = build_world(clock=clock)

        # 三个同槽位申请先全部提交（未确认之间互不排斥），随后按“现在
        # 时刻”逐个支付、确认、取消，验证退款随提前期变化
        bookings = [
            svc.request_booking(
                "PITCH", "青少年足球训练", 12,
                dt(SAT, 10), dt(SAT, 12), f"青训班{i}", 800.0,
                request_id=f"req-ladder-{i}",
            )
            for i in range(3)
        ]

        svc.record_payment("pay-ladder-0", bookings[0].id, 800.0)
        svc.confirm_booking(bookings[0].id, "运营员小李")
        r = svc.cancel_booking(bookings[0].id, "青训负责人")  # 距开始 >48h
        self.assertEqual((r.code, r.ratio, r.amount), ("SELF_FULL", 1.0, 800.0))

        clock.value = datetime(2026, 9, 18, 4, 0)  # 距周六 10:00 为 30h
        svc.record_payment("pay-ladder-1", bookings[1].id, 800.0)
        svc.confirm_booking(bookings[1].id, "运营员小李")
        r = svc.cancel_booking(bookings[1].id, "青训负责人")
        self.assertEqual((r.code, r.ratio), ("SELF_HALF", 0.5))
        self.assertEqual(r.amount, 400.0)

        clock.value = datetime(2026, 9, 19, 0, 0)  # 距 10:00 为 10h
        svc.record_payment("pay-ladder-2", bookings[2].id, 800.0)
        svc.confirm_booking(bookings[2].id, "运营员小李")
        r = svc.cancel_booking(bookings[2].id, "青训负责人")
        self.assertEqual((r.code, r.ratio, r.amount), ("SELF_NONE", 0.0, 0.0))
        self.assertIsNotNone(r.basis)

    def test_closure_cancel_always_full_regardless_of_lead(self):
        clock = MutableClock(datetime(2026, 9, 19, 9, 0))  # 距 10:00 仅 1h
        svc = build_world(clock=clock)
        booking = self._confirmed(svc, dt(SAT, 10))
        closure = svc.publish_closure(
            "PITCH", dt(SAT, 9, 30), dt(SAT, 12), "临时征用", "老王",
        )["closure"]
        r = svc.cancel_booking(
            booking.id, "运营员小李",
            caused_by_closure=True, closure_id=closure.id,
        )
        self.assertEqual(r.code, "CLOSURE_FULL")
        self.assertEqual(r.amount, 800.0)

    def test_unconfirmed_and_unpaid_cancel(self):
        svc = build_world()
        paid = svc.request_booking(
            "PITCH", "成人五人制", 10, dt(SAT, 8), dt(SAT, 9), "周末队", 400.0,
        )
        svc.record_payment("pay-x", paid.id, 400.0)
        r = svc.cancel_booking(paid.id, "运营员小李")
        self.assertEqual(r.code, "UNCONFIRMED_FULL")
        self.assertEqual(r.amount, 400.0)

        unpaid = svc.request_booking(
            "PITCH", "成人五人制", 10, dt(SAT, 6), dt(SAT, 7), "晨练队", 200.0,
        )
        self.assertIsNone(svc.cancel_booking(unpaid.id, "晨练队"))

        # 重复取消幂等：返回首次退款依据，不重复记账
        again = svc.cancel_booking(paid.id, "运营员小李")
        self.assertEqual(again.code, "UNCONFIRMED_FULL")


class ConcurrencyAndIdempotencyTest(unittest.TestCase):
    def test_concurrent_confirmation_only_one_winner(self):
        svc = build_world()
        # 多个青训班并发抢同一时段：申请与支付都可成功，确认时在锁内裁决
        bookings = []
        for i in range(12):
            b = svc.request_booking(
                "PITCH", "青少年足球训练", 12,
                dt(SAT, 19), dt(SAT, 21), f"青训{i}班", 800.0,
                request_id=f"req-{i}",
            )
            svc.record_payment(f"pay-{i}", b.id, 800.0)
            bookings.append(b)

        barrier = threading.Barrier(len(bookings))
        winners, losers, errors = [], [], []

        def confirm(booking):
            barrier.wait()
            try:
                svc.confirm_booking(booking.id, "运营员小李")
                winners.append(booking.id)
            except BookingConflictError:
                losers.append(booking.id)
            except Exception as exc:  # pragma: no cover - 不允许出现其它异常
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=(b,)) for b in bookings]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 11)

        # 物理占用只有一份：日历中该时段只有一条已确认 booking
        # （其余 11 条为待审核申请，以 PAID 状态展示，不构成占用）
        entries = [
            e for e in svc.calendar(dt(SAT, 18), dt(SAT, 22))
            if e["type"] == "booking" and e["status"] == BookingStatus.CONFIRMED
        ]
        self.assertEqual(len(entries), 1)

        # 败者拿到的解释是 BOOKING 而非笼统失败
        loser = svc.get_booking(losers[0])
        with self.assertRaises(BookingConflictError) as ctx:
            svc.confirm_booking(loser.id, "运营员小李")
        self.assertEqual({r.code for r in ctx.exception.reasons}, {"BOOKING"})

    def test_duplicate_payment_callback_and_confirmation_are_idempotent(self):
        svc = build_world()
        booking = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            dt(SUN, 9), dt(SUN, 11), "猎豹青训", 800.0,
            request_id="req-dup",
        )
        first = svc.record_payment("pay-CH-20260920", booking.id, 800.0)
        # 渠道重复回调：返回同一记录，状态不被改写
        second = svc.record_payment("pay-CH-20260920", booking.id, 800.0)
        self.assertIs(first, second)
        self.assertEqual(len(svc.payments), 1)

        c1 = svc.confirm_booking(booking.id, "运营员小李")
        c2 = svc.confirm_booking(booking.id, "运营员小李")  # 重复确认
        self.assertEqual(c1.version, c2.version)
        self.assertEqual(c1.revision, c2.revision)
        self.assertEqual(len(svc.get_booking(booking.id).history), 1)

        # 同流水号不同金额 → 判定串单
        with self.assertRaises(PaymentMismatchError):
            svc.record_payment("pay-CH-20260920", booking.id, 999.0)

        # 申请幂等键重复提交 → 返回原预约；参数不一致 → 报错
        again = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            dt(SUN, 9), dt(SUN, 11), "猎豹青训", 800.0,
            request_id="req-dup",
        )
        self.assertEqual(again.id, booking.id)
        with self.assertRaises(IdempotencyConflict):
            svc.request_booking(
                "PITCH", "青少年足球训练", 12,
                dt(SUN, 8), dt(SUN, 10), "猎豹青训", 800.0,
                request_id="req-dup",
            )

    def test_restart_does_not_double_occupy(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            svc = build_world(path)
            svc.register_maintenance("PITCH", 5, time(22, 0), 480, "草坪养护", "老王")
            booking = svc.request_booking(
                "PITCH", "青少年足球训练", 12,
                dt(SAT, 19), dt(SAT, 21), "猎豹青训", 800.0,
                request_id="req-persist",
            )
            svc.record_payment("pay-persist", booking.id, 800.0)
            svc.confirm_booking(booking.id, "运营员小李")

            # 模拟服务重启：用同一快照重新构造
            svc2 = VenueService(path)
            self.assertEqual(
                svc2.get_booking(booking.id).status, BookingStatus.CONFIRMED
            )
            # 支付回调在重启后重放
            p = svc2.record_payment("pay-persist", booking.id, 800.0)
            self.assertEqual(p.booking_id, booking.id)
            self.assertEqual(len(svc2.payments), 1)
            # 重复确认返回既有确认单
            c = svc2.confirm_booking(booking.id, "运营员小李")
            self.assertEqual(c.version, 1)

            # 占用仍在：同时段无法再约；养护规则同样恢复
            with self.assertRaises(BookingConflictError) as ctx:
                svc2.request_booking(
                    "PITCH-A", "青少年足球训练", 7,
                    dt(SAT, 19, 30), dt(SAT, 20, 30), "雏鹰青训", 600.0,
                )
            codes = {r.code for r in ctx.exception.reasons}
            self.assertTrue({"BOOKING"} <= codes)

            # 待审核支付状态也能跨重启继续走完审核
            pending = svc2.request_booking(
                "PITCH2", "青少年足球训练", 12,
                dt(SUN, 9), dt(SUN, 11), "滨河队", 800.0,
            )
            svc2.record_payment("pay-pending", pending.id, 800.0)
            svc3 = VenueService(path)
            confirmation = svc3.confirm_booking(pending.id, "运营员小李")
            self.assertEqual(confirmation.version, 1)

            # 重启后改期生成 v2，占用随之迁移
            svc3.reschedule_booking(booking.id, dt(SUN, 13), dt(SUN, 15), "小李")
            self.assertEqual(len(svc3.get_booking(booking.id).history), 2)
            self.assertEqual(
                svc3.explain("PITCH", dt(SAT, 19, 30), dt(SAT, 20, 30)), []
            )
            self.assertEqual(
                {r.code for r in svc3.explain("PITCH", dt(SUN, 14), dt(SUN, 14, 30))},
                {"BOOKING"},
            )
        finally:
            os.unlink(path)

    def test_cancel_then_rebook(self):
        svc = build_world()
        booking = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            dt(SAT, 19), dt(SAT, 21), "猎豹青训", 800.0,
        )
        pay_and_confirm(svc, booking)
        svc.cancel_booking(booking.id, "猎豹青训负责人")

        # 释放后同一时段可被新活动预约
        replacement = svc.request_booking(
            "PITCH", "成人五人制", 10,
            dt(SAT, 19), dt(SAT, 21), "夜猫子队", 800.0,
        )
        pay_and_confirm(svc, replacement)
        self.assertEqual(replacement.status, BookingStatus.CONFIRMED)

        # 原预约已终结，不能再次确认或改期
        with self.assertRaises(BookingStateError):
            svc.confirm_booking(booking.id, "运营员小李")
        with self.assertRaises(BookingStateError):
            svc.reschedule_booking(booking.id, dt(SUN, 9), dt(SUN, 11), "小李")


class CalendarAlternativesAuditTest(unittest.TestCase):
    def test_alternatives_explain_capacity_and_activity_compatibility(self):
        svc = build_world()
        svc.publish_closure(
            "PITCH", dt(SAT, 13), dt(SAT, 18), "草坪养护", "老王",
        )
        result = svc.suggest_alternatives(
            "PITCH", dt(SAT, 14), dt(SAT, 16),
            activity="青少年足球训练", headcount=12,
        )
        viable = {r["zone_id"]: r for r in result["viable"]}
        rejected = {r["zone_id"]: r for r in result["rejected"]}

        # 滨河五人制场：同项目、容量够、时段空闲 → 可替代
        self.assertIn("PITCH2", viable)
        self.assertTrue(viable["PITCH2"]["activity_ok"])
        self.assertTrue(viable["PITCH2"]["capacity_ok"])
        self.assertEqual(viable["PITCH2"]["capacity"], 14)

        # 半场容量只有 7，不够 12 人
        self.assertIn("PITCH-A", rejected)
        self.assertFalse(rejected["PITCH-A"]["capacity_ok"])
        self.assertTrue(any("容量不足" in why for why in rejected["PITCH-A"]["reasons"]))

        # 篮球场项目不兼容
        self.assertIn("BBALL", rejected)
        self.assertFalse(rejected["BBALL"]["activity_ok"])
        self.assertTrue(any("项目不兼容" in why for why in rejected["BBALL"]["reasons"]))

    def test_calendar_lists_all_entry_types(self):
        svc = build_world()
        svc.register_maintenance("PITCH", 5, time(22, 0), 480, "草坪养护", "老王")
        booking = svc.request_booking(
            "PITCH-A", "青少年足球训练", 7,
            dt(SAT, 20), dt(SAT, 21, 30), "猎豹青训", 600.0,
        )
        pay_and_confirm(svc, booking)
        svc.publish_closure("PITCH-B", dt(SAT, 14), dt(SAT, 16), "球门维修", "老王")

        entries = svc.calendar(dt(SAT, 12), dt(SUN, 8))
        by_type = {}
        for e in entries:
            by_type.setdefault(e["type"], []).append(e)

        self.assertTrue(by_type["closure"])
        self.assertTrue(by_type["booking"])
        overnight = [e for e in by_type["maintenance"] if e["crosses_midnight"]]
        self.assertTrue(overnight)
        booking_entry = by_type["booking"][0]
        # 清场缓冲在日历中可见
        self.assertEqual(
            booking_entry["buffer_end"], dt(SAT, 21, 45),
        )

        # 按分区过滤：PITCH-A 能看到整场维护（联动），看不到 PITCH-B 的封闭
        only_a = svc.calendar(dt(SAT, 12), dt(SUN, 8), zone_id="PITCH-A")
        zones_seen = {e["zone_id"] for e in only_a}
        self.assertIn("PITCH", zones_seen)       # 上级维护联动
        self.assertNotIn("PITCH-B", zones_seen)   # 同级半场互不影响

    def test_decision_sources_and_audit_trail(self):
        clock = MutableClock(datetime(2026, 9, 16, 9, 0))
        svc = build_world(clock=clock)
        svc.register_maintenance("PITCH", 5, time(22, 0), 480, "草坪养护", "老王")
        booking = svc.request_booking(
            "PITCH", "青少年足球训练", 12,
            dt(SAT, 14), dt(SAT, 16), "猎豹青训", 800.0,
            request_id="req-audit",
        )
        svc.record_payment("pay-audit", booking.id, 800.0)
        svc.confirm_booking(booking.id, "运营员小李")
        closure = svc.publish_closure(
            "PITCH", dt(SAT, 13), dt(SAT, 18), "紧急养护", "老王",
        )["closure"]
        svc.reschedule_booking(booking.id, dt(SUN, 9), dt(SUN, 11), "运营员小李")
        svc.cancel_booking(
            booking.id, "运营员小李",
            caused_by_closure=True, closure_id=closure.id,
        )

        sources = svc.decision_sources(dt(SAT, 12), dt(SUN, 12))

        # 预约：申请来源、支付、两个版本确认单、退款依据全部可查
        record = next(b for b in sources["bookings"] if b["booking_id"] == booking.id)
        self.assertEqual(record["request_id"], "req-audit")
        self.assertEqual(len(record["confirmations"]), 2)
        self.assertEqual(record["refund"]["code"], "CLOSURE_FULL")
        self.assertEqual(record["refund"]["basis"], svc.get_booking(booking.id).refund.basis)

        self.assertTrue(any(p["payment_id"] == "pay-audit" for p in sources["payments"]))
        self.assertTrue(any(c["closure_id"] == closure.id for c in sources["closures"]))

        maint = sources["maintenance"]
        self.assertTrue(any(m["crosses_midnight"] for m in maint))
        self.assertTrue(
            any(m["start"].startswith("2026-09-19T22:00") for m in maint)
        )

        actions = {e["action"] for e in sources["audit_events"]}
        self.assertTrue(
            {
                "BOOKING_REQUEST", "PAYMENT_RECEIVED", "BOOKING_CONFIRM",
                "CLOSURE_PUBLISH", "BOOKING_RESCHEDULE", "BOOKING_CANCEL",
                "MAINTENANCE_REGISTER",
            }
            <= actions
        )

        # 按预约维度追踪完整生命周期，且流水严格有序
        trail = svc.audit(booking_id=booking.id)
        seq = [e["seq"] for e in trail]
        self.assertEqual(seq, sorted(seq))
        self.assertEqual(
            [e["action"] for e in trail],
            ["BOOKING_REQUEST", "PAYMENT_RECEIVED", "BOOKING_CONFIRM",
             "BOOKING_RESCHEDULE", "BOOKING_CANCEL"],
        )
        cancel_event = trail[-1]
        self.assertEqual(cancel_event["payload"]["refund_code"], "CLOSURE_FULL")
        self.assertEqual(cancel_event["payload"]["refund_amount"], 800.0)


if __name__ == "__main__":
    unittest.main()
