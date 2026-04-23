"""Tests for Phantombuster engagement payload validation and build."""

from __future__ import annotations

import unittest

import config as config_module
from core.phantom_payload import (
    append_fetch_output_to_summary,
    build_engagement_argument,
    looks_plausible_browser_user_agent,
    merge_phantom_launch_defaults,
    normalize_engagement_linkedin_url,
    normalize_session_cookie_for_bonus,
    phantom_failure_suggests_linkedin_session_issue,
    phantom_outcome_suggests_input_already_processed,
    validate_engagement_argument,
    validate_phantom_bonus_argument,
)

_FAKE_SESSION = "AQED" + "x" * 100


class TestPhantomPayload(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_mode = config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE
        self._prev_cookie = config_module.PHANTOMBUSTER_SESSION_COOKIE
        self._prev_ua = getattr(config_module, "PHANTOMBUSTER_USER_AGENT", "") or ""
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "singular"
        config_module.PHANTOMBUSTER_SESSION_COOKIE = _FAKE_SESSION
        config_module.PHANTOMBUSTER_USER_AGENT = "Mozilla/5.0 (test-ua)"

    def tearDown(self) -> None:
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = self._prev_mode
        config_module.PHANTOMBUSTER_SESSION_COOKIE = self._prev_cookie
        config_module.PHANTOMBUSTER_USER_AGENT = self._prev_ua

    def test_normalize_canonical_in_url(self) -> None:
        self.assertEqual(
            normalize_engagement_linkedin_url("  https://linkedin.com/in/Some-One  "),
            "https://www.linkedin.com/in/Some-One",
        )
        self.assertEqual(
            normalize_engagement_linkedin_url("https://www.linkedin.com/in/example-person"),
            "https://www.linkedin.com/in/example-person",
        )
        self.assertEqual(
            normalize_engagement_linkedin_url("https://www.linkedin.com/in/handle-name?trk=abc"),
            "https://www.linkedin.com/in/handle-name",
        )
        self.assertIsNone(normalize_engagement_linkedin_url(""))
        self.assertIsNone(normalize_engagement_linkedin_url("https://www.linkedin.com/company/acme"))

    def test_valid_singular_shape(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/example-person",
            "numberOfAddsPerLaunch": 1,
            "message": "Hello there",
            "sessionCookie": _FAKE_SESSION,
            "userAgent": "Mozilla/5.0 (test)",
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertTrue(ok, msg)

    def test_build_singular_matches_schema(self) -> None:
        base = build_engagement_argument(
            "https://linkedin.com/in/example-person",
            "  Hi  ",
        )
        self.assertEqual(set(base.keys()), {"profileUrl", "numberOfAddsPerLaunch", "message"})
        arg = merge_phantom_launch_defaults(base, session_hint="")
        self.assertIn("profileUrl", arg)
        self.assertEqual(arg["profileUrl"], "https://www.linkedin.com/in/example-person")
        self.assertEqual(arg["numberOfAddsPerLaunch"], 1)
        self.assertEqual(arg["message"], "Hi")
        self.assertNotIn("profileUrls", arg)
        self.assertNotIn("inputType", arg)
        self.assertNotIn("dwellTime", arg)
        self.assertIn("sessionCookie", arg)
        ok, msg = validate_engagement_argument(arg)
        self.assertTrue(ok, msg)

    def test_rejects_bad_url_singular(self) -> None:
        arg = {
            "profileUrl": "https://example.com/not-linkedin",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
        }
        ok, _ = validate_engagement_argument(arg)
        self.assertFalse(ok)

    def test_rejects_empty_message_singular(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "   ",
        }
        ok, _ = validate_engagement_argument(arg)
        self.assertFalse(ok)

    def test_normalize_session_cookie_for_bonus_prefixes_raw_value(self) -> None:
        raw = "AQED" + "y" * 80
        out = normalize_session_cookie_for_bonus(raw)
        self.assertTrue(out.startswith("li_at="))
        self.assertIn("li_at=", out)

    def test_singular_valid_with_bonus_omits_argument_session(self) -> None:
        base = build_engagement_argument("https://linkedin.com/in/example-person", "Hi")
        arg = merge_phantom_launch_defaults(base, session_hint="", omit_session_fields=True)
        self.assertEqual(set(arg.keys()), {"profileUrl", "numberOfAddsPerLaunch", "message"})
        self.assertNotIn("sessionCookie", arg)
        bonus = {
            "sessionCookie": "li_at=" + _FAKE_SESSION,
            "userAgent": "Mozilla/5.0 (test-ua)",
        }
        ok, msg = validate_engagement_argument(arg, bonus_argument=bonus)
        self.assertTrue(ok, msg)

    def test_singular_rejects_message_over_default_max_chars(self) -> None:
        from config import MAX_PHANTOM_DM_MESSAGE_CHARS

        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "x" * (MAX_PHANTOM_DM_MESSAGE_CHARS + 1),
            "sessionCookie": _FAKE_SESSION,
            "userAgent": "Mozilla/5.0",
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertFalse(ok)
        self.assertIn("message_too_long", msg)

    def test_connect_note_respects_200_char_cap(self) -> None:
        from config import MAX_CONNECT_NOTE_CHARS

        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "x" * (MAX_CONNECT_NOTE_CHARS + 1),
            "sessionCookie": _FAKE_SESSION,
            "userAgent": "Mozilla/5.0",
        }
        ok, msg = validate_engagement_argument(arg, max_message_chars=MAX_CONNECT_NOTE_CHARS)
        self.assertFalse(ok)
        self.assertIn("message_too_long", msg)

    def test_singular_rejects_extra_argument_keys_with_bonus(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
            "dwellTime": True,
        }
        bonus = {"sessionCookie": "li_at=" + _FAKE_SESSION, "userAgent": "Mozilla/5.0"}
        ok, msg = validate_engagement_argument(arg, bonus_argument=bonus)
        self.assertFalse(ok)
        self.assertIn("argument_extra_keys", msg)

    def test_phantom_failure_suggests_session_issue(self) -> None:
        self.assertTrue(
            phantom_failure_suggests_linkedin_session_issue(
                "status=error",
                {"message": "Invalid session cookie"},
            )
        )
        self.assertFalse(phantom_failure_suggests_linkedin_session_issue("status=finished", {}))
        self.assertTrue(
            phantom_failure_suggests_linkedin_session_issue(
                "exception:HTTPError:401 Client Error",
                None,
            )
        )

    def test_looks_plausible_browser_user_agent(self) -> None:
        self.assertTrue(
            looks_plausible_browser_user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
        )
        self.assertFalse(looks_plausible_browser_user_agent("curl/8.0"))
        self.assertFalse(looks_plausible_browser_user_agent("Mozilla/5.0"))
        self.assertFalse(looks_plausible_browser_user_agent(""))

    def test_validate_phantom_bonus_argument(self) -> None:
        ok, _ = validate_phantom_bonus_argument(
            {"sessionCookie": "li_at=" + "z" * 80, "userAgent": "Mozilla/5.0"}
        )
        self.assertTrue(ok)
        ok2, msg2 = validate_phantom_bonus_argument({"sessionCookie": "AQED" + "z" * 80, "userAgent": "x"})
        self.assertFalse(ok2)
        self.assertIn("li_at", msg2)
        ok3, msg3 = validate_phantom_bonus_argument({"sessionCookie": "li_at=" + "z" * 80, "userAgent": "  "})
        self.assertFalse(ok3)
        self.assertIn("userAgent", msg3)

    def test_merge_does_not_apply_env_cookie_for_non_cookie_session_hint(self) -> None:
        prev = config_module.PHANTOMBUSTER_SESSION_COOKIE
        try:
            config_module.PHANTOMBUSTER_SESSION_COOKIE = "AQED" + "z" * 100
            arg = merge_phantom_launch_defaults(
                {
                    "profileUrl": "https://www.linkedin.com/in/example-person",
                    "numberOfAddsPerLaunch": 1,
                    "message": "Hi",
                },
                session_hint="primary-sdr-session",
            )
            self.assertNotIn("sessionCookie", arg)
        finally:
            config_module.PHANTOMBUSTER_SESSION_COOKIE = prev

    def test_array_mode_validation(self) -> None:
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "array"
        try:
            arg = {
                "spreadsheetUrl": "",
                "profileUrls": ["https://www.linkedin.com/in/example-person"],
                "numberOfLinesPerLaunch": 1,
                "message": "Hello there",
            }
            ok, msg = validate_engagement_argument(arg)
            self.assertTrue(ok, msg)
        finally:
            config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "singular"

    def test_phantom_dedupe_message_in_result(self) -> None:
        r = {"status": "finished", "message": "Input already processed, skipping line 1."}
        self.assertTrue(phantom_outcome_suggests_input_already_processed(r, fetch_output_rows=None))
        r2 = {"status": "finished", "message": "Connected successfully."}
        self.assertFalse(phantom_outcome_suggests_input_already_processed(r2, fetch_output_rows=None))

    def test_phantom_dedupe_in_fetch_output(self) -> None:
        r = {"status": "finished"}
        rows = [{"url": "https://www.linkedin.com/in/x", "message": "This line was already processed."}]
        self.assertTrue(phantom_outcome_suggests_input_already_processed(r, fetch_output_rows=rows))

    def test_append_fetch_output_to_summary(self) -> None:
        s = append_fetch_output_to_summary("status=ok", [{"a": 1}])
        self.assertIn("fetch_output=", s)
        self.assertIn("1", s)


if __name__ == "__main__":
    unittest.main()
