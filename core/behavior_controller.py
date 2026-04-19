"""Account-level safety: schedule, ramp, caps, burst limits (PRD §7, §15)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from config import (
    BURST_MAX_ACTIONS,
    BURST_WINDOW_MINUTES,
    HARD_CAP_CONNECT,
    HARD_CAP_DM,
    HARD_CAP_REPLY,
)
from core.accounts_loader import AccountConfig
from core.repository import get_daily_usage, increment_usage

ActionType = Literal["connect", "dm", "reply"]


@dataclass
class Approval:
    allowed: bool
    reason: str = ""


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    return int(parts[0]), int(parts[1])


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _is_weekend_local(now: datetime) -> bool:
    return now.weekday() >= 5


def _within_schedule(account: AccountConfig, now: Optional[datetime] = None) -> bool:
    now = now or _now_local()
    if _is_weekend_local(now) and not account.weekend_actions:
        return False
    sh, sm = _parse_hhmm(account.schedule_start)
    eh, em = _parse_hhmm(account.schedule_end)
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _ramp_effective_cap(steady: int, day_index: int, hard: int) -> int:
    """day_index: 1-based days since first_action_date."""
    if day_index <= 0:
        day_index = 1
    if day_index <= 3:
        cap = min(10, steady, hard)
    elif day_index <= 7:
        cap = min(20, steady, hard)
    else:
        cap = min(steady, hard)
    return max(1, cap)


def _first_action_day_index(conn: sqlite3.Connection, account_id: str) -> int:
    row = conn.execute(
        "SELECT first_action_date FROM accounts_meta WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row or not row["first_action_date"]:
        return 1
    try:
        first = date.fromisoformat(row["first_action_date"])
    except ValueError:
        return 1
    return max(1, (date.today() - first).days + 1)


def _touch_first_action_date(conn: sqlite3.Connection, account_id: str) -> None:
    row = conn.execute(
        "SELECT first_action_date FROM accounts_meta WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if row and row["first_action_date"]:
        return
    conn.execute(
        "UPDATE accounts_meta SET first_action_date=? WHERE account_id=?",
        (date.today().isoformat(), account_id),
    )
    conn.commit()


def _burst_count(conn: sqlite3.Connection, account_id: str) -> int:
    since = (datetime.now() - timedelta(minutes=BURST_WINDOW_MINUTES)).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM action_log
        WHERE account_id=? AND created_at >= ? AND dry_run=0
          AND status IN ('ok','error')
        """,
        (account_id, since),
    ).fetchone()
    return int(row["c"] if row else 0)


def effective_daily_cap(
    conn: sqlite3.Connection, account: AccountConfig, action: ActionType
) -> int:
    steady = {
        "connect": account.steady_connect_cap,
        "dm": account.steady_dm_cap,
        "reply": account.steady_reply_cap,
    }[action]
    hard = {"connect": HARD_CAP_CONNECT, "dm": HARD_CAP_DM, "reply": HARD_CAP_REPLY}[action]
    idx = _first_action_day_index(conn, account.account_id)
    return _ramp_effective_cap(steady, idx, hard)


def approve_action(
    conn: sqlite3.Connection,
    account: AccountConfig,
    action: ActionType,
    *,
    now: Optional[datetime] = None,
) -> Approval:
    now = now or _now_local()
    row = conn.execute(
        "SELECT paused FROM accounts_meta WHERE account_id=?",
        (account.account_id,),
    ).fetchone()
    if row and row["paused"]:
        return Approval(False, "account_paused_meta")

    if not _within_schedule(account, now):
        return Approval(False, "outside_schedule")

    if _burst_count(conn, account.account_id) >= BURST_MAX_ACTIONS:
        return Approval(False, "burst_cap")

    day = date.today().isoformat()
    usage = get_daily_usage(conn, account.account_id, day)
    cap = effective_daily_cap(conn, account, action)
    used = usage.get(
        {"connect": "connects", "dm": "dms", "reply": "replies"}[action],
        0,
    )
    if used >= cap:
        return Approval(False, f"daily_cap:{action}:{used}/{cap}")

    return Approval(True, "")


def record_action_executed(
    conn: sqlite3.Connection,
    account_id: str,
    action: ActionType,
) -> None:
    _touch_first_action_date(conn, account_id)
    day = date.today().isoformat()
    field = {"connect": "connects", "dm": "dms", "reply": "replies"}[action]
    increment_usage(conn, account_id, day, field, 1)


def set_account_paused(conn: sqlite3.Connection, account_id: str, paused: bool) -> None:
    conn.execute(
        "UPDATE accounts_meta SET paused=? WHERE account_id=?",
        (1 if paused else 0, account_id),
    )
    conn.commit()
