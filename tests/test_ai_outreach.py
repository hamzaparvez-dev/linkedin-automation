"""Unit tests for outreach validation, regeneration, and static fallback."""

from __future__ import annotations

import pytest

from core.ai_engine import generate_outreach_message, validate_outreach_plaintext


def test_validate_rejects_link() -> None:
    ok, reason = validate_outreach_plaintext(
        "Quick question https://example.com/x",
        stage="dm",
        recent_bodies=[],
    )
    assert ok is False
    assert reason == "link"


def test_validate_rejects_sales_language() -> None:
    ok, reason = validate_outreach_plaintext(
        "We help teams like yours ship faster.",
        stage="dm",
        recent_bodies=[],
    )
    assert ok is False
    assert reason == "sales_language"


def test_validate_rejects_word_limit_followup_3() -> None:
    text = " ".join([f"w{i}" for i in range(30)])
    ok, reason = validate_outreach_plaintext(text, stage="followup_3", recent_bodies=[])
    assert ok is False
    assert reason == "too_long"


def test_validate_rejects_word_limit_connect() -> None:
    text = " ".join([f"w{i}" for i in range(25)])
    ok, reason = validate_outreach_plaintext(text, stage="connect", recent_bodies=[])
    assert ok is False
    assert reason == "too_long"


def test_fallback_when_no_openrouter_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.ai_engine.OPEN_ROUTER_API_KEY", "")
    lead = {"first_name": "Alex", "company_name": "Acme Labs", "industry": "SaaS"}
    res = generate_outreach_message("connect", lead, "direct")
    assert res.source == "fallback_no_key"
    assert res.text.strip()
    ok, _ = validate_outreach_plaintext(res.text, stage="connect", recent_bodies=[])
    assert ok is True


def test_regeneration_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.ai_engine.OPEN_ROUTER_API_KEY", "dummy-key")
    calls: list[int] = []

    def fake_chat(messages: list, max_tokens: int = 120) -> str:
        calls.append(1)
        if len(calls) == 1:
            return "We help you scale with our service."  # fails validation
        return "Alex what are you prioritizing at Acme this quarter"

    monkeypatch.setattr("core.ai_engine._chat", fake_chat)
    lead = {"first_name": "Alex", "company_name": "Acme", "industry": "SaaS"}
    res = generate_outreach_message("dm", lead, "direct", recent_bodies=[])
    assert len(calls) == 2
    assert res.source == "llm_regen"
    ok, _ = validate_outreach_plaintext(res.text, stage="dm", recent_bodies=[])
    assert ok is True


def test_double_llm_failure_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.ai_engine.OPEN_ROUTER_API_KEY", "dummy-key")
    monkeypatch.setattr(
        "core.ai_engine._chat",
        lambda *a, **k: "Book a call — our team can help with guaranteed results!!!",
    )
    lead = {"first_name": "Sam", "company_name": "Co", "industry": "defi"}
    res = generate_outreach_message("dm", lead, "value", recent_bodies=[])
    assert res.source == "fallback"
    ok, _ = validate_outreach_plaintext(res.text, stage="dm", recent_bodies=[])
    assert ok is True
