"""Deterministic LinkedIn outreach copy (linkedin_sequence_v1): variable substitution only, no LLM."""

from __future__ import annotations

from typing import Any

from core.ai_engine import OutreachResult, OutreachStage

_CONNECT_WITH_SEGMENT = (
    "Hey {first_name}, saw what you're working on in {segment} — especially around "
    "Web3/Web2 products. Would be good to connect."
)

_CONNECT_WITHOUT_SEGMENT = (
    "Hey {first_name}, saw what you're working on — especially around "
    "Web3/Web2 products. Would be good to connect."
)

_DM_MESSAGE_1 = """Hey {first_name},
saw what you're building — looks like an interesting space.
we usually work with teams building Web3/Web2 products, helping on MVPs and execution (smart contracts, backend, full-stack builds).
just curious — is that something you're already sorted on, or still figuring out?"""

_FOLLOWUP_1 = """Hey {first_name}, just nudging this —
we've been working with a few teams recently on Web3/Web2 product builds and execution.
if it's worth exploring on your side, happy to connect."""

_FOLLOWUP_2 = """Hey {first_name},
guessing timing might not be right —
if anything changes and you need help on product build/execution, feel free to reach out."""

_FOLLOWUP_3 = """Hey {first_name}, closing the loop here —
if building or scaling becomes relevant later, happy to connect.
all the best with {company} 👍"""


def _first_token_name(lead: dict[str, Any]) -> str:
    raw = (lead.get("first_name") or lead.get("full_name") or "there") or "there"
    return str(raw).split()[0].strip() if str(raw).split() else "there"


def template_variables(lead: dict[str, Any]) -> dict[str, str]:
    """Normalize fields for .format(); segment empty means connect omits 'in {segment}' (no heuristic guess)."""
    first = _first_token_name(lead)
    company = str(lead.get("company_name") or "your company").strip() or "your company"
    segment = str(lead.get("segment") or lead.get("industry") or "").strip()
    recent_activity = str(lead.get("recent_activity") or "").strip()
    return {
        "first_name": first,
        "company": company,
        "segment": segment,
        "recent_activity": recent_activity,
    }


def render_fixed_sequence(stage: OutreachStage, lead: dict[str, Any]) -> OutreachResult:
    """Render exact templates for connect / dm / followup_* stages."""
    v = template_variables(lead)
    first_name = v["first_name"]
    company = v["company"]
    segment = v["segment"]

    if stage == "connect":
        if segment:
            text = _CONNECT_WITH_SEGMENT.format(
                first_name=first_name,
                segment=segment,
                company=company,
                recent_activity=v["recent_activity"],
            )
        else:
            text = _CONNECT_WITHOUT_SEGMENT.format(
                first_name=first_name,
                segment=segment,
                company=company,
                recent_activity=v["recent_activity"],
            )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    if stage == "dm":
        text = _DM_MESSAGE_1.format(
            first_name=first_name,
            company=company,
            segment=segment or "",
            recent_activity=v["recent_activity"],
        )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    if stage == "followup_1":
        text = _FOLLOWUP_1.format(
            first_name=first_name,
            company=company,
            segment=segment or "",
            recent_activity=v["recent_activity"],
        )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    if stage == "followup_2":
        text = _FOLLOWUP_2.format(
            first_name=first_name,
            company=company,
            segment=segment or "",
            recent_activity=v["recent_activity"],
        )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    if stage == "followup_3":
        text = _FOLLOWUP_3.format(
            first_name=first_name,
            company=company,
            segment=segment or "",
            recent_activity=v["recent_activity"],
        )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    raise ValueError(f"unknown stage for fixed sequence: {stage}")
