"""Engagement runner: workspace-session launches (no bonusArgument / no sessionCookie)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import config as config_module
from core.accounts_loader import AccountConfig
from core.ai_engine import OutreachResult
from core.engagement_runner import (
    _process_connects,
    _process_dms,
    _send_followup_dm,
)


_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _account(**kwargs: object) -> AccountConfig:
    base = dict(
        account_id="acc_test",
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
        profile_name="Test Identity",
        outreach_copy_mode="llm",
    )
    base.update(kwargs)
    return AccountConfig(**base)  # type: ignore[arg-type]


class TestEngagementRunnerSlot(unittest.TestCase):
    def test_connect_processes_lead_without_busy_check(self) -> None:
        prev_mode = config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "singular"
        try:
            account = _account(phantombuster_connect_agent_id="999")
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
            ):
                m_appr.return_value = MagicMock(allowed=True, reason="")
                _process_connects(conn, pb, account, dry_run=False, sql_limit=10)

            self.assertEqual(pb.run_agent.call_count, 1)
            _args, kwargs = pb.run_agent.call_args
            self.assertIsNone(kwargs.get("bonus_argument"))
            arg = _args[1]
            self.assertNotIn("sessionCookie", arg)
        finally:
            config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = prev_mode

    def test_connect_dedupe_skipped_logs_error_no_invited(self) -> None:
        account = _account(phantombuster_connect_agent_id="999")
        rows = [
            {
                "lead_id": "lead-dedupe",
                "linkedin_url": "https://www.linkedin.com/in/person-dedupe",
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
        pb.run_agent.return_value = (
            {
                "status": "finished",
                "resultObject": {
                    "runtimeEvents": [
                        {"slug": "input-already-processed", "text": "Input is already processed."}
                    ]
                },
            },
            "cid-dedupe",
        )
        pb.fetch_output.return_value = []
        ores = OutreachResult(text="Hi", source="test", raw_llm_snippet="")
        m_log = MagicMock()
        m_transition = MagicMock()
        m_schedule = MagicMock()
        with (
            patch("core.engagement_runner.approve_action") as m_appr,
            patch("core.engagement_runner.pick_strategy", return_value="direct"),
            patch("core.engagement_runner.compose_connect_note", return_value=ores),
            patch("core.engagement_runner.validate_outreach_plaintext", return_value=(True, "")),
            patch("core.engagement_runner.recent_messages_for_repetition", return_value=[]),
            patch("core.engagement_runner.log_action", m_log),
            patch("core.engagement_runner.transition_lead_status", m_transition),
            patch("core.engagement_runner.record_action_executed"),
            patch("core.engagement_runner.record_sent_message"),
            patch("core.engagement_runner.bump_metric"),
            patch("core.engagement_runner.append_message_history"),
            patch("core.engagement_runner.schedule_next_dm_retry_in_days", m_schedule),
            patch("core.engagement_runner._maybe_auto_pause_account_on_pb_auth", return_value=False),
            patch("core.engagement_runner.time.sleep"),
        ):
            m_appr.return_value = MagicMock(allowed=True, reason="")
            _process_connects(conn, pb, account, dry_run=False, sql_limit=10)

        m_log.assert_called_once()
        self.assertEqual(m_log.call_args.kwargs.get("status"), "error")
        self.assertIn("connect_dedupe_skipped", m_log.call_args.kwargs.get("detail", ""))
        m_transition.assert_not_called()
        m_schedule.assert_called_once()

    def test_followup_dm_run_agent_passes_bonus_argument_none(self) -> None:
        account = _account(phantombuster_dm_agent_id="dm-agent-followup-test")
        row = {
            "lead_id": "lead-fu-bonus",
            "linkedin_url": "https://www.linkedin.com/in/testperson",
            "status": "MESSAGED",
            "first_dm_sent_at": "2020-01-01T00:00:00+00:00",
            "next_dm_attempt_at": None,
            "score": 1,
            "created_at": "2020-01-01",
        }
        conn = MagicMock()
        pb = MagicMock()
        pb.run_agent.return_value = ({"status": "finished"}, "cid-fu-1")
        pb.fetch_output.return_value = []
        ores = OutreachResult(text="Short follow-up body for test.", source="test", raw_llm_snippet="")
        with (
            patch("core.engagement_runner.approve_action") as m_appr,
            patch("core.engagement_runner.pick_strategy", return_value="direct"),
            patch("core.engagement_runner.compose_followup_message", return_value=ores),
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
        ):
            m_appr.return_value = MagicMock(allowed=True, reason="")
            _send_followup_dm(
                conn,
                pb,
                account,
                row,
                stage_num=1,
                dry_run=False,
                action_log_label="Test Identity",
                agent_id=account.phantombuster_dm_agent_id or "",
            )

        pb.run_agent.assert_called_once()
        _args, kwargs = pb.run_agent.call_args
        self.assertEqual(_args[0], "dm-agent-followup-test")
        self.assertIsNone(kwargs.get("bonus_argument"))
        self.assertNotIn("sessionCookie", _args[1])

    def test_first_dm_ok_pb_persists_before_log_action(self) -> None:
        account = _account(phantombuster_dm_agent_id="555")
        rows = [
            {
                "lead_id": "lead-dm-ord",
                "linkedin_url": "https://www.linkedin.com/in/person-one",
                "score": 10,
                "created_at": "2020-01-01",
                "status": "CONNECTED",
                "connected_at": "2000-01-01T00:00:00+00:00",
                "invited_at": None,
                "first_dm_sent_at": None,
                "next_dm_attempt_at": None,
            },
        ]
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.execute.return_value = cur
        pb = MagicMock()
        pb.run_agent.return_value = ({"status": "finished"}, "cid-dm-1")
        pb.fetch_output.return_value = []
        ores = OutreachResult(text="Hi", source="test", raw_llm_snippet="")
        order: list[str] = []

        def mark_transition(*_a: object, **_k: object) -> None:
            order.append("transition")

        def mark_log(*_a: object, **_k: object) -> None:
            order.append("log_action")

        with (
            patch("core.engagement_runner.approve_action") as m_appr,
            patch("core.engagement_runner.pick_strategy", return_value="direct"),
            patch("core.engagement_runner.compose_from_template", return_value=ores),
            patch("core.engagement_runner.validate_outreach_plaintext", return_value=(True, "")),
            patch("core.engagement_runner.recent_messages_for_repetition", return_value=[]),
            patch("core.engagement_runner.log_action", side_effect=mark_log),
            patch("core.engagement_runner.transition_lead_status", side_effect=mark_transition),
            patch("core.engagement_runner.record_action_executed"),
            patch("core.engagement_runner.record_sent_message"),
            patch("core.engagement_runner.bump_metric"),
            patch("core.engagement_runner.append_message_history"),
            patch("core.engagement_runner._maybe_auto_pause_account_on_pb_auth", return_value=False),
            patch("core.engagement_runner.time.sleep"),
            patch("core.engagement_runner.validate_dm_message_sender_argument", return_value=(True, "")),
        ):
            m_appr.return_value = MagicMock(allowed=True, reason="")
            _process_dms(conn, pb, account, dry_run=False, sql_limit=10)

        self.assertEqual(order, ["transition", "log_action"])


if __name__ == "__main__":
    unittest.main()
