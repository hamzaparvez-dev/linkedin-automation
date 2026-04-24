"""Unit tests for PhantombusterClient (mocked HTTP)."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from integrations import phantombuster_client as pb
from integrations.phantombuster_client import PhantombusterClient, _RESULTOBJECT_INFER_REPEATS


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


if __name__ == "__main__":
    unittest.main()
