import unittest

from app.venues import Venue, VenueService


class VenueSmokeTest(unittest.TestCase):
    def test_venue_and_health(self):
        self.assertEqual(Venue("东区五人制场", "东区").zone, "东区")
        self.assertEqual(VenueService().health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

