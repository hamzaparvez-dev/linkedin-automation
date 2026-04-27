"""
Daily autonomous job: Apollo Web3 scrape → merge CSV → SQLite import →
score → assign → engagement. (Phantombuster profile scraper removed.)

Designed for cron/systemd (single-instance lock).
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from config import (
    ACCOUNT_CONFIG_PATH,
    APOLLO_WEB3_CSV_PATH,
    BASE_DIR,
    DATABASE_PATH,
    ENGAGEMENT_MAX_LEADS_PER_ACCOUNT,
)
from core.accounts_loader import load_accounts_document
from core.db import get_connection, init_schema
from core.lead_scorer import LeadScorer
from core.metrics import export_intelligence
from core.pipeline import _export_csv
from core.repository import (
    apply_score_to_lead,
    distribute_qualified_leads,
    fetch_leads_for_scoring,
    promote_new_with_linkedin_to_enriched_for_scoring,
    row_to_lead_dict,
    sync_accounts_meta,
    upsert_lead_new,
)
from core.engagement_runner import run_engagement
from lead_extraction.pipeline import run_apollo_web3_extraction

logger = logging.getLogger(__name__)

ROOT = BASE_DIR
MERGED_CSV = Path(os.getenv("LEADS_LOOKUP_MERGED_PATH", str(ROOT / "output" / "leads_lookup_merged.csv")))
LOCK_PATH = Path(os.getenv("DAILY_AUTOMATION_LOCK_PATH", str(ROOT / "logs" / "daily_automation.lock")))


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


@contextmanager
def _single_instance_lock():
    """Non-blocking exclusive lock; skip run if another instance holds the lock."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield True
        return

    fh = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning(
            "Daily automation already running (lock: %s); exiting to avoid overlap.",
            LOCK_PATH,
        )
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _refresh_lookup_merge() -> None:
    script = ROOT / "scripts" / "export_lookup_csv.py"
    if not script.is_file():
        return
    subprocess.run(
        [sys.executable, str(script)],
        check=False,
        cwd=str(ROOT),
    )


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    if not full or not str(full).strip():
        return None, None
    parts = str(full).strip().split(None, 1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def _merged_row_to_lead(row: dict[str, str]) -> dict | None:
    email = str(row.get("email") or "").strip()
    li = str(row.get("linkedin_url") or "").strip()
    apollo_id = str(row.get("id") or "").strip()
    if not email or not li or not apollo_id:
        return None
    fn, ln = _split_name(row.get("full_name"))
    ye_raw = str(row.get("years_experience") or "").strip()
    try:
        years_exp = int(ye_raw) if ye_raw else 0
    except ValueError:
        years_exp = 0
    return {
        "id": apollo_id,
        "linkedin_url": li,
        "email": email,
        "full_name": (row.get("full_name") or "").strip() or None,
        "first_name": fn,
        "last_name": ln,
        "company_name": (row.get("company_name") or "").strip() or None,
        "title": (row.get("title") or "").strip() or None,
        "industry": (row.get("industry") or "").strip() or None,
        "location": (row.get("location") or "").strip() or None,
        "years_experience": years_exp,
        "connection_count": None,
        "active_last_30_days": None,
        "activity_level": None,
        "score": 0,
        "score_breakdown": {},
    }


def import_merged_lookup_csv(
    conn,
    path: Path,
    *,
    campaign_id: str,
) -> int:
    """Upsert rows from merged lookup CSV (prefers apollo_web3 over main_pipeline_raw on same id)."""
    if not path.is_file():
        logger.warning("Merged lookup CSV missing: %s", path)
        return 0
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Later row wins per Apollo id so apollo_web3 should win
    rows.sort(key=lambda r: (str(r.get("source") or "") != "apollo_web3", r.get("id") or ""))
    by_id: dict[str, dict] = {}
    for raw in rows:
        lead = _merged_row_to_lead({k: str(v or "") for k, v in raw.items()})
        if not lead:
            continue
        by_id[lead["id"]] = lead
    n = 0
    for lead in by_id.values():
        upsert_lead_new(conn, lead, campaign_id=campaign_id)
        n += 1
    logger.info("Imported / upserted %s leads from %s (campaign=%s)", n, path, campaign_id)
    return n


def run_daily_automation(
    *,
    accounts_path: str | None = None,
    dry_run_engagement: bool = False,
    max_leads_per_account: int | None = None,
) -> None:
    """
    One end-to-end pass. Env:
      DAILY_SKIP_APOLLO — skip Apollo Web3 (e.g. no credits)
      DAILY_APOLLO_TARGET — new rows to collect per run (default 25)
      max_leads_per_account — cap engagement actions per account (default ENGAGEMENT_MAX_LEADS_PER_ACCOUNT).
    """
    with _single_instance_lock() as proceed:
        if not proceed:
            return

        init_schema()
        cfg_path = accounts_path or ACCOUNT_CONFIG_PATH
        doc = load_accounts_document(cfg_path)
        conn = get_connection()
        try:
            sync_accounts_meta(conn, doc)
        finally:
            conn.close()

        if not _env_bool("DAILY_SKIP_APOLLO", "false"):
            try:
                target = _env_int("DAILY_APOLLO_TARGET", 25)
                run_apollo_web3_extraction(target=target, output_path=APOLLO_WEB3_CSV_PATH)
            except Exception:
                logger.exception("Apollo Web3 step failed; continuing with CSV / DB steps")

        if not _env_bool("DAILY_SKIP_MERGED_CSV", "false"):
            _refresh_lookup_merge()
        else:
            logger.info("DAILY_SKIP_MERGED_CSV: skip export_lookup_csv merge step")

        conn = get_connection()
        try:
            if not _env_bool("DAILY_SKIP_MERGED_CSV", "false"):
                import_merged_lookup_csv(conn, MERGED_CSV, campaign_id=doc.campaign_id)
            else:
                logger.info("DAILY_SKIP_MERGED_CSV: skip merged lookup CSV import")

            promote_new_with_linkedin_to_enriched_for_scoring(conn)

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

            _export_csv(conn, "SELECT * FROM leads ORDER BY score DESC", str(ROOT / "output" / "3_scored_leads.csv"))
            _export_csv(
                conn,
                "SELECT * FROM leads WHERE status='QUALIFIED' ORDER BY score DESC",
                str(ROOT / "output" / "4_qualified_leads.csv"),
            )

            n = distribute_qualified_leads(conn, doc.accounts)
            logger.info("Assigned %s qualified leads across accounts", n)

            paths = export_intelligence(conn)
            logger.info("Campaign intelligence: %s", paths)
        finally:
            conn.close()

        cap = max_leads_per_account if max_leads_per_account is not None else ENGAGEMENT_MAX_LEADS_PER_ACCOUNT
        run_engagement(
            dry_run=dry_run_engagement,
            multi_account=True,
            config_path=cfg_path,
            max_leads_per_account=cap,
        )
        logger.info(
            "Daily automation finished (engagement dry_run=%s, max_leads=%s, db=%s)",
            dry_run_engagement,
            cap,
            DATABASE_PATH,
        )
