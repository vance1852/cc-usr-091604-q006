"""预约主流程:跨午夜、清场缓冲、分区占用、幂等支付/确认、取消再约、退款规则。"""

import unittest
from datetime import date, datetime

from helpers import Clock, UTC, confirmed_booking, dt, make_service

from app.models import BookingStatus
from app.service import ConflictError, StateError


class BookingFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_happy_path_versioned_confirmation(self):
        booking, conf = confirmed_booking(self.svc)
        self.assertEqual(self.svc.get_booking(booking.booking_id).status,
                         BookingStatus.CONFIRMED)
        self.assertEqual(conf.version, 1)
        self.assertEqual(conf.confirmation_id, f"{booking.booking_id}#v1")
        # 日历可见该预约
        entries = self.svc.calendar("east", date(2026, 9, 19))
        self.assertIn(booking.booking_id,
                      [e.ref_id for e in entries if e.kind == "booking"])

    def test_cross_midnight_booking_occupies_both_days(self):
        booking, _ = confirmed_booking(self.svc, start=dt(9, 19, 22), end=dt(9, 20, 1),
                                       payment_id="pay-x")
        day19 = self.svc.calendar("east", date(2026, 9, 19))
        day20 = self.svc.calendar("east", date(2026, 9, 20))
        self.assertIn(booking.booking_id, [e.ref_id for e in day19 if e.kind == "booking"])
        self.assertIn(booking.booking_id, [e.ref_id for e in day20 if e.kind == "booking"])
        # 次日凌晨时段仍被占用
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 20, 0, 30), dt(9, 20, 1, 30), "青训B", 12)
        # 占用到 01:00 + 15分钟清场 = 01:15;01:14 冲突,01:15 起可约
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 20, 1, 14), dt(9, 20, 2, 0), "青训B", 12)
        ok = self.svc.create_booking("east", "east:full", "足球",
                                     dt(9, 20, 1, 15), dt(9, 20, 2, 0), "青训B", 12)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)

    def test_buffer_blocks_back_to_back(self):
        confirmed_booking(self.svc, start=dt(9, 19, 10), end=dt(9, 19, 12))
        # 12:00 结束 + 15分钟清场 → 12:15 前不可约
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 19, 12, 10), dt(9, 19, 13, 0), "散客", 8)
        self.assertIn("清场", str(ctx.exception))
        ok = self.svc.create_booking("east", "east:full", "足球",
                                     dt(9, 19, 12, 15), dt(9, 19, 13, 0), "散客", 8)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)

    def test_zone_occupancy_relation(self):
        confirmed_booking(self.svc, zone="east:left", party=8,
                          start=dt(9, 19, 10), end=dt(9, 19, 12), payment_id="pay-l")
        # 右半场资源单元不重叠 → 可约
        ok = self.svc.create_booking("east", "east:right", "足球",
                                     dt(9, 19, 10), dt(9, 19, 12), "青训B", 8)
        self.assertEqual(ok.status, BookingStatus.PENDING_PAYMENT)
        # 全场含左半资源 → 冲突
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 19, 10), dt(9, 19, 12), "青训C", 12)
        # 反之:全场已约 → 左半场冲突
        confirmed_booking(self.svc, zone="east:full",
                          start=dt(9, 19, 14), end=dt(9, 19, 16), payment_id="pay-f")
        with self.assertRaises(ConflictError):
            self.svc.create_booking("east", "east:left", "足球",
                                    dt(9, 19, 14), dt(9, 19, 16), "青训D", 8)

    def test_capacity_and_sport_validation(self):
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_booking("east", "east:full", "足球",
                                    dt(9, 19, 10), dt(9, 19, 12), "大团", 25)
        self.assertIn("容量", str(ctx.exception))
        with self.assertRaises(ConflictError) as ctx2:
            self.svc.create_booking("east", "east:full", "篮球",
                                    dt(9, 19, 10), dt(9, 19, 12), "篮球队", 10)
        self.assertIn("篮球", str(ctx2.exception))

    def test_duplicate_payment_callback_idempotent(self):
        booking = self.svc.create_booking("east", "east:full", "足球",
                                          dt(9, 19, 10), dt(9, 19, 12), "青训A", 12)
        p1 = self.svc.pay(booking.booking_id, payment_id="pay-dup",
                          amount=booking.quoted_price)
        p2 = self.svc.pay(booking.booking_id, payment_id="pay-dup",
                          amount=booking.quoted_price)
        self.assertEqual(p1.payment_id, p2.payment_id)
        self.assertEqual(self.svc.get_booking(booking.booking_id).status, BookingStatus.PAID)
        # 同一预约换单号重复支付 → 拒绝,不重复入账
        with self.assertRaises(StateError):
            self.svc.pay(booking.booking_id, payment_id="pay-other",
                         amount=booking.quoted_price)

    def test_duplicate_confirm_idempotent(self):
        booking, conf1 = confirmed_booking(self.svc)
        conf2 = self.svc.review(booking.booking_id, operator="运营员小王")
        self.assertEqual(conf2.version, conf1.version)
        self.assertEqual(conf2.confirmation_id, conf1.confirmation_id)

    def test_explain_and_alternatives(self):
        confirmed_booking(self.svc, zone="east:left", party=8,
                          start=dt(9, 19, 10), end=dt(9, 19, 12), payment_id="pay-l")
        report = self.svc.explain("east", "east:left", "足球",
                                  dt(9, 19, 10), dt(9, 19, 12), 8)
        self.assertFalse(report.available)
        self.assertTrue(any(c.kind == "booking" for c in report.conflicts))
        # 替代建议:右半场空闲且兼容;左半场/全场不推荐
        suggestions = self.svc.suggest_alternatives("足球", dt(9, 19, 10), dt(9, 19, 12),
                                                    party_size=8)
        zone_ids = {s.zone_id for s in suggestions}
        self.assertIn("east:right", zone_ids)
        self.assertNotIn("east:left", zone_ids)
        self.assertNotIn("east:full", zone_ids)
        reason = next(s.reason for s in suggestions if s.zone_id == "east:right")
        self.assertIn("容量", reason)
        self.assertIn("足球", reason)

    def test_cancel_then_rebook_frees_slot_and_buffer(self):
        clock = Clock(datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
        svc = make_service(now=clock)
        try:
            booking, _ = confirmed_booking(svc, start=dt(9, 19, 10), end=dt(9, 19, 12))
            refund = svc.cancel(booking.booking_id, actor="客服", reason="customer")
            self.assertEqual(refund.amount, booking.quoted_price)  # 提前18天 → 全额
            self.assertIn("24小时", refund.basis)
            self.assertEqual(svc.get_booking(booking.booking_id).status,
                             BookingStatus.CANCELLED)
            # 原时段(含原清场缓冲 12:00-12:15)可再约
            follow = svc.create_booking("east", "east:full", "足球",
                                        dt(9, 19, 12, 0), dt(9, 19, 13, 0), "散客", 8)
            svc.pay(follow.booking_id, payment_id="pay-2", amount=follow.quoted_price)
            conf = svc.review(follow.booking_id, operator="运营员")
            self.assertEqual(conf.version, 1)
        finally:
            svc.close()

    def test_refund_tiers(self):
        # 开场前22小时取消 → 退50%
        clock = Clock(datetime(2026, 9, 18, 12, 0, tzinfo=UTC))
        svc = make_service(now=clock)
        try:
            booking, _ = confirmed_booking(svc, start=dt(9, 19, 10), end=dt(9, 19, 12))
            refund = svc.cancel(booking.booking_id, actor="客服", reason="customer")
            self.assertEqual(refund.amount, round(booking.quoted_price * 0.5, 2))
            self.assertIn("50%", refund.basis)
        finally:
            svc.close()
        # 开场前2小时取消 → 不退
        clock2 = Clock(datetime(2026, 9, 19, 8, 0, tzinfo=UTC))
        svc2 = make_service(now=clock2)
        try:
            booking2, _ = confirmed_booking(svc2, start=dt(9, 19, 10), end=dt(9, 19, 12))
            refund2 = svc2.cancel(booking2.booking_id, actor="客服", reason="customer")
            self.assertEqual(refund2.amount, 0.0)
            self.assertIn("不予退款", refund2.basis)
        finally:
            svc2.close()


if __name__ == "__main__":
    unittest.main()
