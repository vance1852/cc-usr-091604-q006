"""维护窗口与临时封闭:跨日维护、封闭禁约、受影响预约的改期/取消/恢复、审计来源。"""

import unittest
from datetime import date, datetime, timedelta

from helpers import Clock, UTC, dt, make_service

from app.models import BookingStatus, ClosureStatus
from app.service import ConflictError


def _confirmed(svc, zone="east:left", start=None, end=None,
               customer="青训A", payment_id="pay-1"):
    start = start or dt(9, 19, 18)
    end = end or dt(9, 19, 19)
    booking = svc.create_booking("east", zone, "足球", start, end, customer, 10)
    svc.pay(booking.booking_id, payment_id=payment_id, amount=booking.quoted_price)
    svc.review(booking.booking_id, operator="运营员小王")
    return booking


class MaintenanceWindowTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_weekly_cross_midnight_maintenance(self):
        # 每周日 22:00 养护 4 小时 → 跨午夜至周一 02:00
        self.svc.register_maintenance_window(
            "east", "草坪养护", weekday=6, start_time="22:00",
            duration=timedelta(hours=4), actor="维护员老李")
        # 周一凌晨落在维护窗口内 → 拒绝并说明原因
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 21, 0, 30), dt(9, 21, 1, 30), "青训A", 12)
        self.assertIn("草坪养护", str(ctx.exception))
        # 维护结束后可约
        ok = self.svc.create_booking("east", "east:full", "足球",
                                     dt(9, 21, 2, 30), dt(9, 21, 3, 30), "青训A", 12)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)
        # 每周重复:下周日深夜同样生效
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 27, 23, 0), dt(9, 28, 1, 0), "青训A", 12)
        # 日历在周日与周一都体现该维护窗口
        sun = self.svc.calendar("east", date(2026, 9, 20))
        mon = self.svc.calendar("east", date(2026, 9, 21))
        self.assertTrue(any(e.kind == "maintenance" for e in sun))
        self.assertTrue(any(e.kind == "maintenance" for e in mon))

    def test_once_maintenance_window(self):
        self.svc.register_maintenance_window(
            "east", "灯光检修", start=dt(9, 19, 12), end=dt(9, 19, 14), actor="维护员")
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 19, 13), dt(9, 19, 15), "青训A", 12)
        ok = self.svc.create_booking("east", "east:full", "足球",
                                     dt(9, 19, 14, 15), dt(9, 19, 15, 0), "青训A", 12)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)


class ClosureTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
        self.svc = make_service(now=self.clock)

    def tearDown(self):
        self.svc.close()

    def test_closure_blocks_new_and_marks_existing(self):
        booking = _confirmed(self.svc)  # 青训A 已确认 18:00-19:00
        closure = self.svc.publish_closure(
            "east", zone_ids=(), start=dt(9, 19, 8), end=dt(9, 19, 20),
            reason="草坪养护", actor="维护员老李")
        self.assertEqual(closure.status, ClosureStatus.ACTIVE)
        # 已确认活动被标记为受影响
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.AFFECTED)
        # 封闭期间不得新建预约
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_booking("east", "east:right", "足球",
                                    dt(9, 19, 14), dt(9, 19, 16), "青训B", 8)
        self.assertIn("封闭", str(ctx.exception))
        # 已确认活动按规则取消:封闭原因全额退款并记录依据
        refund = self.svc.cancel(booking.booking_id, actor="运营员小王", reason="closure")
        self.assertEqual(refund.amount, booking.quoted_price)
        self.assertIn("封闭", refund.basis)
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.CANCELLED)
        # 审计:该时间段全部决策来源可查
        actions = [e.action for e in self.svc.audit_trail("east", dt(9, 19, 0), dt(9, 20, 0))]
        for expected in ("booking.created", "booking.paid", "booking.confirmed",
                         "closure.published", "booking.affected",
                         "booking.cancelled", "refund.issued"):
            self.assertIn(expected, actions)

    def test_reschedule_affected_bumps_version(self):
        booking = _confirmed(self.svc)  # 18:00-19:00 已确认 v1
        self.svc.publish_closure("east", zone_ids=(), start=dt(9, 19, 17), end=dt(9, 19, 20),
                                 reason="草坪养护", actor="维护员老李")
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.AFFECTED)
        # 改期到封闭之外 → 确认单版本递增
        conf = self.svc.reschedule(booking.booking_id, dt(9, 20, 10), dt(9, 20, 11),
                                   actor="运营员小王")
        self.assertEqual(conf.version, 2)
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.CONFIRMED)
        # 新时段已被占用
        report = self.svc.explain("east", "east:left", "足球",
                                  dt(9, 20, 10), dt(9, 20, 11), 10)
        self.assertFalse(report.available)

    def test_end_closure_early_restores_booking(self):
        booking = _confirmed(self.svc, start=dt(9, 19, 18), end=dt(9, 19, 19))
        closure = self.svc.publish_closure(
            "east", zone_ids=(), start=dt(9, 19, 8), end=dt(9, 19, 23),
            reason="草坪养护", actor="维护员老李")
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.AFFECTED)
        # 12:00 提前结束封闭 → 18:00 的预约不再受影响,自动恢复
        self.svc.end_closure(closure.closure_id, actor="维护员老李", ended_at=dt(9, 19, 12))
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.CONFIRMED)
        # 封闭实际截止到 12:00,之后时段可约
        ok = self.svc.create_booking("east", "east:right", "足球",
                                     dt(9, 19, 13), dt(9, 19, 14), "散客", 8)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)
        cal = self.svc.calendar("east", date(2026, 9, 19))
        closure_entry = next(e for e in cal if e.kind == "closure")
        self.assertEqual(closure_entry.end, dt(9, 19, 12))

    def test_zone_closure_only_blocks_sharing_zones(self):
        self.svc.publish_closure("east", zone_ids=("east:left",),
                                 start=dt(9, 19, 10), end=dt(9, 19, 12),
                                 reason="局部养护", actor="维护员老李")
        # 右半场资源不重叠 → 可约
        ok = self.svc.create_booking("east", "east:right", "足球",
                                     dt(9, 19, 10), dt(9, 19, 12), "青训B", 8)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)
        # 全场含左半资源 → 冲突
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 19, 10), dt(9, 19, 12), "青训C", 12)
        # 左半场本身 → 冲突
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:left", "足球",
                                    dt(9, 19, 10), dt(9, 19, 12), "青训D", 8)


if __name__ == "__main__":
    unittest.main()
