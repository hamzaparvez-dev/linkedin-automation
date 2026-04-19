"""Validate Phantombuster engagement payloads before launch (connect / DM)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Minimal LinkedIn profile URL shape (http(s) + linkedin.com + /in/ or /company/)
_LI_PATH = re.compile(
    r"^https?://(?:[\w.-]+\.)?linkedin\.com/(?:in|company)/[^/?\s]+/?",
    re.IGNORECASE,
)

# Strict /in/ profile URL for singular-mode phantoms: no trailing slash, no query (normalized form)
_IN_PATH = re.compile(r"^https://www\.linkedin\.com/in/[^/?#\s]+$", re.IGNORECASE)

_SINGULAR_ARG_KEYS = frozenset({"profileUrl", "numberOfAddsPerLaunch", "message"})
_MAX_CONNECT_MESSAGE_LEN = 300  # Phantom: message length must be < 300 characters


def normalize_engagement_linkedin_url(raw: Optional[str]) -> Optional[str]:
    """
    Canonical profile URL for LinkedIn Auto Connect (singular):
    https://www.linkedin.com/in/{handle} — no trailing slash, query/fragment stripped, trimmed.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("http://", "https://", 1)
    if not s.startswith("https://"):
        s = "https://" + s.lstrip("/")
    u = urlparse(s)
    host = (u.netloc or "").lower()
    if "linkedin.com" not in host:
        return None
    path = "/" + (u.path or "").strip().lstrip("/")
    if not path.lower().startswith("/in/"):
        return None
    m = re.match(r"^/in/([^/?#\s]+)/?$", path, re.IGNORECASE)
    if not m:
        return None
    handle = m.group(1).strip()
    if not handle:
        return None
    return f"https://www.linkedin.com/in/{handle}"


def normalize_session_cookie_for_bonus(raw: str) -> str:
    """
    Phantombuster bonusArgument expects a session string that includes the li_at assignment.
    Accepts either a full cookie fragment (already containing li_at=) or the raw li_at value.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "li_at=" in s:
        return s
    return f"li_at={s}"


def validate_phantom_bonus_argument(bonus: Any) -> tuple[bool, str]:
    """Validate bonusArgument dict for POST /agents/launch (LinkedIn session override)."""
    if not isinstance(bonus, dict):
        return False, "bonus_argument_not_object"
    sc = bonus.get("sessionCookie")
    if not isinstance(sc, str) or not sc.strip():
        return False, "bonus_missing_sessionCookie"
    if "li_at=" not in sc:
        return False, "bonus_sessionCookie_must_contain_li_at="
    ua = bonus.get("userAgent")
    if not isinstance(ua, str) or not ua.strip():
        return False, "bonus_userAgent_non_empty_required"
    return True, ""


def redact_phantom_bonus_argument_for_log(bonus: dict[str, Any]) -> dict[str, Any]:
    d = dict(bonus)
    sc = d.get("sessionCookie")
    if isinstance(sc, str) and sc:
        d["sessionCookie"] = f"{sc[:16]}…({len(sc)} chars)"
    return d


def looks_like_linkedin_session_cookie(value: str) -> bool:
    """Heuristic: Phantombuster li_at / LinkedIn export cookies are long opaque strings."""
    v = (value or "").strip()
    if len(v) < 80:
        return False
    if v.startswith("AQED") or v.startswith("li_at") or v.startswith("AUE"):
        return True
    return False


def looks_plausible_browser_user_agent(ua: str) -> bool:
    """
    Permissive check for a real browser-style User-Agent (warn-only).
    Expects Mozilla/5.0 and at least one of WebKit / Gecko / Chrome / Safari / Edg / Firefox hints.
    """
    s = (ua or "").strip()
    if len(s) < 20:
        return False
    if not s.startswith("Mozilla/5.0"):
        return False
    low = s.lower()
    hints = ("webkit", "gecko", "chrome/", "safari/", "edg/", "firefox/", "version/")
    return any(h in low for h in hints)


def merge_phantom_launch_defaults(
    argument: dict[str, Any],
    *,
    session_hint: str = "",
    omit_session_fields: bool = False,
) -> dict[str, Any]:
    """
    Singular (LinkedIn Auto Connect): argument stays minimal — only profileUrl, numberOfAddsPerLaunch,
    message (session/auth via bonusArgument when omit_session_fields=True).
    Array / scraper mode: merge spreadsheet + caps as before.
    """
    from config import (
        PHANTOMBUSTER_DWELL_TIME,
        PHANTOMBUSTER_EMAIL_CHOOSER,
        PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE,
        PHANTOMBUSTER_INPUT_TYPE,
        PHANTOMBUSTER_ONLY_SECOND_CIRCLE,
        PHANTOMBUSTER_SESSION_COOKIE,
        PHANTOMBUSTER_USER_AGENT,
    )

    out = dict(argument)
    mode = (PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE or "").strip().lower()
    if mode == "singular":
        # Strict phantom: never add inputType, dwellTime, emailChooser, profileUrls, spreadsheetUrl, etc.
        base = {k: out[k] for k in _SINGULAR_ARG_KEYS if k in out}
        if omit_session_fields:
            return base
        hint = (session_hint or "").strip()
        if looks_like_linkedin_session_cookie(hint):
            sess = hint
        elif not hint:
            sess = (PHANTOMBUSTER_SESSION_COOKIE or "").strip()
        else:
            sess = ""
        if not str(base.get("sessionCookie") or "").strip() and sess:
            base["sessionCookie"] = sess
        if PHANTOMBUSTER_USER_AGENT:
            base.setdefault("userAgent", PHANTOMBUSTER_USER_AGENT)
        return base

    if omit_session_fields:
        out.pop("sessionCookie", None)
        out.pop("userAgent", None)
    else:
        hint = (session_hint or "").strip()
        if looks_like_linkedin_session_cookie(hint):
            sess = hint
        elif not hint:
            sess = (PHANTOMBUSTER_SESSION_COOKIE or "").strip()
        else:
            sess = ""
        if not str(out.get("sessionCookie") or "").strip() and sess:
            out["sessionCookie"] = sess
        if PHANTOMBUSTER_USER_AGENT:
            out.setdefault("userAgent", PHANTOMBUSTER_USER_AGENT)
    it = (PHANTOMBUSTER_INPUT_TYPE or "profileUrl").strip()
    if it:
        out.setdefault("inputType", it)
    out.setdefault("onlySecondCircle", PHANTOMBUSTER_ONLY_SECOND_CIRCLE)
    out.setdefault("dwellTime", PHANTOMBUSTER_DWELL_TIME)
    ec = (PHANTOMBUSTER_EMAIL_CHOOSER or "none").strip()
    out.setdefault("emailChooser", ec)
    return out


def redact_phantom_argument_for_log(argument: dict[str, Any]) -> dict[str, Any]:
    """Copy argument for logging; truncate sessionCookie."""
    d = dict(argument)
    sc = d.get("sessionCookie")
    if isinstance(sc, str) and sc:
        d["sessionCookie"] = f"{sc[:16]}…({len(sc)} chars)"
    return d


def build_engagement_argument(linkedin_url: str, message: str) -> dict[str, Any]:
    """Build connect/DM argument dict for the configured Phantombuster phantom schema."""
    from config import PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE

    url = normalize_engagement_linkedin_url(linkedin_url)
    if not url:
        raise ValueError("invalid_or_empty_linkedin_url")
    msg = (message or "").strip()
    if not msg:
        raise ValueError("empty_message")
    if PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE == "array":
        return {
            "spreadsheetUrl": "",
            "profileUrls": [url],
            "numberOfLinesPerLaunch": 1,
            "message": msg,
        }
    # LinkedIn Auto Connect (singular): only keys the phantom accepts in `argument`
    return {
        "profileUrl": url,
        "numberOfAddsPerLaunch": 1,
        "message": msg,
    }


def _validate_array(argument: dict[str, Any]) -> tuple[bool, str]:
    urls = argument.get("profileUrls")
    if not isinstance(urls, list) or len(urls) < 1:
        return False, "profileUrls_must_be_non_empty_list"
    for u in urls:
        if not isinstance(u, str) or not u.strip():
            return False, "profileUrl_empty"
        s = u.strip()
        if not _LI_PATH.match(s):
            return False, f"invalid_linkedin_url_shape:{s[:80]}"
    n = argument.get("numberOfLinesPerLaunch")
    try:
        ni = int(n)
    except (TypeError, ValueError):
        return False, "numberOfLinesPerLaunch_not_int"
    if ni < 1 or ni > len(urls):
        return False, f"numberOfLinesPerLaunch_out_of_range:{ni}"
    msg = argument.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return False, "message_empty"
    ss = argument.get("spreadsheetUrl")
    if ss is not None and not isinstance(ss, str):
        return False, "spreadsheetUrl_bad_type"
    return True, ""


def _validate_singular(argument: dict[str, Any], *, session_via_bonus: bool = False) -> tuple[bool, str]:
    allowed = set(_SINGULAR_ARG_KEYS)
    if not session_via_bonus:
        allowed |= {"sessionCookie", "userAgent"}
    extra = set(argument.keys()) - allowed
    if extra:
        return False, f"argument_extra_keys:{','.join(sorted(extra))}"

    raw = argument.get("profileUrl")
    if not isinstance(raw, str) or not raw.strip():
        return False, "profileUrl_missing_or_empty"
    rs = str(raw).strip()
    if not rs.startswith("https://www.linkedin.com/in/"):
        return False, "profileUrl_must_start_with_https_www_linkedin_com_in"
    url = normalize_engagement_linkedin_url(rs)
    if not url:
        return False, f"invalid_linkedin_url_shape:{str(raw)[:80]}"
    if url != rs:
        return False, "profileUrl_not_canonical_remove_query_trailing_slash_use_www"
    if not _IN_PATH.match(url):
        return False, "profileUrl_must_match_https://www.linkedin.com/in/handle"
    n = argument.get("numberOfAddsPerLaunch")
    try:
        ni = int(n)
    except (TypeError, ValueError):
        return False, "numberOfAddsPerLaunch_not_int"
    if ni < 1:
        return False, f"numberOfAddsPerLaunch_out_of_range:{ni}"
    msg = argument.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return False, "message_empty"
    if len(msg) >= _MAX_CONNECT_MESSAGE_LEN:
        return False, f"message_too_long_max_{_MAX_CONNECT_MESSAGE_LEN - 1}_chars"
    if not session_via_bonus:
        sc = argument.get("sessionCookie")
        if not isinstance(sc, str) or len(sc.strip()) < 50:
            return False, "missing_or_short_sessionCookie_set_PHANTOMBUSTER_SESSION_COOKIE"
        ua = argument.get("userAgent")
        if ua is not None and not isinstance(ua, str):
            return False, "userAgent_bad_type"
    return True, ""


def validate_engagement_argument(
    argument: dict[str, Any],
    *,
    bonus_argument: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Validate argument for the configured mode (singular profileUrl vs array profileUrls)."""
    from config import PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE

    if not isinstance(argument, dict):
        return False, "argument_not_object"
    if bonus_argument is not None:
        ok_b, msg_b = validate_phantom_bonus_argument(bonus_argument)
        if not ok_b:
            return False, msg_b
        if PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE != "singular":
            return False, "bonus_argument_only_supported_for_singular_profile_mode"
        return _validate_singular(argument, session_via_bonus=True)
    if PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE == "singular":
        return _validate_singular(argument)
    return _validate_array(argument)


def log_engagement_argument_json(argument: dict[str, Any], *, agent_id: str) -> None:
    """Log the argument JSON (session cookie redacted)."""
    try:
        payload = json.dumps(redact_phantom_argument_for_log(argument), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload = str(redact_phantom_argument_for_log(argument))
    logger.info("[Phantombuster] engagement argument JSON (agent_id=%s): %s", agent_id, payload)


def phantom_failure_suggests_linkedin_session_issue(
    summary: str,
    result: Optional[dict[str, Any]] = None,
) -> bool:
    """
    Heuristic: Phantombuster / LinkedIn Auto Connect failed because the session cookie or login is bad.
    Used to auto-pause an account so operators can refresh li_at / Phantombuster connection without repeated errors.
    """
    parts: list[str] = [summary or ""]
    if isinstance(result, dict):
        for key in ("message", "error", "returnMessage", "output", "returnCode"):
            v = result.get(key)
            if v is not None and str(v).strip():
                parts.append(str(v))
        try:
            parts.append(json.dumps(result, default=str)[:2500])
        except (TypeError, ValueError):
            pass
    blob = " ".join(parts).lower()
    needles = (
        "invalid session",
        "invalid session cookie",
        "session cookie",
        "session expired",
        "cookies expired",
        "cookie expired",
        "not logged",
        "logged in to linkedin",
        "please login",
        "login to linkedin",
        "checkpoint",
        "security challenge",
        "unauthorized",
        " 401",
        "401 ",
        "access token",
        "authentication",
        "auth error",
        "could not load session",
        "disconnected",
        "linkedin session",
        "your session",
        "reconnect",
        "re-connect",
    )
    return any(n in blob for n in needles)


def summarize_phantom_result(result: dict[str, Any], *, max_len: int = 900) -> str:
    """Compact string for DB / logs (status, message, error hints)."""
    if not isinstance(result, dict):
        return ""
    parts = [
        f"status={result.get('status')!s}",
    ]
    for key in ("message", "error", "output", "returnMessage", "returnCode"):
        v = result.get(key)
        if v is not None and str(v).strip():
            parts.append(f"{key}={str(v)[:200]}")
    try:
        raw = json.dumps(result, default=str)[:max_len]
    except (TypeError, ValueError):
        raw = str(result)[:max_len]
    s = " | ".join(parts) + " | " + raw
    return s[:max_len]
