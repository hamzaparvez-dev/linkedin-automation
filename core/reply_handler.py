"""Inbound classification and next actions (PRD §11)."""

from __future__ import annotations

import re
from typing import Literal, Tuple

ReplyType = Literal["positive", "neutral", "negative", "complex"]


_POSITIVE = re.compile(
    r"\b(yes|yeah|sounds good|interested|let'?s (talk|chat|meet)|schedule|book|call me)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(no thanks|not interested|stop|unsubscribe|remove me|don'?t contact)\b",
    re.I,
)
_COMPLEX = re.compile(
    r"\b(pricing|contract|legal|nda|procurement|rfp|vendor|compliance)\b",
    re.I,
)


def classify_reply_auto(text: str) -> ReplyType:
    """Prefer OpenRouter classification when configured; else regex."""
    try:
        from core.ai_engine import classify_inbound_with_llm

        llm_label = classify_inbound_with_llm(text)
        if llm_label:
            return llm_label
    except Exception:
        pass
    return classify_reply(text)


def classify_reply(text: str) -> ReplyType:
    t = (text or "").strip()
    if not t:
        return "neutral"
    if _COMPLEX.search(t):
        return "complex"
    if _NEGATIVE.search(t):
        return "negative"
    if _POSITIVE.search(t):
        return "positive"
    if len(t) > 400:
        return "complex"
    return "neutral"


def reply_action(reply_type: ReplyType) -> Tuple[str, str]:
    """Returns (action_code, suggested_copy_or_empty). No automated outbound copy."""
    if reply_type == "positive":
        return "human_handoff", ""
    if reply_type == "neutral":
        return "human_handoff", ""
    if reply_type == "negative":
        return "stop", ""
    return "human_review", ""
