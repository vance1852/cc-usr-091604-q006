"""重启持久化:状态落库,服务重启后占用、幂等与审计不丢。"""

import os
import tempfile
import unittest

from helpers import dt, make_service

from app.service import ConflictError, VenueService


class PersistenceTest(unittest.TestCase):
    def test_restart_preserves_occupancy_and_idempotency(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "venue.db")
            svc = make_service(path)
            booking = svc.create_booking("east", "east:full", "足球",
                                         dt(9, 19, 10), dt(9, 19, 12), "青训A", 12)
            svc.pay(booking.booking_id, payment_id="pay-1", amount=booking.quoted_price)
            conf = svc.review(booking.booking_id, operator="运营员")
            svc.close()

            # 模拟服务重启:同一数据库文件重新打开
            reopened = VenueService(db_path=path)
            try:
                # 占用仍在:同时段新建被拒
                with self.assertRaises(ConflictError):
                    reopened.create_booking("east", "east:full", "足球",
                                            dt(9, 19, 11), dt(9, 19, 13), "青训B", 12)
                # 重复支付回调:返回原记录,不重复入账
                again = reopened.pay(booking.booking_id, payment_id="pay-1")
                self.assertEqual(again.booking_id, booking.booking_id)
                self.assertEqual(again.amount, booking.quoted_price)
                # 重复确认:版本不变,不产生新确认单
                conf2 = reopened.review(booking.booking_id, operator="运营员")
                self.assertEqual(conf2.version, conf.version)
                # 审计来源完整保留
                actions = [e.action for e in reopened.booking_history(booking.booking_id)]
                for expected in ("booking.created", "booking.paid", "booking.confirmed"):
                    self.assertIn(expected, actions)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
