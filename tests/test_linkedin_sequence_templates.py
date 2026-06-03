"""Fixed LinkedIn sequence: substitution only + validation gates."""

from __future__ import annotations

import unittest
from unittest import mock

import core.ai_engine as ai_engine_mod
from core.accounts_loader import AccountConfig
from core.ai_engine import validate_outreach_plaintext
from core.linkedin_sequence_templates import render_fixed_sequence, template_variables
from core.strategy_engine import compose_connect_note, compose_followup_message, compose_from_template


def _fixed_account() -> AccountConfig:
    return AccountConfig(
        account_id="seq_test",
        user_agent="Mozilla/5.0",
        schedule_start="00:00",
        schedule_end="23:59",
        primary_strategy="direct",
        delay_min_sec=30,
        delay_max_sec=60,
        steady_connect_cap=20,
        steady_dm_cap=15,
        steady_reply_cap=10,
        weekend_actions=False,
        phantombuster_connect_agent_id="c1",
        phantombuster_dm_agent_id="d1",
        profile_name="Tester",
        outreach_copy_mode="linkedin_sequence_v1",
    )


_BASE_LEAD: dict = {
    "first_name": "Sam",
    "linkedin_url": "https://www.linkedin.com/in/sam",
}


class TestLinkedinSequenceTemplates(unittest.TestCase):
    """Templates are ~44 words for DM; local .env may set MAX_DM_WORDS lower — use PRD default for gates."""

    def setUp(self) -> None:
        self._prev_max_dm = getattr(ai_engine_mod, "MAX_DM_WORDS", 180)
        ai_engine_mod.MAX_DM_WORDS = 180

    def tearDown(self) -> None:
        ai_engine_mod.MAX_DM_WORDS = self._prev_max_dm

    def test_template_variables_explicit_segment(self) -> None:
        v = template_variables({**_BASE_LEAD, "industry": "AI startups"})
        self.assertEqual(v["segment"], "AI startups")

    def test_template_variables_empty_segment_without_industry(self) -> None:
        v = template_variables(dict(_BASE_LEAD))
        self.assertEqual(v["segment"], "")

    def test_connect_without_segment_validates(self) -> None:
        res = render_fixed_sequence("connect", dict(_BASE_LEAD))
        self.assertEqual(res.source, "fixed_sequence")
        self.assertIn("Would love to connect", res.text)
        self.assertNotIn("working on in ", res.text)
        ok, reason = validate_outreach_plaintext(res.text, stage="connect", recent_bodies=[])
        self.assertTrue(ok, msg=reason)

    def test_connect_with_segment_validates(self) -> None:
        lead = {**_BASE_LEAD, "industry": "SaaS"}
        res = render_fixed_sequence("connect", lead)
        self.assertIn("in SaaS", res.text)
        ok, reason = validate_outreach_plaintext(res.text, stage="connect", recent_bodies=[])
        self.assertTrue(ok, msg=reason)

    def test_dm_and_followups_validate(self) -> None:
        lead = dict(_BASE_LEAD)
        for stage in ("dm", "followup_1", "followup_2"):
            res = render_fixed_sequence(stage, lead)  # type: ignore[arg-type]
            self.assertEqual(res.source, "fixed_sequence")
            ok, reason = validate_outreach_plaintext(res.text, stage=stage, recent_bodies=[])  # type: ignore[arg-type]
            self.assertTrue(ok, msg=f"{stage}:{reason}")

        res3 = render_fixed_sequence("followup_3", lead)
        ok, reason = validate_outreach_plaintext(res3.text, stage="followup_3", recent_bodies=[])
        self.assertTrue(ok, msg=reason)
        self.assertTrue(res3.text.strip().endswith("👍"))

    def test_strategy_engine_uses_fixed_when_account_mode_set(self) -> None:
        acc = _fixed_account()
        r = compose_connect_note(dict(_BASE_LEAD), "direct", account=acc)
        self.assertEqual(r.source, "fixed_sequence")
        r2 = compose_from_template(dict(_BASE_LEAD), "curiosity", recent_bodies=[], account=acc)
        self.assertEqual(r2.source, "fixed_sequence")
        r3 = compose_followup_message(dict(_BASE_LEAD), "value", 2, recent_bodies=[], account=acc)
        self.assertEqual(r3.source, "fixed_sequence")

    def test_strategy_engine_llm_when_account_llm_mode(self) -> None:
        base = _fixed_account()
        acc_llm = AccountConfig(
            account_id=base.account_id,
            user_agent=base.user_agent,
            schedule_start=base.schedule_start,
            schedule_end=base.schedule_end,
            primary_strategy=base.primary_strategy,
            delay_min_sec=base.delay_min_sec,
            delay_max_sec=base.delay_max_sec,
            steady_connect_cap=base.steady_connect_cap,
            steady_dm_cap=base.steady_dm_cap,
            steady_reply_cap=base.steady_reply_cap,
            weekend_actions=base.weekend_actions,
            phantombuster_connect_agent_id=base.phantombuster_connect_agent_id,
            phantombuster_dm_agent_id=base.phantombuster_dm_agent_id,
            profile_name=base.profile_name,
            outreach_copy_mode="llm",
        )
        with mock.patch("core.strategy_engine.generate_outreach_message") as gm:
            from core.ai_engine import OutreachResult

            gm.return_value = OutreachResult(text="ok", source="llm", raw_llm_snippet="")
            compose_connect_note(dict(_BASE_LEAD), "direct", account=acc_llm)
            gm.assert_called_once()

        acc_fixed = _fixed_account()
        with mock.patch("core.strategy_engine.generate_outreach_message") as gm:
            compose_connect_note(dict(_BASE_LEAD), "direct", account=acc_fixed)
            gm.assert_not_called()


class TestLinkedinSequenceEnvOverride(unittest.TestCase):
    def test_use_fixed_when_env_set(self) -> None:
        acc_llm = AccountConfig(
            account_id="seq_test",
            user_agent="Mozilla/5.0",
            schedule_start="00:00",
            schedule_end="23:59",
            primary_strategy="direct",
            delay_min_sec=30,
            delay_max_sec=60,
            steady_connect_cap=20,
            steady_dm_cap=15,
            steady_reply_cap=10,
            weekend_actions=False,
            phantombuster_connect_agent_id="c1",
            phantombuster_dm_agent_id="d1",
            profile_name="Tester",
            outreach_copy_mode="llm",
        )
        with (
            mock.patch("core.strategy_engine.USE_FIXED_LINKEDIN_SEQUENCE", True),
            mock.patch("core.strategy_engine.generate_outreach_message") as gm,
        ):
            r = compose_connect_note(dict(_BASE_LEAD), "direct", account=acc_llm)
            self.assertEqual(r.source, "fixed_sequence")
            gm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
