"""Offline tests for schedule math and state handling (no network)."""
import unittest

import recurring_agent as ra


class ScheduleTests(unittest.TestCase):
    def test_weekly(self):
        s = {"start": "2026-10-14T20:30:00+00:00", "every": "weekly"}
        self.assertEqual([ra.period_key(s, k) for k in range(3)], ["2026-10-14", "2026-10-21", "2026-10-28"])

    def test_monthly_clamps_to_month_end(self):
        s = {"start": "2026-01-31T09:00:00+00:00", "every": "monthly"}
        self.assertEqual([ra.period_key(s, k) for k in range(4)], ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"])
        s = {"start": "2026-11-15T09:00:00+00:00", "every": "monthly"}
        self.assertEqual(ra.period_key(s, 2), "2027-01-15")

    def test_timezone_required(self):
        with self.assertRaises(ValueError):
            ra.parse_dt("2026-10-15T09:00")
        self.assertEqual(ra.parse_dt("2026-10-15T12:00+03:00").hour, 9)


if __name__ == "__main__":
    unittest.main()
