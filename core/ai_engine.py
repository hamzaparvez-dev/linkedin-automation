"""Controlled LLM-assisted variation via OpenRouter-compatible API (PRD §10)."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Optional

import requests

from config import OPEN_ROUTER_API_KEY, OPEN_ROUTER_BASE_URL, OPEN_ROUTER_MODEL

logger = logging.getLogger(__name__)

OutreachStage = Literal["connect", "dm", "followup_1", "followup_2", "followup_3"]
ReplyLabel = Literal["positive", "neutral", "negative", "complex"]


OUTREACH_SYSTEM_PROMPT = """You are a Web3 founder sending LinkedIn outreach.

Write short, natural messages.
Do NOT sound like sales.
Do NOT use buzzwords.
Do NOT pitch services.

Rules:
- Max 20 words (connect)
- Max 25 words (DM / follow-up)
- No links
- No emojis
- No long sentences
- One idea only

Tone:
- casual
- curious
- peer-to-peer

Goal:
Start a conversation, not sell."""

_STAGE_INSTRUCTIONS: dict[str, str] = {
    "connect": (
        "Stage CONNECT: ask to connect; mention the company or their space. "
        "No question required. Output only the connection note text, nothing else."
    ),
    "dm": (
        "Stage DM: one simple question; focus on growth or product. "
        "Output only the DM text, nothing else."
    ),
    "followup_1": (
        "Stage FOLLOWUP_1: light nudge; refer to having reached out before; no pressure. "
        "Output only the message text, nothing else."
    ),
    "followup_2": (
        "Stage FOLLOWUP_2: another light check-in; still casual; not the last message in the sequence. "
        "Output only the message text, nothing else."
    ),
    "followup_3": (
        "Stage FOLLOWUP_3: absolute final message; polite soft close (e.g. should I assume no interest?). "
        "Output only the message text, nothing else."
    ),
}

# Static fallbacks when LLM is unavailable or fails validation twice.
_FALLBACK: dict[tuple[str, str], list[str]] = {
    ("connect", "direct"): [
        "Hi {first_name}, saw your work at {company} — would be good to connect.",
        "Hi {first_name}, building in {signal} too. Happy to connect.",
    ],
    ("connect", "curiosity"): [
        "Hi {first_name}, came across {company} in {signal} — would love to connect.",
        "Hi {first_name}, curious about what you're building at {company}. Open to connect?",
    ],
    ("connect", "value"): [
        "Hi {first_name}, noticed {company} in {signal} — thought a peer connect made sense.",
        "Hi {first_name}, {signal} founder here — would enjoy connecting.",
    ],
    ("dm", "direct"): [
        "Hey {first_name}, are you focused more on growth or product at {company} right now?",
    ],
    ("dm", "curiosity"): [
        "Hey {first_name}, quick one — how are you thinking about growth at {company} lately?",
    ],
    ("dm", "value"): [
        "Hey {first_name}, curious how you're approaching traction at {company} these days?",
    ],
    ("followup_1", "direct"): [
        "Hey {first_name}, wanted to follow up on my note — still open to connect when you have a moment.",
    ],
    ("followup_1", "curiosity"): [
        "Hey {first_name}, circling back — curious if a short connect still makes sense on your side.",
    ],
    ("followup_1", "value"): [
        "Hey {first_name}, gentle bump from my side — happy to compare notes if timing works.",
    ],
    ("followup_2", "direct"): [
        "Hey {first_name}, checking in again — still open to a quick exchange when you have a moment?",
    ],
    ("followup_2", "curiosity"): [
        "Hey {first_name}, curious if timing loosened at all for a short peer chat on your side?",
    ],
    ("followup_2", "value"): [
        "Hey {first_name}, one more ping — still happy to compare notes if useful on your side.",
    ],
    ("followup_3", "direct"): [
        "Hey {first_name}, last note — should I assume you're swamped and close this out?",
    ],
    ("followup_3", "curiosity"): [
        "Hey {first_name}, final follow-up — want me to leave it here or is a quick reply worth it?",
    ],
    ("followup_3", "value"): [
        "Hey {first_name}, closing the loop on my side unless you'd like to pick this up later.",
    ],
}

_SALES_BANNED = re.compile(
    r"\b(we help|our service|book a call|schedule a demo|free consultation|"
    r"my team|our team|solution we|guaranteed results)\b",
    re.IGNORECASE,
)
_EMOJI_RANGE = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001F600-\U0001F64F\U0001F1E6-\U0001F1FF]"
)


@dataclass(frozen=True)
class OutreachResult:
    """Final outbound copy + provenance for logging."""

    text: str
    source: str  # llm | llm_regen | fallback | fallback_no_key
    raw_llm_snippet: str


def industry_signal(lead: dict[str, Any]) -> str:
    blob = " ".join(
        [
            str(lead.get("industry") or ""),
            str(lead.get("title") or ""),
            str(lead.get("company_name") or ""),
        ]
    ).lower()
    for kw in ("defi", "nft", "web3", "blockchain", "crypto", "infrastructure", "saas", "b2b", "protocol"):
        if kw in blob:
            return kw
    return "Web3"


def _normalize_strategy(strategy: str) -> str:
    s = (strategy or "direct").strip().lower()
    if s in ("direct", "curiosity", "value"):
        return s
    if s in ("question", "observation"):
        return "curiosity"
    return "direct"


def _max_words_for_stage(stage: OutreachStage) -> int:
    return 20 if stage == "connect" else 25


def _has_emoji(s: str) -> bool:
    if _EMOJI_RANGE.search(s):
        return True
    for ch in s:
        try:
            if unicodedata.name(ch).startswith("EMOJI"):
                return True
        except ValueError:
            continue
    return False


def _has_link_like(s: str) -> bool:
    sl = s.lower()
    if "http://" in sl or "https://" in sl:
        return True
    if re.search(r"\bwww\.\S+", sl):
        return True
    return False


def validate_outreach_plaintext(
    text: str,
    *,
    stage: OutreachStage,
    recent_bodies: list[str],
) -> tuple[bool, str]:
    mw = _max_words_for_stage(stage)
    if not text or not text.strip():
        return False, "empty"
    t = text.strip()
    words = t.split()
    if len(words) > mw:
        return False, "too_long"
    if _has_link_like(t):
        return False, "link"
    if _SALES_BANNED.search(t):
        return False, "sales_language"
    if _has_emoji(t):
        return False, "emoji"
    ok, reason = validate_message(t, max_words=mw, recent_bodies=recent_bodies, max_url_count=0)
    if not ok:
        return False, reason
    return True, ""


def _fallback_body(stage: OutreachStage, strategy: str, lead: dict[str, Any]) -> str:
    strat = _normalize_strategy(strategy)
    first = (lead.get("first_name") or lead.get("full_name") or "there").split()[0]
    company = lead.get("company_name") or "your company"
    signal = industry_signal(lead)
    key = (stage, strat)
    pool = _FALLBACK.get(key) or _FALLBACK.get((stage, "direct")) or [
        "Hi {first_name}, would be great to connect."
    ]
    import random

    tpl = random.choice(pool)
    return tpl.format(first_name=first, company=company, signal=signal)


def _build_user_prompt(
    lead: dict[str, Any],
    stage: OutreachStage,
    strategy: str,
    *,
    regen_hint: str = "",
) -> str:
    first = (lead.get("first_name") or lead.get("full_name") or "there").split()[0]
    company = lead.get("company_name") or "your company"
    industry_signal_val = lead.get("industry_signal") or industry_signal(lead)
    strat = _normalize_strategy(strategy)
    base = (
        f"Name: {first}\n"
        f"Company: {company}\n"
        f"Industry: {industry_signal_val}\n"
        f"Stage: {stage}\n"
        f"Strategy: {strat}\n\n"
        f"{_STAGE_INSTRUCTIONS[stage]}"
    )
    if regen_hint:
        base += f"\n\nYour previous reply was invalid: {regen_hint}. Rewrite fully."
    return base


def _chat(messages: list[dict[str, str]], max_tokens: int = 120) -> str:
    if not OPEN_ROUTER_API_KEY:
        return ""
    url = f"{OPEN_ROUTER_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPEN_ROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPEN_ROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()


def generate_outreach_message(
    stage: OutreachStage,
    lead: dict[str, Any],
    strategy: str,
    *,
    recent_bodies: Optional[list[str]] = None,
) -> OutreachResult:
    """
    LLM-first outreach copy with one regeneration on validation failure, then static fallback.
    """
    recent = recent_bodies or []
    strat = _normalize_strategy(strategy)
    raw_snip = ""

    if not OPEN_ROUTER_API_KEY:
        fb = _fallback_body(stage, strat, lead)
        return OutreachResult(text=fb, source="fallback_no_key", raw_llm_snippet="")

    def _one_call(regen_hint: str = "") -> str:
        user = _build_user_prompt(lead, stage, strat, regen_hint=regen_hint)
        return _chat(
            [
                {"role": "system", "content": OUTREACH_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=100 if stage == "connect" else 120,
        )

    text = _one_call()
    raw_snip = (text or "")[:600]
    ok, reason = validate_outreach_plaintext(text, stage=stage, recent_bodies=recent)
    source = "llm"
    if not ok:
        text2 = _one_call(regen_hint=reason)
        raw_snip = (text2 or "")[:600]
        ok2, reason2 = validate_outreach_plaintext(text2, stage=stage, recent_bodies=recent)
        if ok2:
            text = text2
            source = "llm_regen"
        else:
            text = _fallback_body(stage, strat, lead)
            source = "fallback"
            raw_snip = (raw_snip + f" | regen_fail:{reason2}")[:600]
    else:
        text = text.strip()

    if not text:
        text = _fallback_body(stage, strat, lead)
        source = "fallback"

    return OutreachResult(text=text.strip(), source=source, raw_llm_snippet=raw_snip)


def generate_dm_variation(
    *,
    strategy: str,
    lead: dict[str, Any],
    recent_openings: list[str],
    max_words: int,
) -> str:
    """Legacy path; prefer generate_outreach_message(stage='dm', ...)."""
    res = generate_outreach_message("dm", lead, strategy, recent_bodies=recent_openings)
    words = res.text.split()[: max_words or 25]
    return " ".join(words)


_BANNED = re.compile(
    r"\b(guarantee|100%|!!!+|click here|limited time|act now)\b",
    re.IGNORECASE,
)


def validate_message(
    text: str,
    *,
    max_words: int,
    recent_bodies: list[str],
    max_url_count: int = 1,
) -> tuple[bool, str]:
    if not text or not text.strip():
        return False, "empty"
    words = text.split()
    if len(words) > max_words:
        return False, "too_long"
    if _BANNED.search(text):
        return False, "banned_pattern"
    if text.count("http") > max_url_count:
        return False, "too_many_urls"
    if text.count("!") > 4:
        return False, "too_many_bang"
    prefix = " ".join(words[:5]).lower()
    for prev in recent_bodies[:20]:
        prev_w = prev.split()[:5]
        if prev_w and prefix == " ".join(prev_w).lower():
            return False, "repetitive_opening"
    return True, ""


def classify_inbound_with_llm(text: str) -> Optional[ReplyLabel]:
    """Return structured label from LLM, or None to fall back to regex."""
    if not OPEN_ROUTER_API_KEY or not (text or "").strip():
        return None
    system = (
        "You classify a short LinkedIn reply. Reply with exactly one lowercase word, "
        "no punctuation: positive, neutral, negative, or complex. "
        "complex = legal/pricing/vendor or needs a human."
    )
    user = f"Message:\n{(text or '')[:2000]}"
    try:
        out = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=8,
        )
        w = (out or "").strip().lower().split()[0] if out else ""
        w = w.rstrip(".,;:!?")
        if w in ("positive", "neutral", "negative", "complex"):
            return w  # type: ignore[return-value]
    except Exception as e:
        logger.warning("Inbound LLM classify failed: %s", e)
    return None
