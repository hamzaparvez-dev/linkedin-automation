"""Unit tests for PhantombusterClient (mocked HTTP)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from integrations import phantombuster_client as pb
from integrations.phantombuster_client import PhantombusterClient, _NOSTATUS_DEDUPE_SEC, _RESULTOBJECT_INFER_REPEATS


def _ok_json(data: object) -> MagicMock:
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.text = json.dumps(data)
    r.url = "https://api.phantombuster.com/api/v2/containers/fetch-result-object"
    r.json = MagicMock(return_value=data)  # resp.json() == data (dict, not a MagicMock)
    return r


class TestWaitForCompletion(unittest.TestCase):
    def setUp(self) -> None:
        self.client = PhantombusterClient()
        self._p_sleep = patch.object(pb, "time")
        self.mock_time = self._p_sleep.start()
        self.mock_time.sleep = MagicMock()
        # Avoid real datetime drift in the loop: advance deadline each "tick" is hard; we use long timeout + fixed responses.

    def tearDown(self) -> None:
        self._p_sleep.stop()

    def _patch_get(
        self, side_effect: list[MagicMock] | MagicMock, container_id: str = "c1"
    ) -> None:
        # A single value must be wrapped: Mock(side_effect=iterable) treats one MagicMock as an iterable, not a one-shot return.
        seq = list(side_effect) if isinstance(side_effect, (list, tuple)) else [side_effect]
        self.client.session = MagicMock()
        self.client.session.get = MagicMock(side_effect=seq)

    def test_finished_from_api_unchanged_single_poll(self) -> None:
        raw = {"status": "finished", "resultObject": "[]", "message": "ok"}
        self._patch_get(_ok_json(raw))
        out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(out.get("status"), "finished")
        self.assertIsNone(out.get(pb._SYNTHETIC_INFERRED))
        self.assertEqual(self.client.session.get.call_count, 1)

    def test_inferred_finished_after_three_identical_nostatus_nonempty(self) -> None:
        body = {"resultObject": json.dumps([{"li": 1}])}
        r = _ok_json(body)
        self._patch_get([r, r, r])
        out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(out.get("status"), "finished")
        self.assertEqual(out.get(pb._SYNTHETIC_INFERRED), pb._SYNTHETIC_INFER_KEY)
        self.assertEqual(self.client.session.get.call_count, _RESULTOBJECT_INFER_REPEATS)

    def test_two_stable_polls_reaches_timeout_does_not_infer(self) -> None:
        """With only two identical no-status polls, never infer; exit via timeout after 2nd poll."""
        body = {"resultObject": json.dumps([{"a": 1}])}
        r = _ok_json(body)
        t0 = datetime(2020, 1, 1, 12, 0, 0)
        # init: t0, t0; iter1: while+sleep-branch; iter2: while+sleep-branch; next while: past 30m deadline
        t1 = t0 + timedelta(minutes=1)
        t_end = t0 + timedelta(minutes=31)
        _utc = [t0, t0, t0, t0, t1, t1, t_end, t_end, t_end]
        with (
            patch.object(pb, "_now_utc", side_effect=_utc),
            patch.object(pb, "synthetic_polling_timeout_result", wraps=pb.synthetic_polling_timeout_result) as w_to,
        ):
            self._patch_get([r, r])
            out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(self.client.session.get.call_count, 2)
        w_to.assert_called_once()
        self.assertNotEqual(out.get("status"), "finished")

    def test_empty_then_nonempty_needs_three_stable_nonempty(self) -> None:
        empty1 = _ok_json({"resultObject": "[]"})
        empty2 = _ok_json({"resultObject": "[]"})
        data = json.dumps([{"b": 2}])
        nonempty = _ok_json({"resultObject": data})
        self._patch_get([empty1, empty2, nonempty, nonempty, nonempty])
        out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(out.get("status"), "finished")
        self.assertEqual(self.client.session.get.call_count, 5)
        self.assertEqual(out.get(pb._SYNTHETIC_INFERRED), pb._SYNTHETIC_INFER_KEY)

    def test_fingerprint_change_resets_counter(self) -> None:
        """A stable pair then a different object means repeats reset; 3 more identical polls then infer."""
        a = _ok_json({"resultObject": json.dumps([1])})
        c1 = _ok_json({"resultObject": json.dumps([9])})
        b = _ok_json({"resultObject": json.dumps([2])})
        self._patch_get([a, a, c1, b, b, b])
        out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(out.get("status"), "finished")
        self.assertEqual(
            out.get("resultObject"),
            json.dumps([2]),
        )
        self.assertEqual(self.client.session.get.call_count, 6)

    def test_60s_empty_nostatus_dedupe_infers(self) -> None:
        """No status + empty resultObject: infer after _NOSTATUS_DEDUPE_SEC of ghost state in one tick."""
        body: dict = {"resultObject": "[]"}  # materially empty, no status key
        t0 = datetime(2020, 1, 1, 12, 0, 0)
        t_plus = t0 + timedelta(seconds=_NOSTATUS_DEDUPE_SEC)
        _utc = [t0, t0, t0, t0, t_plus]
        with patch.object(pb, "_now_utc", side_effect=_utc):
            self._patch_get(_ok_json(body))
            out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(self.client.session.get.call_count, 1)
        self.assertEqual(out.get("status"), "finished")
        self.assertEqual(out.get(pb._SYNTHETIC_INFERRED), pb._SYNTHETIC_INFER_KEY_60S_EMPTY)

    def test_running_clears_then_empty_needs_60s_from_fresh_ghost(self) -> None:
        """After running, ghost timer restarts: one empty response with time <60s does not dedupe-infer."""
        t0 = datetime(2020, 1, 1, 12, 0, 0)
        t_short = t0 + timedelta(seconds=10)  # <60s ghost stretch
        t_end = t0 + timedelta(minutes=31)  # past 30m deadline, exit while before another GET
        # 2× GET: running (clears), empty (ghost, not 60s); 3rd while() uses t_end, no 3rd GET
        # _now_utc sequence: 2 init + while1 + sleep(running) + while2 + ghost + 60s + sleep(empty) + while3
        _parts = [t0, t0, t0, t0, t0, t_short, t_short, t_short, t_end]
        r_run = _ok_json({"status": "running"})
        r_emp = _ok_json({"resultObject": "[]"})
        with (
            patch.object(pb, "_now_utc", side_effect=_parts),
            patch.object(pb, "synthetic_polling_timeout_result", wraps=pb.synthetic_polling_timeout_result) as w_to,
        ):
            self._patch_get([r_run, r_emp])
            out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(self.client.session.get.call_count, 2)
        w_to.assert_called_once()
        self.assertNotEqual(out.get("status"), "finished")

    def test_nonterminal_string_status_resets_ghost_timer(self) -> None:
        """pending (or similar) is not a ghost: next empty poll restarts 60s clock."""
        t0 = datetime(2019, 6, 1, 9, 0, 0)
        t1 = t0 + timedelta(minutes=31)  # deadline exit
        r_e = _ok_json({"resultObject": "[]"})
        r_p = _ok_json({"status": "pending", "resultObject": "[]"})
        _parts = [t0, t0, t0, t0, t0, t0, t0, t0, t0, t0, t0, t1, t1, t1, t1, t1]
        with (
            patch.object(pb, "_now_utc", side_effect=_parts),
            patch.object(pb, "synthetic_polling_timeout_result", wraps=pb.synthetic_polling_timeout_result) as w_to,
        ):
            self._patch_get([r_e, r_p, r_e])
            out = self.client.wait_for_completion("c1", timeout_minutes=30)
        self.assertEqual(self.client.session.get.call_count, 3)
        w_to.assert_called_once()
        self.assertNotEqual(out.get("status"), "finished")


if __name__ == "__main__":
    unittest.main()
