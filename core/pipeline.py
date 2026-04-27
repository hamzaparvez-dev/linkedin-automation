"""PRD v2 orchestration: Apollo (optional) → promote ENRICHED for scoring → score → assign → exports. No Phantombuster profile scraper."""

from __future__ import annotations

import csv
import logging
import os
from typing import Any, Optional

from config import (
    ACCOUNT_CONFIG_PATH,
    ENRICHED_LEADS_CSV,
    QUALIFIED_LEADS_CSV,
    RAW_LEADS_CSV,
    SCORED_LEADS_CSV,
)
from core.accounts_loader import load_accounts_document
from core.db import get_connection, init_schema
from core.lead_scorer import LeadScorer
from core.metrics import export_intelligence
from core.repository import (
    apply_score_to_lead,
    distribute_qualified_leads,
    fetch_leads_for_scoring,
    promote_new_with_linkedin_to_enriched_for_scoring,
    row_to_lead_dict,
    sync_accounts_meta,
    upsert_lead_new,
)
from integrations.apollo_client import ApolloClient

logger = logging.getLogger(__name__)


def _export_csv(conn, sql: str, path: str, params: tuple = ()) -> None:
    rows = list(conn.execute(sql, params))
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    keys = rows[0].keys()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(keys))
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))


def run_full_pipeline_v2(
    *,
    target_leads: int = 1000,
    skip_apollo: bool = False,
    resume: bool = False,
    accounts_path: Optional[str] = None,
) -> None:
    init_schema()
    conn = get_connection()
    doc = load_accounts_document(accounts_path or ACCOUNT_CONFIG_PATH)
    sync_accounts_meta(conn, doc)
    campaign_id = doc.campaign_id

    if skip_apollo:
        logger.info("Skipping Apollo extraction (resume / manual)")
    else:
        client = ApolloClient()
        leads = client.extract_leads(target_count=target_leads)
        client.save_to_csv(leads, RAW_LEADS_CSV)
        for lead in leads:
            upsert_lead_new(conn, lead, campaign_id=campaign_id)
        conn.commit()
        logger.info("Upserted %d leads into SQLite (campaign=%s)", len(leads), campaign_id)

    logger.info("Profile scraper not used; promoting NEW leads with LinkedIn URL to ENRICHED for scoring")
    promoted = promote_new_with_linkedin_to_enriched_for_scoring(conn)
    if promoted:
        logger.info(
            "Promoted %s NEW lead(s) with LinkedIn URL → ENRICHED for scoring",
            promoted,
        )
    _export_csv(
        conn,
        "SELECT * FROM leads WHERE status='ENRICHED' OR status='QUALIFIED'",
        ENRICHED_LEADS_CSV,
    )
    stuck = conn.execute(
        "SELECT COUNT(*) AS c FROM leads WHERE status='NEW' AND TRIM(linkedin_url)=''"
    ).fetchone()["c"]
    if stuck:
        logger.warning(
            "%s NEW lead(s) have no LinkedIn URL — engagement cannot run until "
            "Apollo bulk_match is enabled (APOLLO_EXTRACT_BULK_MATCH=true).",
            stuck,
        )

    scorer = LeadScorer()
    for r in fetch_leads_for_scoring(conn):
        d = row_to_lead_dict(r)
        scored = scorer.score_lead(d)
        apply_score_to_lead(
            conn,
            r["lead_id"],
            int(scored["score"]),
            scored.get("score_breakdown") or {},
            bool(scored.get("qualified")),
        )
    _export_csv(conn, "SELECT * FROM leads ORDER BY score DESC", SCORED_LEADS_CSV)
    _export_csv(
        conn,
        "SELECT * FROM leads WHERE status='QUALIFIED' ORDER BY score DESC",
        QUALIFIED_LEADS_CSV,
    )

    n = distribute_qualified_leads(conn, doc.accounts)
    logger.info("Assigned %d qualified leads across accounts", n)
    conn.close()

    conn = get_connection()
    paths = export_intelligence(conn)
    conn.close()
    logger.info("Campaign intelligence: %s", paths)
    if resume:
        logger.info("Resume flag set (checkpoint / future use).")
