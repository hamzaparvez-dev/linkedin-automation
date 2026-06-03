"""Tests for Phantombuster engagement payload validation and build."""

from __future__ import annotations

import json
import unittest

import config as config_module
from core.phantom_payload import (
    append_fetch_output_to_summary,
    build_dm_message_sender_argument,
    build_engagement_argument,
    extract_ui_session_from_agent_fetch,
    finalize_dm_message_sender_argument,
    looks_plausible_browser_user_agent,
    merge_phantom_launch_defaults,
    merge_ui_session_into_launch_argument,
    normalize_engagement_linkedin_url,
    phantom_connect_deduplication_skipped,
    phantom_failure_suggests_linkedin_session_issue,
    phantom_outcome_suggests_input_already_processed,
    validate_dm_message_sender_argument,
    validate_engagement_argument,
    validate_phantom_bonus_argument,
)
from integrations.phantombuster_client import is_synthetic_polling_timeout_result, synthetic_polling_timeout_result

_FAKE_SESSION = "AQED" + "x" * 100


class TestPhantomPayload(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_mode = config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE

    def tearDown(self) -> None:
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = self._prev_mode

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

    def test_valid_singular_shape_without_session(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/example-person",
            "spreadsheetUrl": "https://www.linkedin.com/in/example-person",
            "numberOfAddsPerLaunch": 1,
            "message": "Hello there",
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertTrue(ok, msg)

    def test_build_singular_matches_schema(self) -> None:
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "singular"
        base = build_engagement_argument(
            "https://linkedin.com/in/example-person",
            "  Hi  ",
        )
        self.assertEqual(
            set(base.keys()),
            {"profileUrl", "spreadsheetUrl", "numberOfAddsPerLaunch", "message"},
        )
        arg = merge_phantom_launch_defaults(base)
        self.assertEqual(arg["spreadsheetUrl"], "https://www.linkedin.com/in/example-person")
        self.assertEqual(arg["profileUrl"], "https://www.linkedin.com/in/example-person")
        self.assertNotIn("sessionCookie", arg)
        self.assertNotIn("userAgent", arg)
        ok, msg = validate_engagement_argument(arg)
        self.assertTrue(ok, msg)

    def test_rejects_session_fields_in_argument(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
            "sessionCookie": _FAKE_SESSION,
            "userAgent": "Mozilla/5.0",
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertFalse(ok)
        self.assertIn("sessionCookie", msg)

    def test_rejects_bonus_argument(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
        }
        bonus = {"sessionCookie": "li_at=" + _FAKE_SESSION, "userAgent": "Mozilla/5.0"}
        ok, msg = validate_engagement_argument(arg, bonus_argument=bonus)
        self.assertFalse(ok)
        self.assertIn("bonus_argument_not_supported", msg)

    def test_rejects_bad_url_singular(self) -> None:
        arg = {
            "profileUrl": "https://example.com/not-linkedin",
            "spreadsheetUrl": "https://example.com/not-linkedin",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
        }
        ok, _ = validate_engagement_argument(arg)
        self.assertFalse(ok)

    def test_rejects_empty_message_singular(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "   ",
        }
        ok, _ = validate_engagement_argument(arg)
        self.assertFalse(ok)

    def test_singular_rejects_message_over_default_max_chars(self) -> None:
        from config import MAX_PHANTOM_DM_MESSAGE_CHARS

        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "x" * (MAX_PHANTOM_DM_MESSAGE_CHARS + 1),
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertFalse(ok)
        self.assertIn("message_too_long", msg)

    def test_connect_note_respects_200_char_cap(self) -> None:
        from config import MAX_CONNECT_NOTE_CHARS

        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "x" * (MAX_CONNECT_NOTE_CHARS + 1),
        }
        ok, msg = validate_engagement_argument(arg, max_message_chars=MAX_CONNECT_NOTE_CHARS)
        self.assertFalse(ok)
        self.assertIn("message_too_long", msg)

    def test_singular_rejects_extra_argument_keys(self) -> None:
        arg = {
            "profileUrl": "https://www.linkedin.com/in/foo",
            "spreadsheetUrl": "https://www.linkedin.com/in/foo",
            "numberOfAddsPerLaunch": 1,
            "message": "Hi",
            "dwellTime": True,
        }
        ok, msg = validate_engagement_argument(arg)
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

    def test_looks_plausible_browser_user_agent(self) -> None:
        self.assertTrue(
            looks_plausible_browser_user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
        )
        self.assertFalse(looks_plausible_browser_user_agent("curl/8.0"))

    def test_validate_phantom_bonus_argument_rejects_any_bonus(self) -> None:
        ok, _ = validate_phantom_bonus_argument(None)
        self.assertTrue(ok)
        ok2, msg2 = validate_phantom_bonus_argument(
            {"sessionCookie": "li_at=" + "z" * 80, "userAgent": "Mozilla/5.0"}
        )
        self.assertFalse(ok2)
        self.assertIn("bonus_argument_not_supported", msg2)

    def test_array_mode_validation(self) -> None:
        config_module.PHANTOMBUSTER_ENGAGEMENT_PROFILE_MODE = "array"
        arg = {
            "spreadsheetUrl": "",
            "profileUrls": ["https://www.linkedin.com/in/example-person"],
            "numberOfLinesPerLaunch": 1,
            "message": "Hello there",
        }
        ok, msg = validate_engagement_argument(arg)
        self.assertTrue(ok, msg)

    def test_phantom_dedupe_message_in_result(self) -> None:
        r = {"status": "finished", "message": "Input already processed, skipping line 1."}
        self.assertTrue(phantom_outcome_suggests_input_already_processed(r, fetch_output_rows=None))

    def test_phantom_dedupe_runtime_events_slug(self) -> None:
        r = {
            "status": "finished",
            "message": "Success",
            "resultObject": {
                "runtimeEvents": [
                    {
                        "slug": "input-already-processed",
                        "text": "Input is already processed.",
                        "title": "Input already processed",
                        "type": "info",
                    }
                ]
            },
        }
        self.assertTrue(phantom_outcome_suggests_input_already_processed(r, fetch_output_rows=None))

    def test_phantom_connect_dedupe_synthetic_60s(self) -> None:
        r = {
            "status": "finished",
            "_synthetic_inferred": "nostatus_dedupe_60s_empty",
            "resultObject": None,
        }
        self.assertTrue(phantom_connect_deduplication_skipped(r, fetch_output_rows=[]))

    def test_append_fetch_output_to_summary(self) -> None:
        s = append_fetch_output_to_summary("status=ok", [{"a": 1}])
        self.assertIn("fetch_output=", s)

    def test_synthetic_polling_timeout_roundtrip(self) -> None:
        r = synthetic_polling_timeout_result("test-container-1")
        self.assertTrue(is_synthetic_polling_timeout_result(r))


class TestDmMessageSenderPayload(unittest.TestCase):
    def setUp(self) -> None:
        self._msgctl = config_module.PHANTOMBUSTER_MESSAGE_CONTROL
        self._en_scr = config_module.PHANTOMBUSTER_ENABLE_SCRAPING
        config_module.PHANTOMBUSTER_MESSAGE_CONTROL = "sendOnlyIfNoMessage"
        config_module.PHANTOMBUSTER_ENABLE_SCRAPING = False

    def tearDown(self) -> None:
        config_module.PHANTOMBUSTER_MESSAGE_CONTROL = self._msgctl
        config_module.PHANTOMBUSTER_ENABLE_SCRAPING = self._en_scr

    def test_build_uses_spreadsheet_url_only(self) -> None:
        a = build_dm_message_sender_argument("https://www.linkedin.com/in/whoever", "Hello DM")
        self.assertEqual(
            set(a.keys()),
            {"spreadsheetUrl", "message", "messageControl", "enableScraping", "emailChooser"},
        )
        m = finalize_dm_message_sender_argument(a)
        self.assertNotIn("sessionCookie", m)
        self.assertNotIn("userAgent", m)
        ok, msg = validate_dm_message_sender_argument(m)
        self.assertTrue(ok, msg)

    def test_dm_rejects_session_cookie_key_before_launch(self) -> None:
        a = {
            "spreadsheetUrl": "https://www.linkedin.com/in/ok",
            "message": "m",
            "messageControl": "sendOnlyIfNoMessage",
            "enableScraping": False,
            "emailChooser": "none",
            "sessionCookie": _FAKE_SESSION,
            "userAgent": "Mozilla/5.0 (a)",
        }
        ok, msg = validate_dm_message_sender_argument(a)
        self.assertFalse(ok)
        self.assertIn("sessionCookie", msg)

    def test_dm_accepts_session_when_for_launch(self) -> None:
        a = build_dm_message_sender_argument("https://www.linkedin.com/in/ok", "m")
        merged = merge_ui_session_into_launch_argument(
            a, {"sessionCookie": _FAKE_SESSION, "userAgent": "Mozilla/5.0 (ui)"}
        )
        ok, msg = validate_dm_message_sender_argument(merged, for_launch=True)
        self.assertTrue(ok, msg)

    def test_dm_rejects_forbidden_key(self) -> None:
        a = {
            "spreadsheetUrl": "https://www.linkedin.com/in/ok",
            "message": "m",
            "messageControl": "sendOnlyIfNoMessage",
            "enableScraping": False,
            "emailChooser": "none",
            "numberOfAddsPerLaunch": 1,
        }
        ok, msg = validate_dm_message_sender_argument(a)
        self.assertFalse(ok)
        self.assertIn("forbidden", msg)


if __name__ == "__main__":
    unittest.main()
