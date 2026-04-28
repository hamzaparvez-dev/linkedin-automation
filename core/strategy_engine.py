"""Strategy rotation and template composition (PRD §9)."""

from __future__ import annotations

import random
import sqlite3
from datetime import timedelta
from typing import Any, Optional

from config import STRATEGY_MAX_SHARE, STRATEGY_ROLLING_DAYS, USE_FIXED_LINKEDIN_SEQUENCE
from core.accounts_loader import AccountConfig
from core.ai_engine import OutreachResult, OutreachStage, generate_outreach_message, industry_signal
from core.linkedin_sequence_templates import render_fixed_sequence

STRATEGIES = ("direct", "curiosity", "value", "question", "observation")


def _strategy_share_last_days(
    conn: sqlite3.Connection, account_id: str, strategy: str, days: int
) -> float:
    from datetime import datetime, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = conn.execute(
        """
        SELECT strategy_used AS s, COUNT(*) AS c
        FROM action_log
        WHERE account_id=? AND action_type='dm' AND dry_run=0
          AND created_at >= ?
        GROUP BY strategy_used
        """,
        (account_id, cutoff),
    ).fetchall()
    if not row:
        return 0.0
    total = sum(int(r["c"]) for r in row)
    if total == 0:
        return 0.0
    for r in row:
        if r["s"] == strategy:
            return int(r["c"]) / total
    return 0.0


def pick_strategy(conn: sqlite3.Connection, account: AccountConfig) -> str:
    primary = account.primary_strategy
    if primary not in STRATEGIES:
        primary = "direct"
    share = _strategy_share_last_days(
        conn, account.account_id, primary, STRATEGY_ROLLING_DAYS
    )
    if share < STRATEGY_MAX_SHARE:
        return primary
    alts = [s for s in STRATEGIES if s != primary]
    return random.choice(alts) if alts else primary


def _lead_with_signal(lead: dict[str, Any]) -> dict[str, Any]:
    ld = dict(lead)
    ld["industry_signal"] = industry_signal(ld)
    return ld


def _use_fixed_sequence(account: Optional[AccountConfig]) -> bool:
    if USE_FIXED_LINKEDIN_SEQUENCE:
        return True
    if account is not None and account.outreach_copy_mode == "linkedin_sequence_v1":
        return True
    return False


def compose_from_template(
    lead: dict[str, Any],
    strategy: str,
    recent_bodies: list[str] | None = None,
    *,
    account: Optional[AccountConfig] = None,
) -> OutreachResult:
    """First DM body via LLM (fallback static), or fixed linkedin_sequence_v1 templates."""
    if _use_fixed_sequence(account):
        return render_fixed_sequence("dm", lead)
    return generate_outreach_message(
        "dm",
        _lead_with_signal(lead),
        strategy,
        recent_bodies=recent_bodies or [],
    )


def compose_connect_note(
    lead: dict[str, Any],
    strategy: str,
    *,
    account: Optional[AccountConfig] = None,
) -> OutreachResult:
    """Connection note via LLM (fallback static), or fixed linkedin_sequence_v1 templates."""
    if _use_fixed_sequence(account):
        return render_fixed_sequence("connect", lead)
    return generate_outreach_message(
        "connect",
        _lead_with_signal(lead),
        strategy,
        recent_bodies=[],
    )


def compose_followup_message(
    lead: dict[str, Any],
    strategy: str,
    stage: int,
    recent_bodies: list[str] | None = None,
    *,
    account: Optional[AccountConfig] = None,
) -> OutreachResult:
    """stage 1–3 → followup_1 … followup_3 (calendar gaps from config.FOLLOW_UP_SCHEDULE)."""
    n = int(stage)
    stages: tuple[OutreachStage, OutreachStage, OutreachStage] = ("followup_1", "followup_2", "followup_3")
    st = stages[n - 1] if 1 <= n <= 3 else "followup_1"
    if _use_fixed_sequence(account):
        return render_fixed_sequence(st, lead)
    return generate_outreach_message(
        st,
        _lead_with_signal(lead),
        strategy,
        recent_bodies=recent_bodies or [],
    )
