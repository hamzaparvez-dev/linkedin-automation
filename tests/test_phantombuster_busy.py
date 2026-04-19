"""PhantombusterClient.is_agent_runtime_busy fail-safe behavior."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from integrations.phantombuster_client import PhantombusterClient


class TestPhantombusterBusy(unittest.TestCase):
    def test_empty_agent_id_is_busy(self) -> None:
        c = PhantombusterClient()
        self.assertTrue(c.is_agent_runtime_busy(""))

    def test_network_error_treats_as_busy(self) -> None:
        c = PhantombusterClient()
        c.session.get = MagicMock(side_effect=RuntimeError("no network"))
        self.assertTrue(c.is_agent_runtime_busy("42"))

    def test_running_containers_match_marks_busy(self) -> None:
        c = PhantombusterClient()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [{"agentId": "7", "status": "running"}]

        def _get(url: str, **kwargs: object) -> MagicMock:
            if "fetch-running-containers" in url:
                return resp
            raise AssertionError(f"unexpected url {url}")

        c.session.get = MagicMock(side_effect=_get)
        self.assertTrue(c.is_agent_runtime_busy("7"))

    def test_no_match_then_not_busy(self) -> None:
        c = PhantombusterClient()
        r1 = MagicMock()
        r1.raise_for_status = MagicMock()
        r1.json.return_value = [{"agentId": "99", "status": "running"}]
        r2 = MagicMock()
        r2.raise_for_status = MagicMock()
        r2.json.return_value = {"name": "phantom", "running": False}

        urls: list[str] = []

        def _get(url: str, **kwargs: object) -> MagicMock:
            urls.append(url)
            if "fetch-running-containers" in url:
                return r1
            if "agents/fetch" in url:
                return r2
            raise AssertionError(url)

        c.session.get = MagicMock(side_effect=_get)
        self.assertFalse(c.is_agent_runtime_busy("1"))
        self.assertEqual(len(urls), 2)


if __name__ == "__main__":
    unittest.main()
