"""Load multi-account config from JSON (PRD §4)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    linkedin_profile: str
    user_agent: str
    schedule_start: str
    schedule_end: str
    primary_strategy: str
    delay_min_sec: int
    delay_max_sec: int
    steady_connect_cap: int
    steady_dm_cap: int
    steady_reply_cap: int
    weekend_actions: bool
    phantombuster_connect_agent_id: str
    phantombuster_dm_agent_id: str
    profile_name: str


@dataclass(frozen=True)
class AccountsDocument:
    campaign_id: str
    accounts: list[AccountConfig]


def load_accounts_document(path: str) -> AccountsDocument:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(
            f"Account config not found: {path}. Copy config/accounts.example.json to config/accounts.json"
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    campaign_id = str(raw.get("campaign_id") or "default-campaign")
    accounts_raw: list[dict[str, Any]] = raw.get("accounts") or []
    if not accounts_raw:
        raise ValueError("accounts.json must contain a non-empty 'accounts' array")
    accounts: list[AccountConfig] = []
    for a in accounts_raw:
        dr = a.get("delay_range_sec") or [30, 180]
        lim = a.get("steady_daily_limits") or {}
        accounts.append(
            AccountConfig(
                account_id=str(a["account_id"]),
                linkedin_profile=str(a.get("linkedin_profile") or a["account_id"]),
                user_agent=str(a.get("user_agent") or a.get("userAgent") or "").strip(),
                schedule_start=str(a["schedule_start"]),
                schedule_end=str(a["schedule_end"]),
                primary_strategy=str(a.get("primary_strategy") or "direct"),
                delay_min_sec=int(dr[0]),
                delay_max_sec=int(dr[1]) if len(dr) > 1 else int(dr[0]),
                steady_connect_cap=int(lim.get("connect", 20)),
                steady_dm_cap=int(lim.get("dm", 15)),
                steady_reply_cap=int(lim.get("reply", 10)),
                weekend_actions=bool(a.get("weekend_actions", False)),
                phantombuster_connect_agent_id=str(
                    a.get("phantombuster_connect_agent_id") or ""
                ).strip(),
                phantombuster_dm_agent_id=str(
                    a.get("phantombuster_dm_agent_id") or ""
                ).strip(),
                profile_name=str(a.get("profile_name") or "").strip(),
            )
        )
    logger.info("Loaded %d account(s) for campaign %s", len(accounts), campaign_id)
    return AccountsDocument(campaign_id=campaign_id, accounts=accounts)
