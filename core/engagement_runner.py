"""Multi-account engagement: connect + first DM + follow-ups via Phantombuster (PRD §4, §8)."""

from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from config import (
    ACCOUNT_CONFIG_PATH,
    CONNECT_DEDUPE_COOLDOWN_DAYS,
    CONNECT_DEDUPE_FLAG_FAILED,
    DEDUPE_CONNECT_ASSUME_INVITED,
    DM_INVITED_OPTIMISTIC_FAILURE_COOLDOWN_DAYS,
    DM_NOT_CONNECTED_COOLDOWN_DAYS,
    ENGAGEMENT_AUTO_PAUSE_ACCOUNT_ON_PB_AUTH_FAILURE,
    ENGAGEMENT_MAX_LEADS_PER_ACCOUNT,
    MAX_CONNECT_NOTE_CHARS,
    OPTIMISTIC_FIRST_DM_DAYS,
    PHANTOM_ENGAGEMENT_TIMEOUT_MINUTES,
    PHANTOMBUSTER_CONNECT_AGENT_ID,
    PHANTOMBUSTER_DM_AGENT_ID,
    REQUIRE_POST_TEXT_FOR_LLM,
    STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS,
    follow_up_eligibility_gaps,
)
from core.accounts_loader import AccountConfig, load_accounts_document
from core.ai_engine import OutreachResult, validate_outreach_plaintext
from core.behavior_controller import approve_action, record_action_executed, set_account_paused
from core.phantom_payload import (
    append_fetch_output_to_summary,
    build_dm_message_sender_argument,
    build_engagement_argument,
    looks_like_linkedin_session_cookie,
    looks_plausible_browser_user_agent,
    merge_dm_message_sender_session,
    merge_phantom_launch_defaults,
    normalize_session_cookie_for_bonus,
    phantom_failure_suggests_linkedin_session_issue,
    phantom_connect_deduplication_skipped,
    phantom_outcome_suggests_cannot_message_not_first_degree,
    phantom_outcome_suggests_input_already_processed,
    summarize_phantom_result,
    validate_dm_message_sender_argument,
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
    schedule_next_dm_retry_in_days,
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
from integrations.phantombuster_client import (
    PhantombusterClient,
    format_phantom_api_error,
    is_synthetic_polling_timeout_result,
)

logger = logging.getLogger(__name__)


def _missing_post_text_blocks_outbound(account: AccountConfig, lead: dict[str, Any]) -> bool:
    """When REQUIRE_POST_TEXT_FOR_LLM and account uses llm mode, skip outbound without post_text."""
    if not REQUIRE_POST_TEXT_FOR_LLM:
        return False
    if (account.outreach_copy_mode or "").strip().lower() != "llm":
        return False
    return not (lead.get("post_text") or "").strip()


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


def _next_dm_attempt_blocks_row(row: Any) -> bool:
    """True if next_dm_attempt_at is in the future (retry cooldown after not-1st-degree)."""
    try:
        raw = row["next_dm_attempt_at"]
    except (KeyError, IndexError, TypeError):
        return False
    if not raw or not str(raw).strip():
        return False
    t = _parse_ts(str(raw))
    if not t:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < t


def _row_eligible_for_first_dm(row: Any, *, min_days_after_connect: int) -> bool:
    st = (row["status"] or "").strip()
    if st == "CONNECTED":
        ca = row["connected_at"]
        if not ca or not str(ca).strip():
            return False
        return _calendar_days_since_iso(str(ca)) >= min_days_after_connect
    if st == "INVITED":
        if _next_dm_attempt_blocks_row(row):
            return False
        ia = row["invited_at"]
        if not ia or not str(ia).strip():
            return False
        return _calendar_days_since_iso(str(ia)) >= max(0, int(OPTIMISTIC_FIRST_DM_DAYS))
    return False


# Do not automate outbound if the prospect has replied or reached a terminal / human state.
_STOP_OUTBOUND_STATUSES_SQL = (
    "AND leads.status NOT IN ('REPLIED','POSITIVE','NEGATIVE','NEUTRAL','HUMAN_REVIEW')"
)


def _connect_agent_id(account: AccountConfig) -> str:
    return (account.phantombuster_connect_agent_id or PHANTOMBUSTER_CONNECT_AGENT_ID or "").strip()


def _dm_agent_id(account: AccountConfig) -> str:
    return (account.phantombuster_dm_agent_id or PHANTOMBUSTER_DM_AGENT_ID or "").strip()


def _followup_phantom_log_action(stage_num: int) -> str:
    """[phantom_start] / [phantom_end] action= — distinct from first-dm 'dm' in PM2 (followup_dm_1, …)."""
    if stage_num in (1, 2, 3):
        return f"followup_dm_{stage_num}"
    return "followup_dm"


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
    arg = build_engagement_argument(linkedin_url, message)
    url = (
        arg.get("spreadsheetUrl")
        or arg.get("profileUrl")
        or (arg.get("profileUrls") or [None])[0]
    )
    if url:
        arg["spreadsheetUrl"] = url
        arg["profileUrl"] = url
    return arg


def _build_dm_argument(linkedin_url: str, message: str) -> dict:
    arg = build_dm_message_sender_argument(linkedin_url, message)
    url = arg.get("spreadsheetUrl")
    if url:
        # Message Sender requires spreadsheetUrl; profileUrl omitted (forbidden by DM validator).
        arg["spreadsheetUrl"] = url
    return arg


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


def _merge_dm_phantom_argument(
    linkedin_session: str,
    built: dict[str, Any],
    *,
    user_agent: str,
) -> tuple[dict[str, Any], None]:
    """Message Sender: session/UA on root `argument` only; never `bonusArgument`."""
    arg = merge_dm_message_sender_session(
        built, session_hint=linkedin_session, user_agent=user_agent
    )
    return arg, None


def _log_detail(stage: str, strategy: str, src: str) -> str:
    return f"stage={stage}|strategy={strategy}|src={src}"[:500]


def _phantom_log(pb_summary: str, ores: OutreachResult) -> str:
    part = f"llm={ores.raw_llm_snippet[:800]}" if ores.raw_llm_snippet else ""
    if pb_summary:
        return (pb_summary + " | " + part)[:4000]
    return part[:4000]


def _phantom_effective_outcome(
    pb: PhantombusterClient,
    result: dict[str, Any],
    cid: str,
) -> tuple[bool, bool, list[dict[str, Any]]]:
    """
    Phantombuster can return status=finished while the phantom skipped the line (already in memory).
    Returns (raw_finished, effective_ok, fetch_output_rows).
    """
    raw = isinstance(result, dict) and result.get("status") == "finished"
    out: list[dict[str, Any]] = []
    if raw and cid:
        try:
            out = list(pb.fetch_output(cid) or [])
        except Exception as ex:
            logger.debug("Phantombuster fetch_output: %s", ex)
    is_dedupe = (
        raw
        and phantom_outcome_suggests_input_already_processed(result, fetch_output_rows=out)
    )
    return raw, raw and not is_dedupe, out


def _connect_phantom_outcome(
    pb: PhantombusterClient,
    result: dict[str, Any],
    cid: str,
) -> tuple[bool, bool, list[dict[str, Any]]]:
    """
    Connect-only outcome: finished containers that PB skipped (memory / empty sheet) are not effective_ok.
    """
    raw, _, out = _phantom_effective_outcome(pb, result, cid)
    if not raw:
        return raw, False, out
    if phantom_connect_deduplication_skipped(result, fetch_output_rows=out):
        return raw, False, out
    return raw, True, out


def _pb_error_detail_suffix(phantom_summary: str, *, max_len: int = 1500) -> str:
    s = (phantom_summary or "").strip()
    if not s:
        return ""
    return (s[:max_len] + "|") if len(s) > max_len else (s + "|")


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
    Each account row must declare non-empty connect + DM Phantombuster agent ids (per LinkedIn identity).
    Env fallbacks (PHANTOMBUSTER_CONNECT_AGENT_ID / PHANTOMBUSTER_DM_AGENT_ID) apply when a field is blank.
    Non-numeric values (e.g. UNIQUE_A_CONNECT_ID placeholders) are allowed but log a warning — replace with
    the numeric id from the phantom dashboard URL before production launches.
    """
    for a in accounts:
        ca = (a.phantombuster_connect_agent_id or PHANTOMBUSTER_CONNECT_AGENT_ID or "").strip()
        da = (a.phantombuster_dm_agent_id or PHANTOMBUSTER_DM_AGENT_ID or "").strip()
        if not ca:
            return (
                False,
                f"accounts.json: account {a.account_id!r} must set phantombuster_connect_agent_id "
                f"(or set PHANTOMBUSTER_CONNECT_AGENT_ID as fallback).",
            )
        if not ca.isdigit():
            logger.warning(
                "account %s: phantombuster_connect_agent_id is not numeric (%r) — paste the phantom id from "
                "Phantombuster before production",
                a.account_id,
                ca[:80],
            )
        if not da:
            return (
                False,
                f"accounts.json: account {a.account_id!r} must set phantombuster_dm_agent_id "
                f"(or set PHANTOMBUSTER_DM_AGENT_ID as fallback).",
            )
        if not da.isdigit():
            logger.warning(
                "account %s: phantombuster_dm_agent_id is not numeric (%r) — paste the phantom id from "
                "Phantombuster before production",
                a.account_id,
                da[:80],
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


def _validate_distinct_dm_agents(accounts: list[AccountConfig]) -> tuple[bool, str]:
    """When STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS is true, also forbid duplicate DM phantom ids across accounts."""
    if not STRICT_DISTINCT_PHANTOM_CONNECT_AGENTS or len(accounts) < 2:
        return True, ""
    seen: dict[str, str] = {}
    for a in accounts:
        did = _dm_agent_id(a)
        if not did:
            continue
        if did in seen:
            return (
                False,
                f"duplicate_dm_agent_id:{did} accounts {seen[did]} and {a.account_id}",
            )
        seen[did] = a.account_id
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
    ok_dm, reason_dm = _validate_distinct_dm_agents(accounts)
    if not ok_dm:
        logger.error("Engagement aborted: %s", reason_dm)
        conn.close()
        return

    _log_account_phantom_mapping(accounts, conn)
    pb = PhantombusterClient()
    cap = max_leads_per_account if max_leads_per_account is not None else ENGAGEMENT_MAX_LEADS_PER_ACCOUNT
    cap = max(1, int(cap))

    for account in accounts:
        logger.info(
            "Engagement for account %s (dry_run=%s, max_leads=%s)",
            account.account_id,
            dry_run,
            cap,
        )
        _process_connects(conn, pb, account, dry_run=dry_run, sql_limit=cap)
        _process_dms(conn, pb, account, dry_run=dry_run, sql_limit=cap)
        _process_followup_1(conn, pb, account, dry_run=dry_run, sql_limit=cap)
        _process_followup_2(conn, pb, account, dry_run=dry_run, sql_limit=cap)
        _process_followup_3(conn, pb, account, dry_run=dry_run, sql_limit=cap)
    conn.close()


def _process_connects(
    conn,
    pb: PhantombusterClient,
    account: AccountConfig,
    *,
    dry_run: bool,
    sql_limit: int,
) -> None:
    agent_id = _connect_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    user_agent = _resolve_engagement_user_agent(conn, account)
    now_iso = utc_now_iso()
    rows = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=? AND leads.status='ASSIGNED_TO_ACCOUNT' AND TRIM(leads.linkedin_url) != ''
          {_STOP_OUTBOUND_STATUSES_SQL}
          AND (
            leads.next_dm_attempt_at IS NULL
            OR TRIM(COALESCE(leads.next_dm_attempt_at, '')) = ''
            OR leads.next_dm_attempt_at <= ?
          )
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
        (account.account_id, now_iso, sql_limit),
    ).fetchall()

    for row in rows:
        approval = approve_action(conn, account, "connect")
        if not approval.allowed:
            logger.info("Connect hold: %s — %s", account.account_id, approval.reason)
            break

        lead = row_to_lead_dict(row)
        lead_id = lead["lead_id"]
        if _missing_post_text_blocks_outbound(account, lead):
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="connect",
                status="skipped",
                detail="missing_post_text",
                strategy_used="",
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            continue
        strategy = pick_strategy(conn, account)
        ores: OutreachResult = compose_connect_note(lead, strategy, account=account)
        note = ores.text
        recent = recent_messages_for_repetition(conn, account.account_id, 30)
        ok, reason = validate_outreach_plaintext(note, stage="connect", recent_bodies=recent)
        if not ok:
            ores = compose_connect_note(lead, "direct", account=account)
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
        v_ok, v_reason = validate_engagement_argument(
            arg, bonus_argument=bonus_arg, max_message_chars=MAX_CONNECT_NOTE_CHARS
        )
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
            is_dedupe_skip = False
            is_poll_timeout = False
            try:
                result, cid = pb.run_agent(
                    agent_id,
                    arg,
                    timeout_minutes=PHANTOM_ENGAGEMENT_TIMEOUT_MINUTES,
                    bonus_argument=bonus_arg,
                )
                is_poll_timeout = is_synthetic_polling_timeout_result(result)
                if is_poll_timeout:
                    ok_pb = False
                    is_dedupe_skip = False
                    raw_done = False
                    effective_ok = False
                    out_rows: list[dict[str, Any]] = []
                    base_sum = summarize_phantom_result(result)
                    phantom_summary = base_sum
                else:
                    raw_done, effective_ok, out_rows = _connect_phantom_outcome(pb, result, cid)
                    is_dedupe_skip = bool(raw_done and not effective_ok)
                    ok_pb = bool(effective_ok)
                    base_sum = summarize_phantom_result(result)
                    phantom_summary = append_fetch_output_to_summary(base_sum, out_rows)
                _log_phantom_after(
                    "connect",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=ok_pb and not is_dedupe_skip,
                    container_id=cid,
                    summary=phantom_summary,
                )
                if is_dedupe_skip:
                    logger.info(
                        "Phantombuster connect: finished but line skipped (dedupe / already processed) lead=%s",
                        lead_id,
                    )
                logger.info(
                    "Phantombuster connect finished=%s effective=%s poll_timeout=%s container=%s account=%s lead=%s profile=%s",
                    raw_done,
                    effective_ok,
                    is_poll_timeout,
                    cid,
                    account.account_id,
                    lead_id,
                    linkedin_session,
                )
            except Exception as e:
                ok_pb = False
                result = {}
                is_poll_timeout = False
                phantom_summary = format_phantom_api_error(e)
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
            if is_poll_timeout:
                _conn_detail = "pb_polling_timeout|" + detail
                _conn_status = "error"
            elif is_dedupe_skip:
                _conn_detail = "connect_dedupe_skipped|pb_dedupe_already_processed|" + detail
                _conn_status = "error"
            elif ok_pb:
                _conn_detail = "pb_finished|" + detail
                _conn_status = "ok"
            else:
                _conn_detail = "pb_error|" + _pb_error_detail_suffix(phantom_summary) + detail
                _conn_status = "error"

            persisted_ok = False
            if ok_pb:
                try:
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
                    persisted_ok = True
                except Exception:
                    logger.exception(
                        "Connect state persist failed account=%s lead=%s",
                        account.account_id,
                        lead_id,
                    )
                    persisted_ok = False

            if ok_pb and persisted_ok:
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="connect",
                    status="ok",
                    detail=_conn_detail,
                    strategy_used=strategy,
                    message_variant=msg_var,
                    dry_run=False,
                    linkedin_session=linkedin_session,
                    phantom_response=_phantom_log(phantom_summary, ores),
                )
            elif ok_pb and not persisted_ok:
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="connect",
                    status="error",
                    detail="connect_state_persist_failed|" + _conn_detail,
                    strategy_used=strategy,
                    message_variant=msg_var,
                    dry_run=False,
                    linkedin_session=linkedin_session,
                    phantom_response=_phantom_log(phantom_summary, ores),
                )
            else:
                log_action(
                    conn,
                    lead_id=lead_id,
                    account_id=account.account_id,
                    action_type="connect",
                    status=_conn_status,
                    detail=_conn_detail,
                    strategy_used=strategy,
                    message_variant=msg_var,
                    dry_run=False,
                    linkedin_session=linkedin_session,
                    phantom_response=_phantom_log(phantom_summary, ores),
                )

            if not is_dedupe_skip and not is_poll_timeout and _maybe_auto_pause_account_on_pb_auth(
                conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
            ):
                break

            if is_dedupe_skip:
                if DEDUPE_CONNECT_ASSUME_INVITED:
                    try:
                        now = utc_now_iso()
                        transition_lead_status(
                            conn,
                            lead_id,
                            "INVITED",
                            invited_at=now,
                            last_action_at=now,
                        )
                    except Exception:
                        logger.exception(
                            "Connect assume-invited persist failed account=%s lead=%s",
                            account.account_id,
                            lead_id,
                        )
                else:
                    schedule_next_dm_retry_in_days(
                        conn, lead_id, CONNECT_DEDUPE_COOLDOWN_DAYS
                    )
                    logger.info(
                        "Connect dedupe skip: lead=%s backoff %s days (next_dm_attempt_at)",
                        lead_id,
                        CONNECT_DEDUPE_COOLDOWN_DAYS,
                    )
                    if CONNECT_DEDUPE_FLAG_FAILED:
                        try:
                            transition_lead_status(
                                conn,
                                lead_id,
                                "FAILED",
                                last_action_at=utc_now_iso(),
                            )
                        except Exception:
                            logger.exception(
                                "Connect dedupe FAILED transition failed lead=%s",
                                lead_id,
                            )

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
) -> None:
    agent_id = _dm_agent_id(account)
    linkedin_session = get_account_linkedin_profile(conn, account.account_id) or account.linkedin_profile
    user_agent = _resolve_engagement_user_agent(conn, account)
    min_days_after_connect, _, _, _ = follow_up_eligibility_gaps()
    wide = max(sql_limit * 4, 40)
    raw_rows = conn.execute(
        f"""
        SELECT leads.* FROM leads
        WHERE leads.account_id=?
          AND leads.first_dm_sent_at IS NULL
          AND leads.status IN ('CONNECTED', 'INVITED')
          AND (
            (leads.status='CONNECTED' AND leads.connected_at IS NOT NULL AND TRIM(COALESCE(leads.connected_at,'')) != '')
            OR
            (leads.status='INVITED' AND leads.invited_at IS NOT NULL AND TRIM(COALESCE(leads.invited_at,'')) != '')
          )
          {_STOP_OUTBOUND_STATUSES_SQL}
        ORDER BY COALESCE(leads.connected_at, leads.invited_at) ASC
        LIMIT ?
        """,
        (account.account_id, wide),
    ).fetchall()
    rows: list[Any] = []
    for row in raw_rows:
        if not _row_eligible_for_first_dm(row, min_days_after_connect=min_days_after_connect):
            continue
        rows.append(row)
        if len(rows) >= sql_limit:
            break

    for row in rows:
        approval = approve_action(conn, account, "dm")
        if not approval.allowed:
            logger.info("DM hold: %s — %s", account.account_id, approval.reason)
            break

        lead = row_to_lead_dict(row)
        lead_id = lead["lead_id"]
        if _missing_post_text_blocks_outbound(account, lead):
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status="skipped",
                detail="missing_post_text",
                strategy_used="",
                dry_run=dry_run,
                linkedin_session=linkedin_session,
            )
            continue
        strategy = pick_strategy(conn, account)
        recent = recent_messages_for_repetition(conn, account.account_id, 50)
        ores: OutreachResult = compose_from_template(
            lead, strategy, recent_bodies=recent, account=account
        )
        body = ores.text
        ok, reason = validate_outreach_plaintext(body, stage="dm", recent_bodies=recent)
        if not ok:
            ores = compose_from_template(lead, "direct", recent_bodies=recent, account=account)
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

        arg, _ = _merge_dm_phantom_argument(
            linkedin_session,
            _build_dm_argument(lead["linkedin_url"], body),
            user_agent=user_agent,
        )
        v_ok, v_reason = validate_dm_message_sender_argument(arg)
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
            is_dedupe_skip = False
            is_not_connected = False
            is_poll_timeout = False
            raw_done = False
            try:
                result, cid = pb.run_agent(
                    agent_id, arg, timeout_minutes=PHANTOM_ENGAGEMENT_TIMEOUT_MINUTES, bonus_argument=None
                )
                is_poll_timeout = is_synthetic_polling_timeout_result(result)
                if is_poll_timeout:
                    ok_pb = False
                    is_dedupe_skip = False
                    is_not_connected = False
                    raw_done = False
                    out_rows = []
                    base_sum = summarize_phantom_result(result)
                    phantom_summary = base_sum
                else:
                    raw_done = isinstance(result, dict) and result.get("status") == "finished"
                    out_rows: list[dict[str, Any]] = []
                    if raw_done and cid:
                        try:
                            out_rows = list(pb.fetch_output(cid) or [])
                        except Exception as ex:
                            logger.debug("Phantombuster fetch_output: %s", ex)
                    base_sum = summarize_phantom_result(result)
                    phantom_summary = append_fetch_output_to_summary(base_sum, out_rows)
                    is_not_connected = bool(
                        raw_done
                        and phantom_outcome_suggests_cannot_message_not_first_degree(
                            result, fetch_output_rows=out_rows
                        )
                    )
                    is_dedupe_skip = bool(
                        raw_done
                        and not is_not_connected
                        and phantom_outcome_suggests_input_already_processed(
                            result, fetch_output_rows=out_rows
                        )
                    )
                    ok_pb = bool(raw_done and not is_not_connected and not is_dedupe_skip)
                _log_phantom_after(
                    "dm",
                    account_id=account.account_id,
                    agent_id=agent_id,
                    lead_id=lead_id,
                    ok=ok_pb,
                    container_id=cid,
                    summary=phantom_summary,
                )
                if is_not_connected:
                    logger.info("Phantombuster first DM: not 1st-degree / cannot message lead=%s", lead_id)
                if is_dedupe_skip:
                    logger.info("Phantombuster first DM: dedupe/already processed lead=%s", lead_id)
                logger.info(
                    "Phantombuster DM finished=%s not_connected=%s ok=%s poll_timeout=%s container=%s account=%s lead=%s",
                    raw_done,
                    is_not_connected,
                    ok_pb,
                    is_poll_timeout,
                    cid,
                    account.account_id,
                    lead_id,
                )
            except Exception as e:
                ok_pb = False
                result = {}
                phantom_summary = format_phantom_api_error(e)
                is_not_connected = False
                is_poll_timeout = False
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
            if is_poll_timeout:
                logger.warning(
                    "Phantombuster DM poll timeout; continuing to next lead. account=%s lead=%s",
                    account.account_id,
                    lead_id,
                )
            persisted_ok = False
            if ok_pb:
                try:
                    now = utc_now_iso()
                    transition_lead_status(
                        conn,
                        lead_id,
                        "MESSAGED",
                        first_dm_sent_at=now,
                        last_action_at=now,
                        next_dm_attempt_at=None,
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
                    persisted_ok = True
                except Exception:
                    logger.exception(
                        "First DM state persist failed account=%s lead=%s",
                        account.account_id,
                        lead_id,
                    )
                    persisted_ok = False

            if is_poll_timeout:
                _dm_d = "first_dm:pb_polling_timeout|" + detail
                _dm_st = "error"
            elif is_not_connected:
                _dm_d = "first_dm:pb_not_connected_yet|" + detail
                _dm_st = "skipped"
            elif is_dedupe_skip:
                _dm_d = "first_dm:pb_dedupe_already_processed|" + detail
                _dm_st = "skipped"
            elif ok_pb and persisted_ok:
                _dm_d = "first_dm:pb_finished|" + detail
                _dm_st = "ok"
            elif ok_pb and not persisted_ok:
                _dm_d = "first_dm:state_persist_failed|" + detail
                _dm_st = "error"
            else:
                _dm_d = "first_dm:pb_error|" + _pb_error_detail_suffix(phantom_summary) + detail
                _dm_st = "error"
            log_action(
                conn,
                lead_id=lead_id,
                account_id=account.account_id,
                action_type="dm",
                status=_dm_st,
                detail=_dm_d,
                strategy_used=strategy,
                message_variant=msg_var,
                dry_run=False,
                linkedin_session=linkedin_session,
                phantom_response=_phantom_log(phantom_summary, ores),
            )
            lead_status = str(row["status"] or "").strip()
            if not ok_pb or (ok_pb and not persisted_ok):
                if lead_status == "INVITED":
                    schedule_next_dm_retry_in_days(
                        conn, lead_id, DM_INVITED_OPTIMISTIC_FAILURE_COOLDOWN_DAYS
                    )
                    logger.info(
                        "[Safety Cooldown] Lead %s (INVITED) failed optimistic DM. Applying strict %s-day backoff to save tokens.",
                        lead_id,
                        DM_INVITED_OPTIMISTIC_FAILURE_COOLDOWN_DAYS,
                    )
                elif lead_status == "CONNECTED" and is_not_connected:
                    schedule_next_dm_retry_in_days(conn, lead_id, DM_NOT_CONNECTED_COOLDOWN_DAYS)
            if (
                not is_dedupe_skip
                and not is_not_connected
                and not is_poll_timeout
                and _maybe_auto_pause_account_on_pb_auth(
                    conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
                )
            ):
                break

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
) -> bool:
    approval = approve_action(conn, account, "dm")
    if not approval.allowed:
        logger.info("Follow-up DM hold: %s — %s", account.account_id, approval.reason)
        return True

    if stage_num not in (1, 2, 3):
        logger.warning("Invalid follow-up stage %s", stage_num)
        return True

    if _next_dm_attempt_blocks_row(row):
        return True

    user_agent = _resolve_engagement_user_agent(conn, account)
    lead = row_to_lead_dict(row)
    lead_id = lead["lead_id"]
    if _missing_post_text_blocks_outbound(account, lead):
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type=_followup_phantom_log_action(stage_num),
            status="skipped",
            detail="missing_post_text",
            strategy_used="",
            dry_run=dry_run,
            linkedin_session=linkedin_session,
        )
        return True
    strategy = pick_strategy(conn, account)
    recent = recent_messages_for_repetition(conn, account.account_id, 50)
    stage_key = ("followup_1", "followup_2", "followup_3")[stage_num - 1]
    ores: OutreachResult = compose_followup_message(
        lead, strategy, stage_num, recent_bodies=recent, account=account
    )
    body = ores.text
    ok, reason = validate_outreach_plaintext(body, stage=stage_key, recent_bodies=recent)
    if not ok:
        ores = compose_followup_message(
            lead, "direct", stage_num, recent_bodies=recent, account=account
        )
        body = ores.text
        ok, _ = validate_outreach_plaintext(body, stage=stage_key, recent_bodies=recent)
        if not ok:
            logger.warning("Skipping follow-up %s lead=%s: %s", stage_num, lead_id, reason)
            return True

    if not agent_id:
        logger.warning("No DM agent id (phantombuster_dm_agent_id) — skip followup_dm_%s for %s", stage_num, lead_id)
        return True

    phantom_log_action = _followup_phantom_log_action(stage_num)
    arg, _ = _merge_dm_phantom_argument(
        linkedin_session,
        _build_dm_argument(lead["linkedin_url"], body),
        user_agent=user_agent,
    )
    v_ok, v_reason = validate_dm_message_sender_argument(arg)
    if not v_ok:
        logger.warning("Skipping lead %s: invalid_payload (Message Sender) followup_dm_%s: %s", lead_id, stage_num, v_reason)
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type="dm",
            status="skipped",
            detail=f"followup_dm_{stage_num}|invalid_payload:{v_reason}",
            strategy_used=strategy,
            dry_run=dry_run,
            linkedin_session=linkedin_session,
        )
        return True

    detail = _log_detail(stage_key, strategy, ores.source)
    msg_var = f"{body[:180]}|{detail}"[:500]
    _fu_prefix = f"followup_dm_{stage_num}|"

    if dry_run:
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type="dm",
            status="ok",
            detail=f"dry_run|{phantom_log_action}|{stage_key}|" + detail,
            strategy_used=strategy,
            message_variant=msg_var,
            dry_run=True,
            linkedin_session=linkedin_session,
            phantom_response=ores.raw_llm_snippet[:3500],
        )
        logger.info(
            "[dry-run] %s (followup_dm_%s) lead=%s account=%s",
            phantom_log_action,
            stage_num,
            lead_id,
            account.account_id,
        )
    else:
        phantom_summary = ""
        ok_pb = False
        cid = ""
        result: dict[str, Any] = {}
        _log_phantom_before(
            phantom_log_action,
            account_id=account.account_id,
            agent_id=agent_id,
            lead_id=lead_id,
            linkedin_url=str(lead.get("linkedin_url") or ""),
            message=body,
        )
        is_dedupe_skip = False
        is_not_connected = False
        is_poll_timeout = False
        raw_done = False
        try:
            result, cid = pb.run_agent(
                agent_id, arg, timeout_minutes=PHANTOM_ENGAGEMENT_TIMEOUT_MINUTES, bonus_argument=None
            )
            is_poll_timeout = is_synthetic_polling_timeout_result(result)
            if is_poll_timeout:
                ok_pb = False
                is_dedupe_skip = False
                is_not_connected = False
                raw_done = False
                out_rows: list[dict[str, Any]] = []
                base_sum = summarize_phantom_result(result)
                phantom_summary = base_sum
            else:
                raw_done = isinstance(result, dict) and result.get("status") == "finished"
                out_rows: list[dict[str, Any]] = []
                if raw_done and cid:
                    try:
                        out_rows = list(pb.fetch_output(cid) or [])
                    except Exception as ex:
                        logger.debug("Phantombuster fetch_output: %s", ex)
                base_sum = summarize_phantom_result(result)
                phantom_summary = append_fetch_output_to_summary(base_sum, out_rows)
                is_not_connected = bool(
                    raw_done
                    and phantom_outcome_suggests_cannot_message_not_first_degree(
                        result, fetch_output_rows=out_rows
                    )
                )
                is_dedupe_skip = bool(
                    raw_done
                    and not is_not_connected
                    and phantom_outcome_suggests_input_already_processed(
                        result, fetch_output_rows=out_rows
                    )
                )
                ok_pb = bool(raw_done and not is_not_connected and not is_dedupe_skip)
            _log_phantom_after(
                phantom_log_action,
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                ok=ok_pb,
                container_id=cid,
                summary=phantom_summary,
            )
            if is_not_connected:
                logger.info(
                    "Phantombuster %s: not 1st-degree / cannot message lead=%s",
                    phantom_log_action,
                    lead_id,
                )
            if is_dedupe_skip:
                logger.info("Phantombuster %s: dedupe/already processed lead=%s", phantom_log_action, lead_id)
            logger.info(
                "Phantombuster %s finished=%s not_connected=%s ok=%s poll_timeout=%s container=%s account=%s lead=%s",
                phantom_log_action,
                raw_done,
                is_not_connected,
                ok_pb,
                is_poll_timeout,
                cid,
                account.account_id,
                lead_id,
            )
        except Exception as e:
            ok_pb = False
            result = {}
            is_not_connected = False
            is_poll_timeout = False
            is_dedupe_skip = False
            phantom_summary = format_phantom_api_error(e)
            _log_phantom_after(
                phantom_log_action,
                account_id=account.account_id,
                agent_id=agent_id,
                lead_id=lead_id,
                ok=False,
                container_id=cid,
                summary=phantom_summary,
            )
            logger.exception("Follow-up DM (Message Sender) phantom failed lead=%s", lead_id)
        if is_poll_timeout:
            logger.warning(
                "Phantombuster %s poll timeout; continuing to next lead. account=%s lead=%s",
                phantom_log_action,
                account.account_id,
                lead_id,
            )
        persisted_ok = False
        if ok_pb:
            try:
                now = utc_now_iso()
                if stage_num == 1:
                    transition_lead_status(
                        conn,
                        lead_id,
                        "FOLLOW_UP_1",
                        followup_1_sent_at=now,
                        last_action_at=now,
                        next_dm_attempt_at=None,
                    )
                elif stage_num == 2:
                    transition_lead_status(
                        conn,
                        lead_id,
                        "FOLLOW_UP_2",
                        followup_2_sent_at=now,
                        last_action_at=now,
                        next_dm_attempt_at=None,
                    )
                else:
                    transition_lead_status(
                        conn,
                        lead_id,
                        "FOLLOW_UP_3",
                        followup_3_sent_at=now,
                        last_action_at=now,
                        next_dm_attempt_at=None,
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
                persisted_ok = True
            except Exception:
                logger.exception(
                    "Follow-up state persist failed account=%s lead=%s stage=%s",
                    account.account_id,
                    lead_id,
                    stage_num,
                )
                persisted_ok = False

        if is_poll_timeout:
            _fu_d = f"{_fu_prefix}{stage_key}:pb_polling_timeout|" + detail
            _fu_st = "error"
        elif is_not_connected:
            _fu_d = f"{_fu_prefix}{stage_key}:pb_not_connected_yet|" + detail
            _fu_st = "skipped"
        elif is_dedupe_skip:
            _fu_d = f"{_fu_prefix}{stage_key}:pb_dedupe_already_processed|" + detail
            _fu_st = "skipped"
        elif ok_pb and persisted_ok:
            _fu_d = f"{_fu_prefix}{stage_key}:pb_finished|" + detail
            _fu_st = "ok"
        elif ok_pb and not persisted_ok:
            _fu_d = f"{_fu_prefix}{stage_key}:state_persist_failed|" + detail
            _fu_st = "error"
        else:
            _fu_d = f"{_fu_prefix}{stage_key}:pb_error|" + _pb_error_detail_suffix(phantom_summary) + detail
            _fu_st = "error"
        log_action(
            conn,
            lead_id=lead_id,
            account_id=account.account_id,
            action_type="dm",
            status=_fu_st,
            detail=_fu_d,
            strategy_used=strategy,
            message_variant=msg_var,
            dry_run=False,
            linkedin_session=linkedin_session,
            phantom_response=_phantom_log(phantom_summary, ores),
        )
        if is_not_connected:
            schedule_next_dm_retry_in_days(conn, lead_id, DM_NOT_CONNECTED_COOLDOWN_DAYS)
        if (
            not is_dedupe_skip
            and not is_not_connected
            and not is_poll_timeout
            and _maybe_auto_pause_account_on_pb_auth(
                conn, account, ok_pb=ok_pb, phantom_summary=phantom_summary, result=result
            )
        ):
            return True

    delay = random.randint(account.delay_min_sec, account.delay_max_sec)
    time.sleep(delay)
    return True
