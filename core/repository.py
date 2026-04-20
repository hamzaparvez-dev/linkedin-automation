"""Persistence layer for leads, accounts, usage, and logs."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import sqlite3

from core.accounts_loader import AccountConfig, AccountsDocument
from core.db import get_connection, init_schema
from core.ids import make_lead_id, normalize_linkedin_url
from core.state_machine import assert_transition

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sync_accounts_meta(conn: sqlite3.Connection, doc: AccountsDocument) -> None:
    for a in doc.accounts:
        conn.execute(
            """
            INSERT INTO accounts_meta (
                account_id, linkedin_profile, user_agent, schedule_start, schedule_end,
                primary_strategy, delay_min_sec, delay_max_sec,
                steady_connect_cap, steady_dm_cap, steady_reply_cap,
                weekend_actions, phantombuster_connect_agent_id,
                phantombuster_dm_agent_id, paused
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                linkedin_profile=excluded.linkedin_profile,
                user_agent=excluded.user_agent,
                schedule_start=excluded.schedule_start,
                schedule_end=excluded.schedule_end,
                primary_strategy=excluded.primary_strategy,
                delay_min_sec=excluded.delay_min_sec,
                delay_max_sec=excluded.delay_max_sec,
                steady_connect_cap=excluded.steady_connect_cap,
                steady_dm_cap=excluded.steady_dm_cap,
                steady_reply_cap=excluded.steady_reply_cap,
                weekend_actions=excluded.weekend_actions,
                phantombuster_connect_agent_id=excluded.phantombuster_connect_agent_id,
                phantombuster_dm_agent_id=excluded.phantombuster_dm_agent_id
            """,
            (
                a.account_id,
                a.linkedin_profile,
                a.user_agent or None,
                a.schedule_start,
                a.schedule_end,
                a.primary_strategy,
                a.delay_min_sec,
                a.delay_max_sec,
                a.steady_connect_cap,
                a.steady_dm_cap,
                a.steady_reply_cap,
                1 if a.weekend_actions else 0,
                a.phantombuster_connect_agent_id or None,
                a.phantombuster_dm_agent_id or None,
                0,
            ),
        )
    conn.commit()


def upsert_lead_new(
    conn: sqlite3.Connection,
    lead: dict[str, Any],
    campaign_id: str,
) -> str:
    apollo_id = str(lead.get("id") or "")
    li = normalize_linkedin_url(str(lead.get("linkedin_url") or ""))
    lead_id = make_lead_id(apollo_id, li)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO leads (
            lead_id, apollo_person_id, linkedin_url, email, first_name, last_name, full_name,
            company_name, title, industry, location, years_experience,
            connection_count, active_last_30_days, activity_level,
            score, score_breakdown_json, status, campaign_id,
            message_history_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(lead_id) DO UPDATE SET
            apollo_person_id=excluded.apollo_person_id,
            linkedin_url=excluded.linkedin_url,
            email=excluded.email,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            full_name=excluded.full_name,
            company_name=excluded.company_name,
            title=excluded.title,
            industry=excluded.industry,
            location=excluded.location,
            years_experience=excluded.years_experience,
            updated_at=excluded.updated_at
        WHERE leads.status IN ('NEW','FAILED')
        """,
        (
            lead_id,
            apollo_id,
            li,
            str(lead.get("email") or "").strip() or None,
            lead.get("first_name"),
            lead.get("last_name"),
            lead.get("full_name"),
            lead.get("company_name"),
            lead.get("title"),
            lead.get("industry"),
            lead.get("location"),
            int(lead.get("years_experience") or 0),
            _int_or_none(lead.get("connection_count")),
            _bool_to_int(lead.get("active_last_30_days")),
            lead.get("activity_level"),
            int(lead.get("score") or 0),
            json.dumps(lead.get("score_breakdown") or {}),
            "NEW",
            campaign_id,
            json.dumps([]),
            now,
            now,
        ),
    )
    conn.commit()
    return lead_id


def insert_lead_csv_import(
    conn: sqlite3.Connection,
    *,
    lead_id: str,
    linkedin_url: str,
    campaign_id: str,
    apollo_person_id: str = "",
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    company_name: Optional[str] = None,
    title: Optional[str] = None,
    industry: Optional[str] = None,
    location: Optional[str] = None,
    years_experience: int = 0,
) -> bool:
    """
    Insert a single NEW lead from CSV/Apify import. Does not overwrite existing rows.
    Returns True if a row was inserted, False if lead_id already exists (ON CONFLICT DO NOTHING).
    """
    li = normalize_linkedin_url(linkedin_url)
    if not li:
        return False
    now = utc_now_iso()
    ap = (apollo_person_id or "").strip() or None
    cur = conn.execute(
        """
        INSERT INTO leads (
            lead_id, apollo_person_id, linkedin_url, email, first_name, last_name, full_name,
            company_name, title, industry, location, years_experience,
            connection_count, active_last_30_days, activity_level,
            score, score_breakdown_json, status, account_id, campaign_id,
            message_history_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(lead_id) DO NOTHING
        """,
        (
            lead_id,
            ap,
            li,
            email,
            first_name,
            last_name,
            full_name,
            company_name,
            title,
            industry,
            location,
            int(years_experience or 0),
            None,
            None,
            None,
            0,
            json.dumps({}),
            "NEW",
            None,
            campaign_id,
            json.dumps([]),
            now,
            now,
        ),
    )
    inserted = getattr(cur, "rowcount", 0) == 1
    conn.commit()
    return inserted


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def _bool_to_int(v: Any) -> Optional[int]:
    if v is True or str(v).lower() in ("true", "1", "yes"):
        return 1
    if v is False or str(v).lower() in ("false", "0", "no"):
        return 0
    return None


def update_lead_enrichment(conn: sqlite3.Connection, lead_id: str, fields: dict[str, Any]) -> None:
    row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        return
    prev = row["status"]
    now = utc_now_iso()
    assert_transition(prev, "ENRICHED")
    conn.execute(
        """
        UPDATE leads SET
            connection_count=?,
            active_last_30_days=?,
            activity_level=?,
            status='ENRICHED',
            updated_at=?
        WHERE lead_id=?
        """,
        (
            fields.get("connection_count"),
            1 if fields.get("active_last_30_days") else 0,
            fields.get("activity_level"),
            now,
            lead_id,
        ),
    )
    conn.commit()


def apply_score_to_lead(
    conn: sqlite3.Connection,
    lead_id: str,
    score: int,
    breakdown: dict[str, Any],
    qualified: bool,
) -> None:
    row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        return
    prev = row["status"]
    now = utc_now_iso()
    target = "QUALIFIED" if qualified else "ENRICHED"
    assert_transition(prev, target)
    conn.execute(
        """
        UPDATE leads SET
            score=?,
            score_breakdown_json=?,
            status=?,
            updated_at=?
        WHERE lead_id=?
        """,
        (score, json.dumps(breakdown), target, now, lead_id),
    )
    conn.commit()


def fetch_leads_for_enrichment(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM leads WHERE status='NEW' AND linkedin_url != '' ORDER BY created_at"
        )
    )


def promote_new_with_linkedin_to_enriched_for_scoring(conn: sqlite3.Connection) -> int:
    """
    When Phantombuster enrichment is skipped, still allow scoring for leads that already
    have a LinkedIn URL (e.g. from Apollo bulk_match).
    """
    rows = list(
        conn.execute(
            "SELECT lead_id FROM leads WHERE status='NEW' AND TRIM(linkedin_url) != ''"
        )
    )
    now = utc_now_iso()
    updated = 0
    for r in rows:
        lid = r["lead_id"]
        st = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lid,)).fetchone()
        if not st or st["status"] != "NEW":
            continue
        assert_transition("NEW", "ENRICHED")
        conn.execute(
            "UPDATE leads SET status='ENRICHED', updated_at=? WHERE lead_id=?",
            (now, lid),
        )
        updated += 1
    conn.commit()
    return updated


def fetch_leads_for_scoring(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM leads WHERE status='ENRICHED' ORDER BY created_at"))


def distribute_qualified_leads(conn: sqlite3.Connection, accounts: list[AccountConfig]) -> int:
    rows = list(
        conn.execute(
            """
            SELECT lead_id FROM leads
            WHERE status='QUALIFIED' AND (account_id IS NULL OR account_id='')
            ORDER BY score DESC, created_at
            """
        )
    )
    if not rows or not accounts:
        return 0
    n = len(accounts)
    updated = 0
    for i, r in enumerate(rows):
        aid = accounts[i % n].account_id
        lid = r["lead_id"]
        row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lid,)).fetchone()
        assert_transition(row["status"], "ASSIGNED_TO_ACCOUNT")
        conn.execute(
            """
            UPDATE leads SET account_id=?, status='ASSIGNED_TO_ACCOUNT', updated_at=?
            WHERE lead_id=?
            """,
            (aid, utc_now_iso(), lid),
        )
        updated += 1
    conn.commit()
    return updated


def get_daily_usage(conn: sqlite3.Connection, account_id: str, day: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT connects, dms, replies FROM daily_usage WHERE account_id=? AND day=?",
        (account_id, day),
    ).fetchone()
    if not row:
        return {"connects": 0, "dms": 0, "replies": 0}
    return {"connects": row["connects"], "dms": row["dms"], "replies": row["replies"]}


def increment_usage(
    conn: sqlite3.Connection, account_id: str, day: str, field: str, delta: int = 1
) -> None:
    assert field in ("connects", "dms", "replies")
    conn.execute(
        f"""
        INSERT INTO daily_usage (account_id, day, connects, dms, replies)
        VALUES (?, ?, 0, 0, 0)
        ON CONFLICT(account_id, day) DO NOTHING
        """,
        (account_id, day),
    )
    conn.execute(
        f"""
        UPDATE daily_usage SET {field} = {field} + ?
        WHERE account_id=? AND day=?
        """,
        (delta, account_id, day),
    )
    conn.commit()


def get_account_linkedin_profile(conn: sqlite3.Connection, account_id: str) -> str:
    row = conn.execute(
        "SELECT linkedin_profile FROM accounts_meta WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        return ""
    return str(row["linkedin_profile"] or "").strip()


def get_account_user_agent(conn: sqlite3.Connection, account_id: str) -> str:
    row = conn.execute(
        "SELECT user_agent FROM accounts_meta WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        return ""
    return str(row["user_agent"] or "").strip()


def log_action(
    conn: sqlite3.Connection,
    *,
    lead_id: Optional[str],
    account_id: Optional[str],
    action_type: str,
    status: str,
    detail: str = "",
    strategy_used: str = "",
    message_variant: str = "",
    dry_run: bool = False,
    linkedin_session: str = "",
    phantom_response: str = "",
) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(action_log)").fetchall()}
    has_session = "linkedin_session" in cols
    has_phantom = "phantom_response" in cols
    if has_session and has_phantom:
        conn.execute(
            """
            INSERT INTO action_log (
                lead_id, account_id, action_type, status, detail,
                strategy_used, message_variant, dry_run, created_at,
                linkedin_session, phantom_response
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lead_id,
                account_id,
                action_type,
                status,
                detail,
                strategy_used,
                message_variant,
                1 if dry_run else 0,
                utc_now_iso(),
                (linkedin_session or "")[:500],
                (phantom_response or "")[:4000],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO action_log (
                lead_id, account_id, action_type, status, detail,
                strategy_used, message_variant, dry_run, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                lead_id,
                account_id,
                action_type,
                status,
                detail,
                strategy_used,
                message_variant,
                1 if dry_run else 0,
                utc_now_iso(),
            ),
        )
    conn.commit()


def record_sent_message(conn: sqlite3.Connection, account_id: str, body: str, strategy: str) -> None:
    conn.execute(
        """
        INSERT INTO sent_messages (account_id, body, strategy, created_at)
        VALUES (?,?,?,?)
        """,
        (account_id, body, strategy, utc_now_iso()),
    )
    conn.commit()


def recent_messages_for_repetition(conn: sqlite3.Connection, account_id: str, limit: int = 50) -> list[str]:
    rows = conn.execute(
        """
        SELECT body FROM sent_messages
        WHERE account_id=? ORDER BY id DESC LIMIT ?
        """,
        (account_id, limit),
    ).fetchall()
    return [r["body"] for r in rows]


def bump_metric(conn: sqlite3.Connection, day: str, account_id: Optional[str], metric: str, delta: int = 1) -> None:
    aid = account_id or "__global__"
    conn.execute(
        """
        INSERT INTO metrics_daily (day, account_id, metric, value) VALUES (?,?,?,?)
        ON CONFLICT(day, account_id, metric) DO UPDATE SET value = value + excluded.value
        """,
        (day, aid, metric, delta),
    )
    conn.commit()


def set_checkpoint(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO checkpoints (key, value) VALUES (?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )
    conn.commit()


def get_checkpoint(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM checkpoints WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def row_to_lead_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("active_last_30_days") is not None:
        d["active_last_30_days"] = bool(d["active_last_30_days"])
    if d.get("connection_count") is not None:
        d["connection_count"] = int(d["connection_count"])
    if d.get("years_experience") is not None:
        d["years_experience"] = int(d["years_experience"])
    if d.get("score_breakdown_json"):
        try:
            d["score_breakdown"] = json.loads(d["score_breakdown_json"])
        except json.JSONDecodeError:
            d["score_breakdown"] = {}
    if d.get("message_history_json"):
        try:
            d["message_history"] = json.loads(d["message_history_json"])
        except json.JSONDecodeError:
            d["message_history"] = []
    return d


def ingest_inbound_reply(conn: sqlite3.Connection, lead_id: str, inbound_text: str) -> str:
    """
    Record an inbound LinkedIn reply, classify it (LLM + regex), move lead to a terminal
    engagement outcome. Does not send any outbound automation (human handles replies).
    """
    from core.reply_handler import classify_reply_auto

    row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        raise ValueError("unknown lead_id")
    prev = row["status"]
    if prev not in ("MESSAGED", "FOLLOW_UP_1", "FOLLOW_UP_2", "FOLLOW_UP_3"):
        raise ValueError(f"ingest_inbound_reply not allowed from status {prev!r}")
    body = (inbound_text or "").strip()
    if not body:
        raise ValueError("empty inbound message")

    append_message_history(conn, lead_id, "inbound_dm", body)
    label = classify_reply_auto(body)
    now = utc_now_iso()
    transition_lead_status(conn, lead_id, "REPLIED", reply_type=label, last_action_at=now)

    terminal = {
        "positive": "POSITIVE",
        "negative": "NEGATIVE",
        "neutral": "NEUTRAL",
        "complex": "HUMAN_REVIEW",
    }.get(label, "HUMAN_REVIEW")
    transition_lead_status(conn, lead_id, terminal, last_action_at=utc_now_iso())
    return terminal


def promote_to_connected(conn: sqlite3.Connection, lead_id: str) -> None:
    """Operator hook: mark INVITED → CONNECTED after LinkedIn shows accept (PRD §8)."""
    row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        raise ValueError("unknown lead_id")
    assert_transition(row["status"], "CONNECTED")
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE leads SET status='CONNECTED', connected_at=?, last_action_at=?, updated_at=?
        WHERE lead_id=?
        """,
        (now, now, now, lead_id),
    )
    conn.commit()


def append_message_history(
    conn: sqlite3.Connection, lead_id: str, role: str, text: str
) -> None:
    row = conn.execute(
        "SELECT message_history_json FROM leads WHERE lead_id=?", (lead_id,)
    ).fetchone()
    hist: list[Any] = []
    if row and row["message_history_json"]:
        try:
            hist = json.loads(row["message_history_json"])
        except json.JSONDecodeError:
            hist = []
    hist.append({"role": role, "text": text, "at": utc_now_iso()})
    conn.execute(
        "UPDATE leads SET message_history_json=?, updated_at=? WHERE lead_id=?",
        (json.dumps(hist), utc_now_iso(), lead_id),
    )
    conn.commit()


def transition_lead_status(conn: sqlite3.Connection, lead_id: str, new_status: str, **kwargs: Any) -> None:
    row = conn.execute("SELECT status FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        return
    prev = row["status"]
    assert_transition(prev, new_status)
    fields = ["status=?", "updated_at=?"]
    values: list[Any] = [new_status, utc_now_iso()]
    for k, v in kwargs.items():
        if k in (
            "invited_at",
            "connected_at",
            "first_dm_sent_at",
            "last_action_at",
            "reply_type",
            "followup_1_sent_at",
            "followup_2_sent_at",
            "followup_3_sent_at",
        ):
            fields.append(f"{k}=?")
            values.append(v)
    values.append(lead_id)
    conn.execute(f"UPDATE leads SET {', '.join(fields)} WHERE lead_id=?", values)
    conn.commit()
