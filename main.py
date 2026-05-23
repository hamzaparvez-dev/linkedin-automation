"""
main.py — PRD v2 orchestrator (multi-account, SQLite, behavior controller).

Default: full pipeline (Apollo → score → assign → metrics; no Phantombuster profile scraper).

Legacy Clay / Expandi path: --legacy-full (requires ENABLE_LEGACY_OUTREACH=true for Expandi).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from config import (
    ACCOUNT_CONFIG_PATH,
    ENABLE_LEGACY_OUTREACH,
    ENRICHED_LEADS_CSV,
    LOG_DIR,
    MIN_SCORE_THRESHOLD,
    QUALIFIED_LEADS_CSV,
    RAW_LEADS_CSV,
    resolve_engagement_dry_run,
)
from core.db import init_schema
from core.engagement_runner import run_engagement
from core.metrics import export_intelligence
from core.pipeline import run_full_pipeline_v2
from core.behavior_controller import set_account_paused
from core.repository import get_connection, ingest_inbound_reply, promote_to_connected
from integrations.apollo_client import ApolloClient
from integrations.clay_client import ClayClient
from integrations.outreach_client import OutreachDispatcher
from core.lead_scorer import LeadScorer
from lead_extraction.pipeline import run_apollo_web3_extraction
from core.daily_automation import run_daily_automation

LOG_DIR.mkdir(parents=True, exist_ok=True)
os.makedirs("output", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            str(LOG_DIR / f"pipeline_{datetime.now().strftime('%Y%m%d')}.log")
        ),
    ],
)
logger = logging.getLogger(__name__)


def _refresh_lookup_merge() -> None:
    """Rebuild output/leads_lookup_merged.csv from raw + Web3 Apollo CSVs."""
    script = Path(__file__).resolve().parent / "scripts" / "export_lookup_csv.py"
    if not script.is_file():
        return
    try:
        subprocess.run([sys.executable, str(script)], check=False, cwd=str(Path(__file__).resolve().parent))
    except OSError as e:
        logger.warning("Lookup merge script failed: %s", e)


def _save_csv(leads: list[dict], path: str) -> None:
    if not leads:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(leads[0].keys()))
        w.writeheader()
        w.writerows(leads)


def _load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def step1_apollo(target: int) -> list[dict]:
    logger.info("━━━ STEP 1: Apollo Lead Extraction (legacy) ━━━")
    client = ApolloClient()
    leads = client.extract_leads(target_count=target)
    client.save_to_csv(leads, RAW_LEADS_CSV)
    return leads


def step2_clay(leads: list[dict]) -> None:
    if not os.getenv("CLAY_API_KEY"):
        logger.info("━━━ STEP 2: Clay skipped (no CLAY_API_KEY) ━━━")
        return
    logger.info("━━━ STEP 2: Clay Enrichment Push ━━━")
    ClayClient().push_leads(leads)


def step3_save_enriched_snapshot(leads: list[dict]) -> list[dict]:
    """Write leads to ENRICHED_LEADS_CSV for the legacy CSV scoring path (no PB profile scraper)."""
    logger.info("━━━ STEP 3: Save enriched snapshot (same fields as input) ━━━")
    _save_csv(leads, ENRICHED_LEADS_CSV)
    return leads


def step4_score(leads: list[dict]) -> list[dict]:
    logger.info("━━━ STEP 4: Lead Scoring ━━━")
    scorer = LeadScorer()
    scored = scorer.score_all(leads)
    qualified = scorer.filter_qualified(scored)
    scorer.save_scored(scored, "output/3_scored_leads.csv")
    scorer.save_qualified(qualified, QUALIFIED_LEADS_CSV)
    return qualified


def step5_outreach(qualified: list[dict], campaign_id: str) -> dict:
    logger.info("━━━ STEP 5: Legacy SaaS outreach ━━━")
    return OutreachDispatcher(campaign_id=campaign_id).dispatch(qualified)


def run_legacy_full_pipeline(
    target_leads: int,
    campaign_id: str,
    skip_apollo: bool,
) -> None:
    if not ENABLE_LEGACY_OUTREACH:
        logger.warning("Legacy outreach disabled (ENABLE_LEGACY_OUTREACH=false). Step 5 may fail without keys.")
    if skip_apollo and os.path.exists(RAW_LEADS_CSV):
        leads = _load_csv(RAW_LEADS_CSV)
    else:
        leads = step1_apollo(target=target_leads)
    if not leads:
        logger.error("No leads extracted.")
        return
    step2_clay(leads)
    leads = step3_save_enriched_snapshot(leads)
    qualified = step4_score(leads)
    if not qualified:
        logger.warning("No qualified leads.")
        return
    step5_outreach(qualified, campaign_id=campaign_id)


def run_daily_outreach(campaign_id: str) -> None:
    if not os.path.exists(QUALIFIED_LEADS_CSV):
        logger.warning("No qualified leads CSV.")
        return
    qualified = _load_csv(QUALIFIED_LEADS_CSV)
    step5_outreach(qualified, campaign_id=campaign_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Leadgen PRD v2 pipeline",
        epilog="Web3 Apollo-only run: python main.py --apollo-web3 --target 300",
    )
    parser.add_argument("--target", type=int, default=1000)
    parser.add_argument(
        "--apollo-web3",
        action="store_true",
        help="Run Apollo Web3 founder extraction only; writes output/apollo_leads.csv (override via APOLLO_WEB3_CSV_PATH).",
    )
    parser.add_argument("--campaign", type=str, default="YOUR_CAMPAIGN_ID")
    parser.add_argument("--accounts-config", type=str, default=ACCOUNT_CONFIG_PATH)
    parser.add_argument("--full-run", action="store_true", help="Explicit v2 full pipeline (default if no mode flag)")
    parser.add_argument("--engagement-only", action="store_true")
    parser.add_argument("--multi-account-run", action="store_true")
    parser.add_argument("--single-account", action="store_true", help="Engagement: first account only")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Engagement: plan only, no Phantombuster launches (overrides env DRY_RUN).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Engagement: force live Phantombuster even when DRY_RUN=true in .env (use with care).",
    )
    parser.add_argument(
        "--max-leads",
        type=int,
        default=None,
        metavar="N",
        help="Engagement: max leads per account per connect/DM pass (default: ENGAGEMENT_MAX_LEADS_PER_ACCOUNT).",
    )
    parser.add_argument("--resume", action="store_true", help="Reserved for checkpoint resume (partial)")
    parser.add_argument("--skip-apollo", action="store_true")
    parser.add_argument("--legacy-full", action="store_true", help="Old CSV + Clay + Expandi/Waalaxy flow")
    parser.add_argument("--outreach-only", action="store_true", help="Legacy daily SaaS outreach from CSV")
    parser.add_argument(
        "--daily-automation",
        action="store_true",
        help="Daily job: Apollo Web3 → merge CSV → SQLite import → score → assign → engagement (see core/daily_automation.py, DAILY_* env).",
    )
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--promote-connected", type=str, default="", metavar="LEAD_ID")
    parser.add_argument("--intelligence-export", action="store_true")
    parser.add_argument(
        "--ingest-reply",
        nargs=2,
        default=None,
        metavar=("LEAD_ID", "TEXT"),
        help="Record inbound LinkedIn reply: classify (LLM+regex), set terminal status; no auto outbound.",
    )
    parser.add_argument(
        "--pause-account",
        type=str,
        default="",
        metavar="ACCOUNT_ID",
        help="Set accounts_meta.paused=1 for this account_id (live engagement skips until --resume-account).",
    )
    parser.add_argument(
        "--resume-account",
        type=str,
        default="",
        metavar="ACCOUNT_ID",
        help="Set accounts_meta.paused=0 for this account_id after refreshing LinkedIn session / cookies.",
    )
    parser.add_argument(
        "--reset-leads",
        action="store_true",
        help="Delete all leads (and action_log by default). Requires --confirm. Backs up DB first.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --reset-leads to confirm destructive wipe.",
    )
    parser.add_argument(
        "--leads-only",
        action="store_true",
        help="With --reset-leads: delete leads only, keep action_log.",
    )
    parser.add_argument(
        "--import-post-csv",
        type=str,
        default="",
        metavar="PATH",
        help="Import Google Sheet post-text CSV (Name, Headline, occupation, Profile Url, Post url, Post text).",
    )
    parser.add_argument(
        "--promote-post-leads",
        action="store_true",
        help="Promote/score/qualify/assign post-text leads (ENRICHED → QUALIFIED → ASSIGNED_TO_ACCOUNT).",
    )
    parser.add_argument(
        "--reset-first",
        action="store_true",
        help="With --import-post-csv: run --reset-leads --confirm before import.",
    )
    args = parser.parse_args()

    if (args.pause_account or "").strip():
        init_schema()
        aid = (args.pause_account or "").strip()
        c = get_connection()
        try:
            set_account_paused(c, aid, True)
            logger.info("Paused account %s (engagement will hold until resumed).", aid)
        finally:
            c.close()
        sys.exit(0)

    if (args.resume_account or "").strip():
        init_schema()
        aid = (args.resume_account or "").strip()
        c = get_connection()
        try:
            set_account_paused(c, aid, False)
            logger.info("Resumed account %s (engagement allowed again).", aid)
        finally:
            c.close()
        sys.exit(0)

    if args.reset_leads:
        if not args.confirm:
            logger.error("--reset-leads requires --confirm")
            sys.exit(1)
        from core.lead_reset import reset_leads_database

        init_schema()
        c = get_connection()
        try:
            backup = reset_leads_database(c, leads_only=args.leads_only, backup=True)
            if backup:
                logger.info("Database backup: %s", backup)
            logger.info(
                "Reset complete: leads deleted%s",
                "" if args.leads_only else "; action_log cleared",
            )
        finally:
            c.close()
        sys.exit(0)

    if (args.import_post_csv or "").strip():
        from core.post_lead_pipeline import import_post_csv_file, promote_post_import_leads
        from core.lead_reset import reset_leads_database

        csv_path = (args.import_post_csv or "").strip()
        if not Path(csv_path).is_file():
            logger.error("CSV not found: %s", csv_path)
            sys.exit(1)
        if args.reset_first:
            if not args.confirm:
                logger.error("--reset-first requires --confirm")
                sys.exit(1)
            init_schema()
            c = get_connection()
            try:
                backup = reset_leads_database(c, leads_only=False, backup=True)
                if backup:
                    logger.info("Database backup: %s", backup)
            finally:
                c.close()
        init_schema()
        c = get_connection()
        try:
            imported, skipped, errors = import_post_csv_file(c, csv_path, args.campaign)
            logger.info("Post CSV import: imported=%s skipped=%s", imported, skipped)
            if errors:
                logger.warning("Import errors (sample): %s", errors[:5])
            stats = promote_post_import_leads(c, accounts_path=args.accounts_config)
            logger.info("Post lead pipeline: %s", stats)
        finally:
            c.close()
        sys.exit(0)

    if args.promote_post_leads:
        from core.post_lead_pipeline import promote_post_import_leads

        init_schema()
        c = get_connection()
        try:
            stats = promote_post_import_leads(c, accounts_path=args.accounts_config)
            logger.info("Post lead pipeline: %s", stats)
        finally:
            c.close()
        sys.exit(0)

    if args.ingest_reply:
        init_schema()
        c = get_connection()
        try:
            terminal = ingest_inbound_reply(c, args.ingest_reply[0].strip(), args.ingest_reply[1])
            logger.info("Ingested reply for %s → %s", args.ingest_reply[0], terminal)
        finally:
            c.close()
        sys.exit(0)

    if args.apollo_web3:
        path = run_apollo_web3_extraction(target=args.target)
        logger.info("Apollo Web3 extraction finished: %s", path)
        _refresh_lookup_merge()
        sys.exit(0)

    if args.daily_automation:
        init_schema()
        run_daily_automation(
            accounts_path=args.accounts_config,
            dry_run_engagement=resolve_engagement_dry_run(
                cli_dry=args.dry_run, cli_live=args.live
            ),
            max_leads_per_account=args.max_leads,
        )
        sys.exit(0)

    if args.promote_connected:
        init_schema()
        c = get_connection()
        try:
            promote_to_connected(c, args.promote_connected.strip())
            logger.info("Promoted lead %s to CONNECTED", args.promote_connected)
        finally:
            c.close()
        sys.exit(0)

    if args.intelligence_export:
        init_schema()
        c = get_connection()
        try:
            paths = export_intelligence(c)
            logger.info("Exported: %s", paths)
        finally:
            c.close()
        sys.exit(0)

    if args.engagement_only or args.multi_account_run:
        init_schema()
        idle_seconds = max(
            5, int((os.getenv("ENGAGEMENT_IDLE_SECONDS") or "60").strip() or 60)
        )
        logger.info(
            "Engagement worker loop (idle %ss when no actions taken; Ctrl+C to stop)",
            idle_seconds,
        )
        while True:
            try:
                actions = run_engagement(
                    dry_run=resolve_engagement_dry_run(
                        cli_dry=args.dry_run, cli_live=args.live
                    ),
                    multi_account=not args.single_account,
                    config_path=args.accounts_config,
                    max_leads_per_account=args.max_leads,
                )
                if actions <= 0:
                    logger.info(
                        "Engagement pass: no actions taken across accounts; sleeping %ss",
                        idle_seconds,
                    )
                    time.sleep(idle_seconds)
            except KeyboardInterrupt:
                logger.info("Engagement worker stopped")
                break
        sys.exit(0)

    if args.legacy_full:
        run_legacy_full_pipeline(
            target_leads=args.target,
            campaign_id=args.campaign,
            skip_apollo=args.skip_apollo,
        )
        sys.exit(0)

    if args.outreach_only:
        run_daily_outreach(campaign_id=args.campaign)
        sys.exit(0)

    if args.schedule:
        import schedule

        daily_time = (os.getenv("DAILY_SCHEDULE_TIME") or "07:00").strip() or "07:00"
        acct_path = args.accounts_config

        def _scheduled_daily() -> None:
            init_schema()
            run_daily_automation(
                accounts_path=acct_path,
                dry_run_engagement=resolve_engagement_dry_run(cli_dry=False, cli_live=False),
                max_leads_per_account=None,
            )

        logger.info(
            "Scheduler: daily automation at %s (Apollo Web3 + merge + SQLite + score + engagement). "
            "Override time with DAILY_SCHEDULE_TIME=HH:MM",
            daily_time,
        )
        schedule.every().day.at(daily_time).do(_scheduled_daily)
        while True:
            schedule.run_pending()
            time.sleep(60)

    # Default + --full-run: PRD v2 SQLite pipeline
    run_full_pipeline_v2(
        target_leads=args.target,
        skip_apollo=args.skip_apollo,
        resume=args.resume,
        accounts_path=args.accounts_config,
    )
    _refresh_lookup_merge()
