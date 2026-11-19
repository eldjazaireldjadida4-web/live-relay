"""Regression tests for the live-relay 4.8.3 fixes.

Covers the three open audit items from the 4.8.1 round:
- a YouTube broadcast in state "created" is startable, not dead;
- every dashboard /api GET endpoint requires the control token;
- stop_relay waits for the relay thread to tear down instead of
  returning while the encoder is still running.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests

from live_relay.dashboard import (
    PROJECT_ROOT,
    DashboardController,
    build_handler_class,
)
import live_relay.dashboard as dashboard_module
from live_relay.destinations import DestinationSession
from live_relay.service import _session_is_dead


def _session_with_broadcast_status(status: str | None) -> DestinationSession:
    youtube = mock.Mock()
    if status is None:
        youtube.check_status.side_effect = RuntimeError("API down")
    else:
        youtube.check_status.return_value = {"broadcast_status": status}
    return DestinationSession(youtube=youtube)


class SessionIsDeadTests(unittest.TestCase):
    def test_created_broadcast_is_startable_not_dead(self):
        # "created" means the event exists and has never gone live yet;
        # YouTube moves it to "live" when the encoder connects. Treating
        # it as dead used to discard the event (and its watch URL) and
        # mint a new one on every gave-up retry.
        self.assertFalse(_session_is_dead(_session_with_broadcast_status("created")))
        self.assertFalse(_session_is_dead(_session_with_broadcast_status("live")))

    def test_complete_and_revoked_broadcasts_are_dead(self):
        self.assertTrue(_session_is_dead(_session_with_broadcast_status("complete")))
        self.assertTrue(_session_is_dead(_session_with_broadcast_status("revoked")))

    def test_status_check_failure_is_not_treated_as_dead(self):
        self.assertFalse(_session_is_dead(_session_with_broadcast_status(None)))

    def test_session_without_youtube_is_not_dead(self):
        self.assertFalse(_session_is_dead(DestinationSession()))


def _make_controller(root: Path) -> DashboardController:
    (root / ".env.example").write_text(
        (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / ".env").write_text(
        f"LIVE_RELAY_STATE_DIR={root / 'state'}\n", encoding="utf-8"
    )
    with mock.patch.dict(os.environ, {}, clear=True):
        return DashboardController(root)


class StopRelayTests(unittest.TestCase):
    def _running_controller(self, root: Path, thread_fn) -> DashboardController:
        controller = _make_controller(root)
        controller._stop_event = threading.Event()
        controller._relay_thread = threading.Thread(
            target=thread_fn, args=(controller._stop_event,), daemon=True
        )
        controller._relay_thread.start()
        return controller

    def test_stop_waits_until_the_thread_is_dead(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-stop-", dir=PROJECT_ROOT
        ) as directory:
            def _worker(stop_event):
                stop_event.wait(timeout=5)

            controller = self._running_controller(Path(directory), _worker)
            result = controller.stop_relay()
            self.assertTrue(result["stopped"])
            self.assertFalse(result["stop_timed_out"])
            self.assertFalse(controller._relay_thread.is_alive())
            self.assertFalse(controller._is_running())
            self.assertEqual(controller.get_status()["mode"], "stopped")

    def test_stop_reports_a_timeout_instead_of_pretending(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-stop-", dir=PROJECT_ROOT
        ) as directory:
            def _stubborn(_stop_event):
                threading.Event().wait(timeout=10)

            controller = self._running_controller(Path(directory), _stubborn)
            try:
                result = controller.stop_relay(timeout=0.2)
            finally:
                controller._relay_thread.join(timeout=0.1)
            self.assertFalse(result["stopped"])
            self.assertTrue(result["stop_timed_out"])
            self.assertEqual(controller.get_status()["mode"], "stopping")

    def test_stop_when_nothing_runs_is_a_noop(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-stop-", dir=PROJECT_ROOT
        ) as directory:
            controller = _make_controller(Path(directory))
            result = controller.stop_relay()
            self.assertNotIn("stop_timed_out", result)

    def test_clean_relay_end_sets_the_stopped_mode(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-stop-", dir=PROJECT_ROOT
        ) as directory:
            controller = _make_controller(Path(directory))
            controller.status.update(mode="starting", message="Starting relay")
            with mock.patch("live_relay.dashboard.run", return_value=None):
                controller._run_relay(mock.Mock(), threading.Event())
            self.assertEqual(controller.get_status()["mode"], "stopped")


class ApiAuthHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-auth-http-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        self.controller = _make_controller(root)
        html_path = Path(dashboard_module.__file__).with_name("dashboard.html")
        handler = build_handler_class(self.controller, html_path)
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _headers(self) -> dict[str, str]:
        return {"X-Relay-Token": self.controller.control_token}

    def test_api_get_endpoints_require_the_control_token(self):
        for path in ("/api/status", "/api/config", "/api/logs", "/api/update"):
            denied = requests.get(f"{self.base_url()}{path}", timeout=5)
            self.assertEqual(denied.status_code, 403, path)
            allowed = requests.get(
                f"{self.base_url()}{path}", headers=self._headers(), timeout=5
            )
            self.assertEqual(allowed.status_code, 200, path)

    def test_dashboard_page_is_still_public_and_sends_the_token_on_get(self):
        response = requests.get(f"{self.base_url()}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.controller.control_token, response.text)
        self.assertIn('headers:{"X-Relay-Token":CONTROL_TOKEN}', response.text)

    def test_unknown_route_still_returns_404_without_a_token(self):
        response = requests.get(f"{self.base_url()}/nope", timeout=5)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
