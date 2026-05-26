"""Controlled LLM-assisted variation via OpenRouter-compatible API (PRD §10)."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Optional

import requests

from config import (
    MAX_CONNECT_NOTE_CHARS,
    MAX_CONNECT_NOTE_WORDS,
    MAX_DM_WORDS,
    OPEN_ROUTER_API_KEY,
    OPEN_ROUTER_BASE_URL,
    OPEN_ROUTER_MODEL,
    POST_TEXT_LLM_MAX_CHARS,
)

logger = logging.getLogger(__name__)

OutreachStage = Literal["connect", "dm", "followup_1", "followup_2", "followup_3"]
ReplyLabel = Literal["positive", "neutral", "negative", "complex"]


OUTREACH_SYSTEM_PROMPT = f"""You write highly personalized LinkedIn outreach for an AI-native cinematic
content and storytelling studio. The studio helps AI startups, SaaS companies, founder-led brands,
and media-first businesses with cinematic AI-generated launch videos, founder-led storytelling,
short-form visual storytelling, and AI-native brand content.

Produce fully personalized copy grounded in the facts in the user message. Vary wording every lead.
Never paste a template line verbatim. Tone: founder-to-founder, intelligent, conversational,
naturally curious — not salesy, not corporate, not agency-like. Goal: start relevant conversations,
create curiosity, establish relevance, and softly qualify — not hard-sell immediately.

Personalization priority (when provided in the user message):
1. Post text (most important) — reference the ACTUAL idea, insight, or founder take. Never say
   "great post" or generic praise.
2. Headline — use to understand company/category and positioning.
3. Role — use for tone and context.

Do NOT: use buzzword stacks, sound like an agency pitch, or use phrases like "we help", "book a call",
"our team", or "free consultation". No hard pitch in connection requests.

URLs: no links in connect notes or follow-ups. For first DM only, you may include exactly once:
https://linktr.ee/palnesto.work (portfolio line). No other URLs.

Output limits (strict):
- Connect note: at most {MAX_CONNECT_NOTE_WORDS} words AND at most {MAX_CONNECT_NOTE_CHARS} characters
  (including spaces). No emojis. No links. No hard pitch.
- First DM: at most {MAX_DM_WORDS} words. Short paragraphs; no excessive emojis.
- Follow-up 1, 2, 3: at most {MAX_DM_WORDS} words each. Short, thoughtful, low pressure.
  No emojis in DM, FU1, or FU2. For follow-up 3 only, you may optionally add a single 👍 as the
  very last character.

The user message lists the lead and stage-specific structure to follow. Return only the message
text for the requested stage — no quotes, no labels, no preface.
"""

_POST_AWARE_SUFFIX = (
    " If Post text is provided above, tie the opener to one specific detail from that post (accurate only)."
)

_STAGE_INSTRUCTIONS: dict[str, str] = {
    "connect": (
        "Stage CONNECT (connection request). Under the character and word caps. Greet {first_name}. "
        "Reference their recent post idea, headline category, or positioning in {segment} / at {company} — "
        "one specific insight, not generic praise. Natural connect ask; no hard pitch, no links. "
        "Output only the connection note, nothing else."
        + _POST_AWARE_SUFFIX
    ),
    "dm": (
        "Stage DM (message 1, after they accept). Structure: Hey {first_name}; open with a specific "
        "insight from their recent post or headline (accurate only); relate naturally to AI storytelling / "
        "founder visibility; briefly mention cinematic AI-native storytelling, launch visuals, or short-form "
        "content for founder-led AI/SaaS brands; soft qualify (e.g. internal vs external production). "
        "Include portfolio line: https://linktr.ee/palnesto.work — no other URLs. Short paragraphs; "
        "no buzzwords. Output only the DM, nothing else."
        + _POST_AWARE_SUFFIX
    ),
    "followup_1": (
        "Stage FOLLOWUP_1. Short thoughtful nudge for {first_name}; reference {segment} or visual "
        "storytelling / content systems if natural; offer to share examples or concepts if useful. "
        "Low pressure. No links. Output only the message, nothing else."
        + _POST_AWARE_SUFFIX
    ),
    "followup_2": (
        "Stage FOLLOWUP_2. Light check-in for {first_name}; insight-driven, low pressure; tie to "
        "{company} or {segment} if natural. No links. Output only the message, nothing else."
        + _POST_AWARE_SUFFIX
    ),
    "followup_3": (
        "Stage FOLLOWUP_3 (last touch). Polite close for {first_name}; assume timing may be off or "
        "offer to leave the loop open. You may add a single optional 👍 as the final character. "
        "No links. Output only the message, nothing else."
        + _POST_AWARE_SUFFIX
    ),
}

# Static fallbacks when LLM is unavailable or fails validation twice.
_FALLBACK: dict[tuple[str, str], list[str]] = {
    ("connect", "direct"): [
        "Hey {first_name}, came across your work at {company} in {segment} — interesting direction. Would love to connect.",
        "Hi {first_name}, your take in {segment} stood out — would be good to connect.",
    ],
    ("connect", "curiosity"): [
        "Hey {first_name}, came across {company} in {segment} — would love to connect.",
        "Hi {first_name}, curious about the story you're building at {company}. Open to connect?",
    ],
    ("connect", "value"): [
        "Hey {first_name}, noticed {company} in {segment} — thought it made sense to connect.",
        "Hi {first_name}, founder in {segment} here — would enjoy connecting.",
    ],
    ("dm", "direct"): [
        "Hey {first_name},\nyour positioning at {company} caught my eye.\n"
        "We've been working with founder-led brands on AI-native storytelling and launch visuals.\n"
        "Portfolio: https://linktr.ee/palnesto.work\n"
        "Curious — is content production fully internal for you right now?",
    ],
    ("dm", "curiosity"): [
        "Hey {first_name},\nquick one on visual storytelling at {company}.\n"
        "Portfolio: https://linktr.ee/palnesto.work\n"
        "Are you handling production in-house or experimenting with external support?",
    ],
    ("dm", "value"): [
        "Hey {first_name},\nwe've been working with AI/SaaS teams on cinematic launch content lately.\n"
        "Portfolio: https://linktr.ee/palnesto.work\n"
        "Worth a quick compare on how you're approaching content at {company}?",
    ],
    ("followup_1", "direct"): [
        "Hey {first_name}, just nudging this — happy to share a few visual storytelling examples if useful.",
    ],
    ("followup_1", "curiosity"): [
        "Hey {first_name}, circling back — brands in {segment} seem to be leaning harder into content systems lately.",
    ],
    ("followup_1", "value"): [
        "Hey {first_name}, gentle bump — happy to share concepts if visual storytelling is on your radar.",
    ],
    ("followup_2", "direct"): [
        "Hey {first_name}, checking in once more — no pressure, just wanted to leave the door open.",
    ],
    ("followup_2", "curiosity"): [
        "Hey {first_name}, curious if timing shifted on your side for a quick exchange.",
    ],
    ("followup_2", "value"): [
        "Hey {first_name}, one more ping — still happy to share examples if it helps.",
    ],
    ("followup_3", "direct"): [
        "Hey {first_name}, last note from my side — should I assume timing is off?",
    ],
    ("followup_3", "curiosity"): [
        "Hey {first_name}, final follow-up — want me to close the loop or is a quick reply still worth it?",
    ],
    ("followup_3", "value"): [
        "Hey {first_name}, closing the loop here — all the best with {company}.",
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
    return MAX_CONNECT_NOTE_WORDS if stage == "connect" else MAX_DM_WORDS


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


def _emoji_violates_stage_policy(s: str, stage: OutreachStage) -> bool:
    """Rejects emojis; followup_3 may use a single optional trailing 👍 only (no other emojis)."""
    t = s.strip()
    if not t:
        return False
    if stage != "followup_3":
        return _has_emoji(t)
    rest = t.rstrip()
    if rest.endswith("👍"):
        rest = rest[: -1].rstrip()
        return _has_emoji(rest)
    return _has_emoji(t)


_DM_ALLOWED_PORTFOLIO_URLS = (
    "https://linktr.ee/palnesto.work",
    "https://linktr.ee/palnesto.work/",
    "http://linktr.ee/palnesto.work",
    "http://linktr.ee/palnesto.work/",
)


def _has_link_like(s: str, *, stage: Optional[OutreachStage] = None) -> bool:
    sl = s.lower()
    if stage == "dm":
        remainder = sl
        for allowed in _DM_ALLOWED_PORTFOLIO_URLS:
            remainder = remainder.replace(allowed.lower(), "")
        if "http://" in remainder or "https://" in remainder:
            return True
        if re.search(r"\bwww\.\S+", remainder):
            return True
        return False
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
    if stage == "connect" and len(t) > MAX_CONNECT_NOTE_CHARS:
        return False, "too_many_chars"
    if _has_link_like(t, stage=stage):
        return False, "link"
    if _SALES_BANNED.search(t):
        return False, "sales_language"
    if _emoji_violates_stage_policy(t, stage):
        return False, "emoji"
    max_urls = 1 if stage == "dm" else 0
    ok, reason = validate_message(t, max_words=mw, recent_bodies=recent_bodies, max_url_count=max_urls)
    if not ok:
        return False, reason
    return True, ""


def _truncate_post_text(lead: dict[str, Any]) -> str:
    raw = (lead.get("post_text") or "").strip()
    if not raw:
        return ""
    return raw[:POST_TEXT_LLM_MAX_CHARS]


def _fallback_body(stage: OutreachStage, strategy: str, lead: dict[str, Any]) -> str:
    strat = _normalize_strategy(strategy)
    first = (lead.get("first_name") or lead.get("full_name") or "there").split()[0]
    company = lead.get("company_name") or "your company"
    segment = (
        str(lead.get("segment") or lead.get("industry") or "") or industry_signal(lead) or "AI-native content"
    ).strip()
    if not segment:
        segment = "AI-native content"
    if _truncate_post_text(lead):
        post_lines = {
            "connect": f"Hey {first}, came across your recent post — really interesting direction. Would love to connect.",
            "dm": (
                f"Hey {first},\nyour recent post genuinely stood out.\n"
                f"We've been helping founder-led brands with AI-native storytelling and launch visuals.\n"
                f"Portfolio: https://linktr.ee/palnesto.work\n"
                f"Curious — is content production fully internal at {company} right now?"
            ),
            "followup_1": f"Hey {first}, just nudging this — happy to share a few examples if useful.",
            "followup_2": f"Hey {first}, gentle bump — still happy to share concepts if timing works.",
            "followup_3": f"Hey {first}, last note from my side — should I assume timing is off?",
        }
        return post_lines.get(stage, post_lines["connect"])
    key = (stage, strat)
    pool = _FALLBACK.get(key) or _FALLBACK.get((stage, "direct")) or [
        "Hi {first_name}, would be great to connect."
    ]
    import random

    tpl = random.choice(pool)
    return tpl.format(first_name=first, company=company, segment=segment)


def _build_user_prompt(
    lead: dict[str, Any],
    stage: OutreachStage,
    strategy: str,
    *,
    regen_hint: str = "",
) -> str:
    first = (lead.get("first_name") or lead.get("full_name") or "there").split()[0]
    company = lead.get("company_name") or "your company"
    seg = (
        str(lead.get("segment") or lead.get("industry") or "") or industry_signal(lead) or "AI-native content"
    ).strip()
    if not seg:
        seg = "AI-native content"
    strat = _normalize_strategy(strategy)
    stage_brief = _STAGE_INSTRUCTIONS[stage].format(
        first_name=first, company=company, segment=seg
    )
    headline = (lead.get("linkedin_headline") or "").strip()
    role = (lead.get("title") or "").strip()
    post_url = (lead.get("post_url") or "").strip()
    post_snip = _truncate_post_text(lead)
    base = (
        f"Name: {first}\n"
        f"Company: {company}\n"
        f"Segment: {seg}\n"
    )
    if headline:
        base += f"Headline: {headline}\n"
    if role:
        base += f"Role: {role}\n"
    if post_url:
        base += f"Post URL: {post_url} (context only — do not paste URL in outbound message)\n"
    if post_snip:
        base += f"Post text: {post_snip}\n"
    base += (
        f"Stage: {stage}\n"
        f"Strategy: {strat}\n\n"
        f"{stage_brief}"
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
            max_tokens=100 if stage == "connect" else 500,
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
