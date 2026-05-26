"""Deterministic LinkedIn outreach copy (linkedin_sequence_v1): variable substitution only, no LLM."""

from __future__ import annotations

from typing import Any

from core.ai_engine import OutreachResult, OutreachStage

_CONNECT_WITH_SEGMENT = (
    "Hey {first_name}, came across your work in {segment} — especially around how you're "
    "telling the story. Really interesting direction. Would love to connect."
)

_CONNECT_WITHOUT_SEGMENT = (
    "Hey {first_name}, came across your recent thoughts — really interesting direction. "
    "Would love to connect."
)

_DM_MESSAGE_1 = """Hey {first_name},
saw what you're building at {company} — looks like an interesting space.
We've been helping founder-led AI/SaaS brands with cinematic AI-native storytelling, launch visuals, and short-form content systems lately.
Portfolio: https://linktr.ee/palnesto.work
Curious — are you handling content/visual production fully internally, or still experimenting with external support as well?"""

_FOLLOWUP_1 = """Hey {first_name}, just nudging this —
brands in the {segment} space seem to be benefiting from stronger visual storytelling and content systems right now.
Happy to share a few examples or concepts if useful."""

_FOLLOWUP_2 = """Hey {first_name},
guessing timing might not be right —
if anything changes and visual storytelling becomes a priority, feel free to reach out."""

_FOLLOWUP_3 = """Hey {first_name}, closing the loop here —
if stronger brand content becomes relevant later, happy to connect.
All the best with {company} 👍"""


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
            segment=segment or "your space",
            recent_activity=v["recent_activity"],
        )
        return OutreachResult(text=text, source="fixed_sequence", raw_llm_snippet="")

    if stage == "followup_1":
        text = _FOLLOWUP_1.format(
            first_name=first_name,
            company=company,
            segment=segment or "your category",
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
