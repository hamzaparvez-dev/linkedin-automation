"""SQLite schema and connection helper (PRD §13)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from config import DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    path = Path(DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    if own:
        conn = get_connection()
    assert conn is not None
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts_meta (
            account_id TEXT PRIMARY KEY,
            linkedin_profile TEXT,
            schedule_start TEXT NOT NULL,
            schedule_end TEXT NOT NULL,
            primary_strategy TEXT NOT NULL,
            delay_min_sec INTEGER NOT NULL,
            delay_max_sec INTEGER NOT NULL,
            steady_connect_cap INTEGER NOT NULL,
            steady_dm_cap INTEGER NOT NULL,
            steady_reply_cap INTEGER NOT NULL,
            weekend_actions INTEGER NOT NULL DEFAULT 0,
            phantombuster_connect_agent_id TEXT,
            phantombuster_dm_agent_id TEXT,
            first_action_date TEXT,
            paused INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS leads (
            lead_id TEXT PRIMARY KEY,
            apollo_person_id TEXT,
            linkedin_url TEXT NOT NULL,
            email TEXT,
            first_name TEXT,
            last_name TEXT,
            full_name TEXT,
            company_name TEXT,
            title TEXT,
            industry TEXT,
            location TEXT,
            years_experience INTEGER,
            connection_count INTEGER,
            active_last_30_days INTEGER,
            activity_level TEXT,
            score INTEGER,
            score_breakdown_json TEXT,
            status TEXT NOT NULL,
            account_id TEXT,
            campaign_id TEXT,
            invited_at TEXT,
            connected_at TEXT,
            first_dm_sent_at TEXT,
            last_action_at TEXT,
            reply_type TEXT,
            message_history_json TEXT,
            auto_reply_depth INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts_meta(account_id)
        );

        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
        CREATE INDEX IF NOT EXISTS idx_leads_account ON leads(account_id);
        CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);

        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT,
            account_id TEXT,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            strategy_used TEXT,
            message_variant TEXT,
            dry_run INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            linkedin_session TEXT,
            phantom_response TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_action_log_account_time
            ON action_log(account_id, created_at);

        CREATE TABLE IF NOT EXISTS daily_usage (
            account_id TEXT NOT NULL,
            day TEXT NOT NULL,
            connects INTEGER NOT NULL DEFAULT 0,
            dms INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, day)
        );

        CREATE TABLE IF NOT EXISTS sent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            body TEXT NOT NULL,
            strategy TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sent_messages_account
            ON sent_messages(account_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS checkpoints (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metrics_daily (
            day TEXT NOT NULL,
            account_id TEXT,
            metric TEXT NOT NULL,
            value INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, account_id, metric)
        );
        """
    )
    _migrate_leads_columns(conn)
    _migrate_action_log_columns(conn)
    _migrate_accounts_meta_columns(conn)
    conn.commit()
    if own:
        conn.close()


def _migrate_leads_columns(conn: sqlite3.Connection) -> None:
    """Additive migrations for existing SQLite files."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if cols and "email" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN email TEXT")
    if cols and "followup_1_sent_at" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN followup_1_sent_at TEXT")
    if cols and "followup_2_sent_at" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN followup_2_sent_at TEXT")
    if cols and "followup_3_sent_at" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN followup_3_sent_at TEXT")


def _migrate_action_log_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(action_log)").fetchall()}
    if not cols:
        return
    if "linkedin_session" not in cols:
        conn.execute("ALTER TABLE action_log ADD COLUMN linkedin_session TEXT")
    if "phantom_response" not in cols:
        conn.execute("ALTER TABLE action_log ADD COLUMN phantom_response TEXT")


def _migrate_accounts_meta_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts_meta)").fetchall()}
    if not cols:
        return
    if "user_agent" not in cols:
        conn.execute("ALTER TABLE accounts_meta ADD COLUMN user_agent TEXT")
