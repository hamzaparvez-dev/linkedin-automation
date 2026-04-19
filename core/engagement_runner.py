"""Multi-account engagement: connect + first DM + follow-ups via Phantombuster (PRD §4, §8)."""

from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from config import (
    ACCOUNT_CONFIG_PATH,
    ENGAGEMENT_AUTO_PAUSE_ACCOUNT_ON_PB_AUTH_FAILURE,
    ENGAGEMENT_MAX_LEADS_PER_ACCOUNT,
    PHANTOMBUSTER_CONNECT_AGENT_ID,
    PHANTOMBUSTER_DM_AGENT_ID,
    STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS,
    follow_up_eligibility_gaps,
)
from core.accounts_loader import AccountConfig, load_accounts_document
from core.ai_engine import OutreachResult, validate_outreach_plaintext
from core.behavior_controller import approve_action, record_action_executed, set_account_paused
from core.phantom_payload import (
    build_engagement_argument,
    looks_like_linkedin_session_cookie,
    looks_plausible_browser_user_agent,
    merge_phantom_launch_defaults,
    normalize_session_cookie_for_bonus,
    phantom_failure_suggests_linkedin_session_issue,
    summarize_phantom_result,
    validate_engagement_argument,
)
from core.repository import (
    append_message_history,
    bump_metric,
    get_account_linkedin_profile,
    get_account_user_agent,
    get_connection,
    log_action,
    recent_messages_for_repetition,
    record_sent_message,
    row_to_lead_dict,
    sync_accounts_meta,
    transition_lead_status,
    utc_now_iso,
)
from core.strategy_engine import (
    compose_connect_note,
    compose_followup_message,
    compose_from_template,
    pick_strategy,
)
from integrations.phantombuster_client import PhantombusterClient

logger = logging.getLogger(__name__)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _calendar_days_since_iso(ts: Optional[str]) -> int:
    """Whole calendar days elapsed since the UTC calendar day of `ts` (inclusive → 0 on same day)."""
    t = _parse_ts(ts)
    if not t:
        return -1
    event_day = t.astimezone(timezone.utc).date()
    today = datetime.now(timezone.utc).date()
    return (today - event_day).days


# Do not automate outbound if lead already engaged terminally or awaiting human.
_STOP_OUTBOUND_STATUSES_SQL = (
    "AND leads.status NOT IN ('REPLIED','POSITIVE','NEGATIVE','HUMAN_REVIEW')"
)


def _connect_agent_id(account: AccountConfig) -> str:
    return (account.phantombuster_connect_agent_id or PHANTOMBUSTER_CONNECT_AGENT_ID or "").strip()


def _dm_agent_id(account: AccountConfig) -> str:
    return (account.phantombuster_dm_agent_id or PHANTOMBUSTER_DM_AGENT_ID or "").strip()


def _shared_phantom_agent_ids(accounts: list[AccountConfig], *, kind: str) -> frozenset[str]:
    """Non-empty numeric agent ids used by 2+ accounts (same phantom slot shared across identities)."""
    counts: dict[str, int] = {}
    for a in accounts:
        if kind == "connect":
            rid = (a.phantombuster_connect_agent_id or PHANTOMBUSTER_CONNECT_AGENT_ID or "").strip()
        elif kind == "dm":
            rid = (a.phantombuster_dm_agent_id or PHANTOMBUSTER_DM_AGENT_ID or "").strip()
        else:
            raise ValueError("kind must be connect or dm")
        if rid.isdigit():
            counts[rid] = counts.get(rid, 0) + 1
    return frozenset(aid for aid, n in counts.items() if n >= 2)


def _resolve_engagement_user_agent(conn, account: AccountConfig) -> str:
    """SQLite accounts_meta → accounts.json → warn + PHANTOMBUSTER_USER_AGENT."""
    from config import PHANTOMBUSTER_USER_AGENT

    ua = get_account_user_agent(conn, account.account_id).strip()
    if not ua:
        ua = (account.user_agent or "").strip()
    if not ua:
        ua = (PHANTOMBUSTER_USER_AGENT or "").strip()
        if ua:
            logger.warning(
                "account %s: no user_agent in SQLite/accounts.json; using PHANTOMBUSTER_USER_AGENT fallback",
                account.account_id,
            )
    if ua and not looks_plausible_browser_user_agent(ua):
        logger.warning(
            "account %s: user_agent may not look like a browser UA (prefix): %s",
            account.account_id,
            ua[:120],
        )
    return ua


def _build_connect_argument(linkedin_url: str, message: str) -> dict:
    return build_engagement_argument(linkedin_url, message)


def _build_dm_argument(linkedin_url: str, message: str) -> dict:
    return build_engagement_argument(linkedin_url, message)


def _engagement_bonus_argument(linkedin_session: str, user_agent: str) -> Optional[dict[str, str]]:
    """Per-launch session + UA override for POST /agents/launch bonusArgument."""
    ua = (user_agent or "").strip()
    if not ua:
        return None
    raw = (linkedin_session or "").strip()
    if not raw or not looks_like_linkedin_session_cookie(raw):
        return None
    sc = normalize_session_cookie_for_bonus(raw)
    return {"sessionCookie": sc, "userAgent": ua}


def _merge_engagement_phantom_argument(
    linkedin_session: str,
    built: dict[str, Any],
    *,
    user_agent: str,
) -> tuple[dict[str, Any], Optional[dict[str, str]]]:
    bonus = _engagement_bonus_argument(linkedin_session, user_agent)
    if bonus:
        arg = merge_phantom_launch_defaults(built, session_hint="", omit_session_fields=True)
        return arg, bonus
    arg = merge_phantom_launch_defaults(built, session_hint=linkedin_session)
    return arg, None


def _log_detail(stage: str, strategy: str, src: str) -> str:
    return f"stage={stage}|strategy={strategy}|src={src}"[:500]


def _phantom_log(pb_summary: str, ores: OutreachResult) -> str:
    part = f"llm={ores.raw_llm_snippet[:800]}" if ores.raw_llm_snippet else ""
    if pb_summary:
        return (pb_summary + " | " + part)[:4000]
    return part[:4000]


def _maybe_auto_pause_account_on_pb_auth(
    conn,
    account: AccountConfig,
    *,
    ok_pb: bool,
    phantom_summary: str,
    result: Optional[dict[str, Any]] = None,
) -> bool:
    """
    If enabled and the phantom output looks like a bad LinkedIn session, pause this account in SQLite
    so the rest of the engagement run stops (approve_action → account_paused_meta).
    """
    if ok_pb or not ENGAGEMENT_AUTO_PAUSE_ACCOUNT_ON_PB_AUTH_FAILURE:
        return False
    if not phantom_failure_suggests_linkedin_session_issue(phantom_summary, result):
        return False
    set_account_paused(conn, account.account_id, True)
    logger.error(
        "Auto-paused account %s (accounts_meta.paused=1): Phantombuster output suggests an expired or invalid "
        "LinkedIn session. Update linkedin_profile in config/accounts.json, re-connect that identity in "
        "Phantombuster if needed, then run: python main.py --resume-account %s",
        account.account_id,
        account.account_id,
    )
    return True


def _log_phantom_before(
    action: str,
    *,
    account_id: str,
    agent_id: str,
    lead_id: str,
    linkedin_url: str,
    message: str,
) -> None:
    preview = (message or "").replace("\n", " ").strip()[:800]
    logger.info(
        "[phantom_start] action=%s account_id=%s agent_id=%s lead_id=%s linkedin_url=%s message=%s",
        action,
        account_id,
        agent_id,
        lead_id,
        linkedin_url,
        preview,
    )


def _log_phantom_after(
    action: str,
    *,
    account_id: str,
    agent_id: str,
    lead_id: str,
    ok: bool,
    container_id: str,
    summary: str,
) -> None:
    logger.info(
        "[phantom_end] action=%s account_id=%s agent_id=%s lead_id=%s ok=%s container_id=%s summary=%s",
        action,
        account_id,
        agent_id,
        lead_id,
        ok,
        container_id or "(none)",
        (summary or "").replace("\n", " ")[:1200],
    )


def _require_distinct_phantom_ids_per_account(accounts: list[AccountConfig]) -> tuple[bool, str]:
    """
    Multi-account: each row must declare numeric connect + DM phantom ids (env fallback allowed).
    Duplicate ids across accounts are allowed when identities share a Phantombuster slot; a pre-launch
    busy check then serializes launches for those shared ids.
    """
    if len(accounts) < 2:
        return True, ""
    for a in accounts:
        ca = (a.phantombuster_connect_agent_id or PHANTOMBUSTER_CONNECT_AGENT_ID or "").strip()
        da = (a.phantombuster_dm_agent_id or PHANTOMBUSTER_DM_AGENT_ID or "").strip()
        if not ca:
            return (
                False,
                f"accounts.json: account {a.account_id!r} must set phantombuster_connect_agent_id "
                f"(or set PHANTOMBUSTER_CONNECT_AGENT_ID when a single connect phantom is shared).",
            )
        if not ca.isdigit():
            return (
                False,
                f"accounts.json: account {a.account_id!r} phantombuster_connect_agent_id must be numeric (phantom id).",
            )
        if not da:
            return (
                False,
                f"accounts.json: account {a.account_id!r} must set phantombuster_dm_agent_id "
                f"(or set PHANTOMBUSTER_DM_AGENT_ID when shared).",
            )
        if not da.isdigit():
            return (
                False,
                f"accounts.json: account {a.account_id!r} phantombuster_dm_agent_id must be numeric (phantom id).",
            )
    return True, ""


def _validate_distinct_connect_agents(accounts: list[AccountConfig]) -> tuple[bool, str]:
    if not STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS or len(accounts) < 2:
        return True, ""
    seen: dict[str, str] = {}
    for a in accounts:
        cid = _connect_agent_id(a)
        if not cid:
            continue
        if cid in seen:
            return (
                False,
                f"duplicate_connect_agent_id:{cid} accounts {seen[cid]} and {a.account_id}",
            )
        seen[cid] = a.account_id
    return True, ""


def _log_account_phantom_mapping(accounts: list[AccountConfig], conn) -> None:
    for a in accounts:
        li = get_account_linkedin_profile(conn, a.account_id) or a.linkedin_profile
        logger.info(
            "Phantombuster mapping account=%s linkedin_profile=%s connect_agent=%s dm_agent=%s",
            a.account_id,
            li,
            _connect_agent_id(a) or "(unset)",
            _dm_agent_id(a) or "(unset)",
        )


def run_engagement(
    *,
    dry_run: bool = False,
    multi_account: bool = True,
    config_path: Optional[str] = None,
    max_leads_per_account: Optional[int] = None,
) -> None:
    doc = load_accounts_document(config_path or ACCOUNT_CONFIG_PATH)
    accounts = doc.accounts if multi_account else [doc.accounts[0]]
    conn = get_connection()
    sync_accounts_meta(conn, doc)
    ok_pb_ids, reason_pb = _require_distinct_phantom_ids_per_account(accounts)
    if not ok_pb_ids:
        logger.error("Engagement aborted: %s", reason_pb)
        conn.close()
        return
    ok_map, reason = _validate_distinct_connect_agents(accounts)
    if not ok_map:
        logger.error("Engagement aborted: %s", reason)
        conn.close()
        return

    _log_account_phantom_mapping(accounts, conn)
    pb = PhantombusterClient()
    cap = max_leads_per_account if max_leads_per_account is not None else ENGAGEMENT_MAX_LEADS_PER_ACCOUNT
    cap = max(1, int(cap))
    shared_connect_ids = _shared_phantom_agent_ids(accounts, kind="connect")
    shared_dm_ids = _shared_phantom_agent_ids(accounts, kind="dm")

    for account in accounts:
        logger.info(
            "Engagement for account %s (dry_run=%s, max_leads=%s)",
            account.account_id,
            dry_run,
            cap,
        )
        _process_connects(
            conn, pb, account, dry_run=dry_run, sql_limit=cap, shared_connect_ids=shared_connect_ids
        )
        _process_dms(conn, pb, account, dry_run=dry_run, sql_limit=cap, shared_dm_ids=shared_dm_ids)
        _process_followup_1(conn, pb, account, dry_run=dry_run, sql_limit=cap, shared_dm_ids=shared_dm_ids)
        _process_followup_2(conn, pb, account, dry_run=dry_run, sql_limit=cap, shared_dm_ids=shared_dm_ids)
        _process_followup_3(conn, pb, account, dry_run=dry_run, sql_limit=cap, shared_dm_ids=shared_dm_ids)
    conn.close()


def _process_connects(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
    shared_connect_ids: frozenset[str],
) -> None:
    agent_id = _connect_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    user_agent = _resolve_engagement_user_agent(conn, account)
    rows = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='ASSIGNED_TO_ACCOUNT' AND TRIM(leads.linkedin_url) != ''
          {_STOP_OUTBOUND_STATUSES_SQL}
          AND NOT EXISTS (
            SELECT 1 FROM action_log al
            WHERE al.lead_id = leads.lead_id
              AND al.action_type = 'connect'
              AND al.status = 'ok'
              AND al.dry_run = 0
          )
        ORDER BY leads.score DESC, leads.created_at
        LIMIT ?
        """,
        (account.account_id, sql_limit),
    ).fetchall()

    for row in rows:
        approval = approve_action(conn, account, "connect")
        if not approval.allowed:
            logger.info("Connect hold: %s — %s", account.account_id, approval.reason)
            break

        lead = row_to_lead_dict(row)
        lead_id = lead["lead_id"]
        strategy = pick_strategy(conn, account)
        ores: OutreachResult = compose_connect_note(lead, strategy)
        note = ores.text
        recent = recent_messages_for_repetition(conn, account.account_id, 30)
        ok, reason = validate_outreach_plaintext(note, stage="connect", recent_bodies=recent)
        if not ok:
            ores = compose_connect_note(lead, "direct")
            note = ores.text
            ok, _ = validate_outreach_plaintext(note, stage="connect", recent_bodies=recent)
            if not ok:
                logger.warning("Skipping lead %s: connect validation %s", lead_id, reason)
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="connect",
                    status="skipped",
                    detail=reason,
                    strategy_used=strategy,
                    dry_run=dry_run,
                    linkedin_session=linkedin_session,
                )
                continue

        if not agent_id:
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="connect",
                status="skipped",
                detail="no_connect_agent_id",
                strategy_used=strategy,
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            logger.warning("No Phantombuster connect agent id — configure per account or env.")
            continue

        arg, bonus_arg = _merge_engagement_phantom_argument(
            linkedin_session,
            _build_connect_argument(lead["linkedin_url"], note),
            user_agent=user_agent,
        )
        v_ok, v_reason = validate_engagement_argument(arg, bonus_argument=bonus_arg)
        if not v_ok:
            logger.warning("Skipping lead %s: invalid_payload:%s", lead_id, v_reason)
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="connect",
                status="skipped",
                detail=f"invalid_payload:{v_reason}",
                strategy_used=strategy,
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            continue

        detail = _log_detail("connect", strategy, ores.source)
        msg_var = f"{note[:180]}|{detail}"[:500]

        if dry_run:
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="connect",
                status="ok",
                detail="dry_run|" + detail,
                strategy_used=strategy,
                message_variant=msg_var,
                dry_run=True,
                linkedin_session=linkedin_session,
                phantom_response=ores.raw_llm_snippet[:3500],
            )
            logger.info(
                "[dry-run] connect lead=%s account=%s linkedin_profile=%s",
                lead_id,
                account.account_id,
                linkedin_session,
            )
        else:
            phantom_summary = ""
            ok_pb = False
            cid = ""
            result: dict[str, Any] = {}
            _log_phantom_before(
                "connect",
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                linkedin_url=str(lead.get("linkedin_url") or ""),
                message=note,
            )
            if agent_id in shared_connect_ids and pb.is_agent_runtime_busy(agent_id):
                logger.warning(
                    "phantom_slot_busy_yield action=connect agent_id=%s account_id=%s",
                    agent_id,
                    account.account_id,
                )
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="connect",
                    status="skipped",
                    detail="phantom_slot_busy_yield|connect",
                    strategy_used=strategy,
                    dry_run=False,
                    linkedin_session=linkedin_session,
                )
                break
            try:
                result, cid = pb.run_agent(agent_id, arg, timeout_minutes=60, bonus_argument=bonus_arg)
                ok_pb = result.get("status") == "finished"
                phantom_summary = summarize_phantom_result(result)
                _log_phantom_after(
                    "connect",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=ok_pb,
                    container_id=cid,
                    summary=phantom_summary,
                )
                logger.info(
                    "Phantombuster connect finished=%s container=%s account=%s lead=%s profile=%s",
                    ok_pb,
                    cid,
                    account.account_id,
                    lead_id,
                    linkedin_session,
                )
            except Exception as e:
                ok_pb = False
                result = {}
                phantom_summary = f"exception:{type(e).__name__}:{e!s}"[:900]
                _log_phantom_after(
                    "connect",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=False,
                    container_id=cid,
                    summary=phantom_summary,
                )
                logger.exception(
                    "Phantombuster connect failed account=%s lead=%s profile=%s",
                    account.account_id,
                    lead_id,
                    linkedin_session,
                )
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="connect",
                status="ok" if ok_pb else "error",
                detail=("pb_finished|" if ok_pb else "pb_error|") + detail,
                strategy_used=strategy,
                message_variant=msg_var,
                dry_run=False,
                linkedin_session=linkedin_session,
                phantom_response=_phantom_log(phantom_summary, ores),
            )
            if _maybe_auto_pause_account_on_pb_auth(
                conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
            ):
                break
            if ok_pb:
                now = utc_now_iso()
                transition_lead_status(
                    conn,
                    lead_id,
                    "INVITED",
                    invited_at=now,
                    last_action_at=now,
                )
                record_action_executed(conn, account.account_id, "connect")
                record_sent_message(conn, account.account_id, note, strategy)
                bump_metric(
                    conn,
                    date.today().isoformat(),
                    account.account_id,
                    "connections_sent",
                    1,
                )
                append_message_history(conn, lead_id, "outbound_connect", note)

        delay = random.randint(account.delay_min_sec, account.delay_max_sec)
        logger.debug("Post-connect delay %ss (account=%s)", delay, account.account_id)
        time.sleep(delay)


def _process_dms(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
    shared_dm_ids: frozenset[str],
) -> None:
    agent_id = _dm_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    user_agent = _resolve_engagement_user_agent(conn, account)
    rows = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='CONNECTED' AND leads.first_dm_sent_at IS NULL
          AND leads.connected_at IS NOT NULL
          {_STOP_OUTBOUND_STATUSES_SQL}
        ORDER BY leads.connected_at ASC
        LIMIT ?
        """,
        (account.account_id, sql_limit),
    ).fetchall()

    min_days_after_connect, _, _, _ = follow_up_eligibility_gaps()

    for row in rows:
        ca = row["connected_at"]
        if not ca or _calendar_days_since_iso(str(ca)) < min_days_after_connect:
            continue

        approval = approve_action(conn, account, "dm")
        if not approval.allowed:
            logger.info("DM hold: %s — %s", account.account_id, approval.reason)
            break

        lead = row_to_lead_dict(row)
        lead_id = lead["lead_id"]
        strategy = pick_strategy(conn, account)
        recent = recent_messages_for_repetition(conn, account.account_id, 50)
        ores: OutreachResult = compose_from_template(lead, strategy, recent_bodies=recent)
        body = ores.text
        ok, reason = validate_outreach_plaintext(body, stage="dm", recent_bodies=recent)
        if not ok:
            ores = compose_from_template(lead, "direct", recent_bodies=recent)
            body = ores.text
            ok, _ = validate_outreach_plaintext(body, stage="dm", recent_bodies=recent)
            if not ok:
                logger.warning("Skipping DM %s: %s", lead_id, reason)
                continue

        if not agent_id:
            logger.warning("No DM agent id — skip DM for %s", lead_id)
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="skipped",
                detail="no_dm_agent_id",
                strategy_used=strategy,
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            continue

        arg, bonus_arg = _merge_engagement_phantom_argument(
            linkedin_session,
            _build_dm_argument(lead["linkedin_url"], body),
            user_agent=user_agent,
        )
        v_ok, v_reason = validate_engagement_argument(arg, bonus_argument=bonus_arg)
        if not v_ok:
            logger.warning("Skipping lead %s: invalid_payload:%s", lead_id, v_reason)
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="skipped",
                detail=f"invalid_payload:{v_reason}",
                strategy_used=strategy,
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            continue

        detail = _log_detail("dm", strategy, ores.source)
        msg_var = f"{body[:180]}|{detail}"[:500]

        if dry_run:
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="ok",
                detail="dry_run|" + detail,
                strategy_used=strategy,
                message_variant=msg_var,
                dry_run=True,
                linkedin_session=linkedin_session,
                phantom_response=ores.raw_llm_snippet[:3500],
            )
            logger.info(
                "[dry-run] dm lead=%s account=%s linkedin_profile=%s",
                lead_id,
                account.account_id,
                linkedin_session,
            )
        else:
            phantom_summary = ""
            ok_pb = False
            cid = ""
            result: dict[str, Any] = {}
            _log_phantom_before(
                "dm",
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                linkedin_url=str(lead.get("linkedin_url") or ""),
                message=body,
            )
            if agent_id in shared_dm_ids and pb.is_agent_runtime_busy(agent_id):
                logger.warning(
                    "phantom_slot_busy_yield action=dm agent_id=%s account_id=%s",
                    agent_id,
                    account.account_id,
                )
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="dm",
                    status="skipped",
                    detail="phantom_slot_busy_yield|dm",
                    strategy_used=strategy,
                    dry_run=False,
                    linkedin_session=linkedin_session,
                )
                break
            try:
                result, cid = pb.run_agent(agent_id, arg, timeout_minutes=60, bonus_argument=bonus_arg)
                ok_pb = result.get("status") == "finished"
                phantom_summary = summarize_phantom_result(result)
                _log_phantom_after(
                    "dm",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=ok_pb,
                    container_id=cid,
                    summary=phantom_summary,
                )
                logger.info(
                    "Phantombuster DM finished=%s container=%s account=%s lead=%s profile=%s",
                    ok_pb,
                    cid,
                    account.account_id,
                    lead_id,
                    linkedin_session,
                )
            except Exception as e:
                ok_pb = False
                result = {}
                phantom_summary = f"exception:{type(e).__name__}:{e!s}"[:900]
                _log_phantom_after(
                    "dm",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=False,
                    container_id=cid,
                    summary=phantom_summary,
                )
                logger.exception(
                    "DM phantom failed account=%s lead=%s profile=%s",
                    account.account_id,
                    lead_id,
                    linkedin_session,
                )
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="ok" if ok_pb else "error",
                detail=("first_dm:pb_finished|" if ok_pb else "first_dm:pb_error|") + detail,
                strategy_used=strategy,
                message_variant=msg_var,
                dry_run=False,
                linkedin_session=linkedin_session,
                phantom_response=_phantom_log(phantom_summary, ores),
            )
            if _maybe_auto_pause_account_on_pb_auth(
                conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
            ):
                break
            if ok_pb:
                now = utc_now_iso()
                transition_lead_status(
                    conn,
                    lead_id,
                    "MESSAGED",
                    first_dm_sent_at=now,
                    last_action_at=now,
                )
                record_action_executed(conn, account.account_id, "dm")
                record_sent_message(conn, account.account_id, body, strategy)
                bump_metric(
                    conn,
                    date.today().isoformat(),
                    account.account_id,
                    "messages_sent",
                    1,
                )
                append_message_history(conn, lead_id, "outbound_dm", body)

        delay = random.randint(account.delay_min_sec, account.delay_max_sec)
        logger.debug("Post-DM delay %ss (account=%s)", delay, account.account_id)
        time.sleep(delay)


def _process_followup_1(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
    shared_dm_ids: frozenset[str],
) -> None:
    _, gap_fu1, _, _ = follow_up_eligibility_gaps()
    agent_id = _dm_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    rows_fu1 = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='MESSAGED'
          AND leads.first_dm_sent_at IS NOT NULL
          AND (leads.followup_1_sent_at IS NULL OR leads.followup_1_sent_at = '')
          {_STOP_OUTBOUND_STATUSES_SQL}
        ORDER BY leads.first_dm_sent_at ASC
        LIMIT ?
        """,
        (account.account_id, sql_limit),
    ).fetchall()

    for row in rows_fu1:
        fts = row["first_dm_sent_at"]
        if not fts or _calendar_days_since_iso(str(fts)) < gap_fu1:
            continue
        cont = _send_followup_dm(
            conn,
            pb,
            account,
            row,
            stage_num=1,
            dry_run=dry_run,
            linkedin_session=linkedin_session,
            agent_id=agent_id,
            shared_dm_ids=shared_dm_ids,
        )
        if not cont:
            break


def _process_followup_2(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
    shared_dm_ids: frozenset[str],
) -> None:
    _, _, gap_fu2, _ = follow_up_eligibility_gaps()
    agent_id = _dm_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    rows_fu2 = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='FOLLOW_UP_1'
          AND leads.followup_1_sent_at IS NOT NULL
          AND (leads.followup_2_sent_at IS NULL OR leads.followup_2_sent_at = '')
          {_STOP_OUTBOUND_STATUSES_SQL}
        ORDER BY leads.followup_1_sent_at ASC
        LIMIT ?
        """,
        (account.account_id, sql_limit),
    ).fetchall()

    for row in rows_fu2:
        ts = row["followup_1_sent_at"]
        if not ts or _calendar_days_since_iso(str(ts)) < gap_fu2:
            continue
        cont = _send_followup_dm(
            conn,
            pb,
            account,
            row,
            stage_num=2,
            dry_run=dry_run,
            linkedin_session=linkedin_session,
            agent_id=agent_id,
            shared_dm_ids=shared_dm_ids,
        )
        if not cont:
            break


def _process_followup_3(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
    shared_dm_ids: frozenset[str],
) -> None:
    _, _, _, gap_fu3 = follow_up_eligibility_gaps()
    agent_id = _dm_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    rows_fu3 = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='FOLLOW_UP_2'
          AND leads.followup_2_sent_at IS NOT NULL
          AND (leads.followup_3_sent_at IS NULL OR leads.followup_3_sent_at = '')
          {_STOP_OUTBOUND_STATUSES_SQL}
        ORDER BY leads.followup_2_sent_at ASC
        LIMIT ?
        """,
        (account.account_id, sql_limit),
    ).fetchall()

    for row in rows_fu3:
        ts = row["followup_2_sent_at"]
        if not ts or _calendar_days_since_iso(str(ts)) < gap_fu3:
            continue
        cont = _send_followup_dm(
            conn,
            pb,
            account,
            row,
            stage_num=3,
            dry_run=dry_run,
            linkedin_session=linkedin_session,
            agent_id=agent_id,
            shared_dm_ids=shared_dm_ids,
        )
        if not cont:
            break


def _send_followup_dm(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    row,
    *,
    stage_num: int,
    dry_run: bool,
    linkedin_session: str,
    agent_id: str,
    shared_dm_ids: frozenset[str],
) -> bool:
    approval = approve_action(conn, account, "dm")
    if not approval.allowed:
        logger.info("Follow-up DM hold: %s — %s", account.account_id, approval.reason)
        return True

    if stage_num not in (1, 2, 3):
        logger.warning("Invalid follow-up stage %s", stage_num)
        return True

    user_agent = _resolve_engagement_user_agent(conn, account)
    lead = row_to_lead_dict(row)
    lead_id = lead["lead_id"]
    strategy = pick_strategy(conn, account)
    recent = recent_messages_for_repetition(conn, account.account_id, 50)
    stage_key = ("followup_1", "followup_2", "followup_3")[stage_num - 1]
    ores: OutreachResult = compose_followup_message(lead, strategy, stage_num, recent_bodies=recent)
    body = ores.text
    ok, reason = validate_outreach_plaintext(body, stage=stage_key, recent_bodies=recent)
    if not ok:
        ores = compose_followup_message(lead, "direct", stage_num, recent_bodies=recent)
        body = ores.text
        ok, _ = validate_outreach_plaintext(body, stage=stage_key, recent_bodies=recent)
        if not ok:
            logger.warning("Skipping follow-up %s lead=%s: %s", stage_num, lead_id, reason)
            return True

    if not agent_id:
        logger.warning("No DM agent id — skip follow-up for %s", lead_id)
        return True

    arg, bonus_arg = _merge_engagement_phantom_argument(
        linkedin_session,
        _build_dm_argument(lead["linkedin_url"], body),
        user_agent=user_agent,
    )
    v_ok, v_reason = validate_engagement_argument(arg, bonus_argument=bonus_arg)
    if not v_ok:
        logger.warning("Skipping lead %s: invalid_payload:%s", lead_id, v_reason)
        return True

    detail = _log_detail(stage_key, strategy, ores.source)
    msg_var = f"{body[:180]}|{detail}"[:500]

    if dry_run:
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type="dm",
            status="ok",
            detail=f"dry_run|{stage_key}|" + detail,
            strategy_used=strategy,
            message_variant=msg_var,
            dry_run=True,
            linkedin_session=linkedin_session,
            phantom_response=ores.raw_llm_snippet[:3500],
        )
        logger.info("[dry-run] %s lead=%s account=%s", stage_key, lead_id, account.account_id)
    else:
        phantom_summary = ""
        ok_pb = False
        cid = ""
        result: dict[str, Any] = {}
        _log_phantom_before(
            stage_key,
            account_id=account.account_id,
            agent_id=agent_id,
            lead_id=lead_id,
            linkedin_url=str(lead.get("linkedin_url") or ""),
            message=body,
        )
        if agent_id in shared_dm_ids and pb.is_agent_runtime_busy(agent_id):
            logger.warning(
                "phantom_slot_busy_yield action=%s agent_id=%s account_id=%s",
                stage_key,
                agent_id,
                account.account_id,
            )
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="skipped",
                detail=f"phantom_slot_busy_yield|{stage_key}",
                strategy_used=strategy,
                dry_run=False,
                linkedin_session=linkedin_session,
            )
            return False
        try:
            result, cid = pb.run_agent(agent_id, arg, timeout_minutes=60, bonus_argument=bonus_arg)
            ok_pb = result.get("status") == "finished"
            phantom_summary = summarize_phantom_result(result)
            _log_phantom_after(
                stage_key,
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                ok=ok_pb,
                container_id=cid,
                summary=phantom_summary,
            )
        except Exception as e:
            ok_pb = False
            result = {}
            phantom_summary = f"exception:{type(e).__name__}:{e!s}"[:900]
            _log_phantom_after(
                stage_key,
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                ok=False,
                container_id=cid,
                summary=phantom_summary,
            )
            logger.exception("Follow-up DM phantom failed lead=%s", lead_id)
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type="dm",
            status="ok" if ok_pb else "error",
            detail=(f"{stage_key}:pb_finished|" if ok_pb else f"{stage_key}:pb_error|") + detail,
            strategy_used=strategy,
            message_variant=msg_var,
            dry_run=False,
            linkedin_session=linkedin_session,
            phantom_response=_phantom_log(phantom_summary, ores),
        )
        if _maybe_auto_pause_account_on_pb_auth(
            conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
        ):
            return True
        if ok_pb:
            now = utc_now_iso()
            if stage_num == 1:
                transition_lead_status(
                    conn,
                    lead_id,
                    "FOLLOW_UP_1",
                    followup_1_sent_at=now,
                    last_action_at=now,
                )
            elif stage_num == 2:
                transition_lead_status(
                    conn,
                    lead_id,
                    "FOLLOW_UP_2",
                    followup_2_sent_at=now,
                    last_action_at=now,
                )
            else:
                transition_lead_status(
                    conn,
                    lead_id,
                    "FOLLOW_UP_3",
                    followup_3_sent_at=now,
                    last_action_at=now,
                )
            record_action_executed(conn, account.account_id, "dm")
            record_sent_message(conn, account.account_id, body, strategy)
            bump_metric(
                conn,
                date.today().isoformat(),
                account.account_id,
                "messages_sent",
                1,
            )
            append_message_history(conn, lead_id, f"outbound_followup_{stage_num}", body)

    delay = random.randint(account.delay_min_sec, account.delay_max_sec)
    time.sleep(delay)
    return True
