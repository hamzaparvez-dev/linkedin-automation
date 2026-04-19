"""FOLLOW_UP_SCHEDULE → calendar-day gaps used by engagement_runner."""

from __future__ import annotations

from config import FOLLOW_UP_SCHEDULE, follow_up_eligibility_gaps


def test_default_schedule_matches_timeline() -> None:
    assert FOLLOW_UP_SCHEDULE["dm"] == 1
    assert FOLLOW_UP_SCHEDULE["followup_1"] == 3
    assert FOLLOW_UP_SCHEDULE["followup_2"] == 6
    assert FOLLOW_UP_SCHEDULE["followup_3"] == 11


def test_eligibility_gaps_derived_from_schedule() -> None:
    """After connect → DM; then +2 / +3 / +5 calendar days between outbound stages."""
    assert follow_up_eligibility_gaps() == (1, 2, 3, 5)
