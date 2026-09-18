"""并发锁定行为:多线程抢同一时段,支付/确认串行化,绝不产生双重占用。"""

import threading
import unittest

from helpers import dt, make_service

from app.models import OCCUPYING_STATUSES, BookingStatus
from app.service import ConflictError

THREADS = 8


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def _race(self, worker):
        barrier = threading.Barrier(THREADS)

        def wrapped(i):
            barrier.wait(timeout=10)
            worker(i)

        threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertTrue(all(not t.is_alive() for t in threads), "有线程未在限时内完成")

    def test_concurrent_pay_single_occupant(self):
        """同一时段多人同时支付:只有一人能锁定,其余收到冲突。"""
        start, end = dt(9, 19, 10), dt(9, 19, 12)
        bookings = [self.svc.create_booking("east", "east:full", "足球",
                                            start, end, f"青训{i}", 12)
                    for i in range(THREADS)]
        paid, conflicts = [], []

        def worker(i):
            try:
                self.svc.pay(bookings[i].booking_id, payment_id=f"pay-{i}")
                paid.append(i)
            except ConflictError:
                conflicts.append(i)

        self._race(worker)
        self.assertEqual(len(paid), 1)
        self.assertEqual(len(conflicts), THREADS - 1)
        occupying = self.svc.list_bookings("east", statuses=list(OCCUPYING_STATUSES))
        self.assertEqual(len(occupying), 1)
        self.assertEqual(occupying[0].status, BookingStatus.PAID)

    def test_concurrent_full_flow_no_double_occupancy(self):
        """完整流程(申请→支付→确认)并发:最终只有一笔确认占用。"""
        start, end = dt(9, 19, 14), dt(9, 19, 16)
        confirmed, failed = [], []

        def worker(i):
            try:
                booking = self.svc.create_booking("east", "east:full", "足球",
                                                  start, end, f"客户{i}", 12)
                self.svc.pay(booking.booking_id, payment_id=f"pay-x-{i}")
                self.svc.review(booking.booking_id, operator="运营员")
                confirmed.append(booking.booking_id)
            except ConflictError:
                failed.append(i)

        self._race(worker)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(failed), THREADS - 1)
        occupying = self.svc.list_bookings("east", statuses=list(OCCUPYING_STATUSES))
        self.assertEqual(len(occupying), 1)
        self.assertEqual(occupying[0].status, BookingStatus.CONFIRMED)
        # 确认单只有一个版本,没有重复确认
        conf = self.svc.confirmation_of(confirmed[0])
        self.assertEqual(conf.version, 1)


if __name__ == "__main__":
    unittest.main()
