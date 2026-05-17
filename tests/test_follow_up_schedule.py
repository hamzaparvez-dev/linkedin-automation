"""FOLLOW_UP_SCHEDULE → calendar-day gaps used by engagement_runner."""

from __future__ import annotations

import unittest

from config import FOLLOW_UP_SCHEDULE, follow_up_eligibility_gaps


class TestFollowUpSchedule(unittest.TestCase):
    def test_default_schedule_matches_timeline(self) -> None:
        self.assertEqual(FOLLOW_UP_SCHEDULE["dm"], 1)
        self.assertEqual(FOLLOW_UP_SCHEDULE["followup_1"], 2)
        self.assertEqual(FOLLOW_UP_SCHEDULE["followup_2"], 5)
        self.assertEqual(FOLLOW_UP_SCHEDULE["followup_3"], 9)

    def test_eligibility_gaps_derived_from_schedule(self) -> None:
        self.assertEqual(follow_up_eligibility_gaps(), (1, 1, 3, 4))


if __name__ == "__main__":
    unittest.main()
