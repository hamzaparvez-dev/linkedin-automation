"""Engagement runner: per-account UA for bonusArgument + connect processing."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import config as config_module
from core.accounts_loader import AccountConfig
from core.ai_engine import OutreachResult
from core.engagement_runner import _engagement_bonus_argument, _process_connects


_FAKE_SESSION = "AQED" + "x" * 100
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _account(**kwargs: object) -> AccountConfig:
    base = dict(
        account_id="acc_test",
        linkedin_profile=_FAKE_SESSION,
        user_agent=_UA,
        schedule_start="00:00",
        schedule_end="23:59",
        primary_strategy="direct",
        delay_min_sec=1,
        delay_max_sec=2,
        steady_connect_cap=20,
        steady_dm_cap=15,
        steady_reply_cap=10,
        weekend_actions=False,
        phantombuster_connect_agent_id="111",
        phantombuster_dm_agent_id="222",
        profile_name="",
    )
    base.update(kwargs)
    return AccountConfig(**base)  # type: ignore[arg-type]


class TestEngagementRunnerSlot(unittest.TestCase):
    def test_engagement_bonus_argument_uses_per_account_ua(self) -> None:
        bonus = _engagement_bonus_argument(_FAKE_SESSION, "Mozilla/5.0 (custom)")
        self.assertIsNotNone(bonus)
        assert bonus is not None
        self.assertIn("Mozilla/5.0 (custom)", bonus["userAgent"])

    def test_connect_processes_lead_without_busy_check(self) -> None:
        prev_mode = config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE
        prev_ua = getattr(config_module, "PHANTOMBUSTER_USER_AGENT", "") or ""
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "singular"
        config_module.PHANTOMBUSTER_USER_AGENT = _UA
        try:
            account = _account(
                phantombuster_connect_agent_id="999",
                linkedin_profile=_FAKE_SESSION,
                user_agent=_UA,
            )
            rows = [
                {
                    "lead_id": "lead-1",
                    "linkedin_url": "https://www.linkedin.com/in/person-one",
                    "score": 10,
                    "created_at": "2020-01-01",
                    "status": "ASSIGNED_TO_ACCOUNT",
                },
            ]
            cur = MagicMock()
            cur.fetchall.return_value = rows
            conn = MagicMock()
            conn.execute.return_value = cur
            pb = MagicMock()
            pb.run_agent.return_value = ({"status": "finished"}, "cid-1")
            pb.fetch_output.return_value = []
            ores = OutreachResult(text="Hi", source="test", raw_llm_snippet="")
            with (
                patch("core.engagement_runner.approve_action") as m_appr,
                patch("core.engagement_runner.pick_strategy", return_value="direct"),
                patch("core.engagement_runner.compose_connect_note", return_value=ores),
                patch("core.engagement_runner.validate_outreach_plaintext", return_value=(True, "")),
                patch("core.engagement_runner.recent_messages_for_repetition", return_value=[]),
                patch("core.engagement_runner.log_action"),
                patch("core.engagement_runner.transition_lead_status"),
                patch("core.engagement_runner.record_action_executed"),
                patch("core.engagement_runner.record_sent_message"),
                patch("core.engagement_runner.bump_metric"),
                patch("core.engagement_runner.append_message_history"),
                patch("core.engagement_runner._maybe_auto_pause_account_on_pb_auth", return_value=False),
                patch("core.engagement_runner.time.sleep"),
                patch("core.engagement_runner.get_account_linkedin_profile", return_value=_FAKE_SESSION),
                patch("core.engagement_runner.get_account_user_agent", return_value=""),
            ):
                m_appr.return_value = MagicMock(allowed=True, reason="")
                _process_connects(conn, pb, account, dry_run=False, sql_limit=10)

            self.assertEqual(pb.run_agent.call_count, 1)
        finally:
            config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = prev_mode
            config_module.PHANTOMBUSTER_USER_AGENT = prev_ua


if __name__ == "__main__":
    unittest.main()
