"""Unit tests for outreach validation, regeneration, and static fallback."""

from __future__ import annotations

import unittest
from unittest import mock

from core.ai_engine import generate_outreach_message, validate_outreach_plaintext


class TestAiOutreach(unittest.TestCase):
    def test_validate_rejects_link(self) -> None:
        ok, reason = validate_outreach_plaintext(
            "Quick question https://example.com/x",
            stage="dm",
            recent_bodies=[],
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "link")

    def test_validate_rejects_sales_language(self) -> None:
        ok, reason = validate_outreach_plaintext(
            "We help teams like yours ship faster.",
            stage="dm",
            recent_bodies=[],
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "sales_language")

    def test_validate_rejects_word_limit_followup_3(self) -> None:
        text = " ".join([f"w{i}" for i in range(200)])
        ok, reason = validate_outreach_plaintext(text, stage="followup_3", recent_bodies=[])
        self.assertFalse(ok)
        self.assertEqual(reason, "too_long")

    def test_validate_rejects_word_limit_connect(self) -> None:
        text = " ".join([f"w{i}" for i in range(250)])
        ok, reason = validate_outreach_plaintext(text, stage="connect", recent_bodies=[])
        self.assertFalse(ok)
        self.assertEqual(reason, "too_long")

    def test_validate_rejects_too_many_chars_connect(self) -> None:
        t = "a" * 201
        ok, reason = validate_outreach_plaintext(t, stage="connect", recent_bodies=[])
        self.assertFalse(ok)
        self.assertEqual(reason, "too_many_chars")

    def test_validate_followup_3_allows_optional_trailing_thumbsup(self) -> None:
        t = "Hey Jan, one last nudge on my side — if timing is off, no worries. 👍"
        ok, _ = validate_outreach_plaintext(t, stage="followup_3", recent_bodies=[])
        self.assertTrue(ok)

    @mock.patch("core.ai_engine.OPEN_ROUTER_API_KEY", "")
    def test_fallback_when_no_openrouter_key(self) -> None:
        lead = {"first_name": "Alex", "company_name": "Acme Labs", "industry": "SaaS"}
        res = generate_outreach_message("connect", lead, "direct")
        self.assertEqual(res.source, "fallback_no_key")
        self.assertTrue(res.text.strip())
        ok, _ = validate_outreach_plaintext(res.text, stage="connect", recent_bodies=[])
        self.assertTrue(ok)

    def test_regeneration_path(self) -> None:
        calls: list[int] = []

        def fake_chat(messages: list, max_tokens: int = 120) -> str:
            calls.append(1)
            if len(calls) == 1:
                return "We help you scale with our service."
            return "Alex what are you prioritizing at Acme this quarter"

        lead = {"first_name": "Alex", "company_name": "Acme", "industry": "SaaS"}
        with (
            mock.patch("core.ai_engine.OPEN_ROUTER_API_KEY", "dummy-key"),
            mock.patch("core.ai_engine._chat", fake_chat),
        ):
            res = generate_outreach_message("dm", lead, "direct", recent_bodies=[])
        self.assertEqual(len(calls), 2)
        self.assertEqual(res.source, "llm_regen")
        ok, _ = validate_outreach_plaintext(res.text, stage="dm", recent_bodies=[])
        self.assertTrue(ok)

    def test_double_llm_failure_uses_fallback(self) -> None:
        lead = {"first_name": "Sam", "company_name": "Co", "industry": "defi"}
        with (
            mock.patch("core.ai_engine.OPEN_ROUTER_API_KEY", "dummy-key"),
            mock.patch(
                "core.ai_engine._chat",
                lambda *a, **k: "Book a call — our team can help with guaranteed results!!!",
            ),
        ):
            res = generate_outreach_message("dm", lead, "value", recent_bodies=[])
        self.assertEqual(res.source, "fallback")
        ok, _ = validate_outreach_plaintext(res.text, stage="dm", recent_bodies=[])
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
