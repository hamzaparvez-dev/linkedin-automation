"""Promote and assign post-text CSV leads (no Apollo) for engagement."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from config import ACCOUNT_CONFIG_PATH, MIN_SCORE_THRESHOLD
from core.accounts_loader import load_accounts_document
from core.db import init_schema
from core.lead_scorer import LeadScorer
from core.repository import (
    apply_score_to_lead,
    distribute_qualified_leads,
    fetch_leads_for_scoring,
    get_connection,
    promote_new_with_linkedin_to_enriched_for_scoring,
    row_to_lead_dict,
    utc_now_iso,
)
from core.state_machine import assert_transition
from dashboard_app.apify_csv_import import import_apify_csv

logger = logging.getLogger(__name__)


def promote_post_leads_to_enriched(conn: sqlite3.Connection) -> int:
    """NEW → ENRICHED when linkedin_url and post_text are present."""
    rows = list(
        conn.execute(
            """
            SELECT lead_id FROM leads
            WHERE status='NEW'
              AND TRIM(COALESCE(linkedin_url,'')) != ''
              AND TRIM(COALESCE(post_text,'')) != ''
            """
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


def score_and_qualify_post_leads(conn: sqlite3.Connection) -> tuple[int, int]:
    """Score ENRICHED rows; return (scored_count, qualified_count)."""
    scorer = LeadScorer()
    rows = fetch_leads_for_scoring(conn)
    scored_n = 0
    qualified_n = 0
    for row in rows:
        lead = row_to_lead_dict(row)
        if not (lead.get("post_text") or "").strip():
            continue
        scored = scorer.score_lead(lead)
        apply_score_to_lead(
            conn,
            lead["lead_id"],
            int(scored["score"]),
            scored.get("score_breakdown") or {},
            bool(scored.get("qualified")),
        )
        scored_n += 1
        if scored.get("qualified"):
            qualified_n += 1
    return scored_n, qualified_n


def promote_post_import_leads(
    conn: sqlite3.Connection,
    *,
    accounts_path: str | None = None,
) -> dict[str, int]:
    """
    Promote post-text NEW leads → ENRICHED → score → QUALIFIED → assign to accounts.
    """
    enriched = promote_post_leads_to_enriched(conn)
    if enriched == 0:
        enriched = promote_new_with_linkedin_to_enriched_for_scoring(conn)
    scored, qualified = score_and_qualify_post_leads(conn)
    doc = load_accounts_document(accounts_path or ACCOUNT_CONFIG_PATH)
    assigned = distribute_qualified_leads(conn, doc.accounts)
    return {
        "enriched": enriched,
        "scored": scored,
        "qualified": qualified,
        "assigned": assigned,
    }


def import_post_csv_file(
    conn: sqlite3.Connection,
    path: str | Path,
    campaign_id: str,
) -> tuple[int, int, list[str]]:
    """Read a local CSV and insert via import_apify_csv."""
    data = Path(path).read_bytes()
    return import_apify_csv(conn, data, campaign_id)
