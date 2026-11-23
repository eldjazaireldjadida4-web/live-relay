"""Regression tests for v4.8.12: the fourth audit round on the 4.8.11 code.

- S1: the stall watchdog killed a slow-starting worker before its first
  frame. ``Progress.last_output_change`` was set at construction, so
  ``stalled()`` became true ``stall_timeout`` after construction even
  with zero output, and the stall check had no startup grace (unlike
  ``WORKER_STABLE_SECONDS = 120`` for the failure reset). A starting
  worker now gets the stall timeout plus a startup grace before it can
  be declared stalled; once output has flowed, the plain timeout from
  the last output change applies.
- S2: ``refresh_source()`` ran a blocking yt-dlp lookup (up to 90 s)
  synchronously in the supervision thread, freezing stall checks,
  heartbeats, status probes and stop handling. The lookup now runs on
  a background thread and the supervision loop keeps ticking until the
  fresh source is ready; a stop issued during a lookup returns
  promptly.
- S3: last-writer-wins across ``YouTubeQuotaTracker`` instances — the
  dashboard's short-lived verify tracker loaded the count, recorded 1
  unit and saved; the running relay tracker later overwrote it with
  its own stale count, losing units. ``record()`` now re-reads and
  merges the on-disk count before incrementing.
- S4: the YouTube authorize endpoints mutated
  ``os.environ["LIVE_PLATFORMS"]`` without the controller lock, so a
  concurrent Start could pick up the wrong platform set. They now use
  the ``platforms_override=`` mechanism the verify paths already use,
  and the environment is never touched.
- S5: ``apply_pending_update`` recorded success, cleared the staged
  marker and deleted the zip when the state-dir guard skipped every
  target (state dir covers the project tree), even though nothing was
  copied — and the stale-file sweep would then have deleted
  everything the updater installed before. It now refuses loudly and
  leaves the staged update in place for a retry.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from live_relay import updater
from live_relay.dashboard import PROJECT_ROOT, DashboardController
from live_relay.destinations import (
    YOUTUBE_QUOTA_STATE_FILENAME,
    YouTubeQuotaTracker,
)
from live_relay.quality import AdaptiveQuality
from live_relay.service import (
    Progress,
    _start_encoder,
    _teardown_worker,
)


def _settings(**overrides):
    values = {
        "stall_timeout": 1,
        "heartbeat": 10,
        "adaptive_quality": False,
        "pipe_mode": False,
        "ffmpeg_preset": "veryfast",
        "logo_enabled": False,
        "logo_file": "",
        "logo_position": "bottom-right",
        "logo_size_percent": 12,
        "logo_opacity": 0.9,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session(targets):
    return SimpleNamespace(
        targets=targets,
        youtube=None,
        facebook=None,
        active_platforms=[name for name, _ in targets],
        failures={},
    )


class _FakeProcess:
    """Stand-in for subprocess.Popen: poll() None means still running."""

    def __init__(self, code=None) -> None:
        self.returncode = code
        self.stdout = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class StallWatchdogTests(unittest.TestCase):
    def test_startup_grace_protects_a_slow_start_but_not_a_dead_stream(self):
        # The grace is patched small so the test runs in seconds; the
        # production value (WORKER_STARTUP_GRACE_SECONDS) is far
        # larger than any stall_timeout the loop can see.
        with patch(
            "live_relay.service.WORKER_STARTUP_GRACE_SECONDS", 3.0
        ):
            self._no_output_worker_survives_the_grace()
            self._output_then_silent_worker_is_killed()

    def _no_output_worker_survives_the_grace(self):
        stopped = threading.Event()
        settings = _settings(stall_timeout=1)
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        session = _session([("youtube", "rtmp://a.fake/y")])
        quality = AdaptiveQuality("720p")
        starts: list[int] = []
        teardowns: list[float] = []

        def fake_start(worker, *_args, **_kwargs):
            starts.append(worker.index)
            worker.process = _FakeProcess()
            worker.progress = Progress()
            worker.transport_failure = threading.Event()
            worker.reader_stop = threading.Event()
            worker.threads = []
            worker.started_at = time.monotonic()
            return True

        def fake_teardown(worker):
            # The finally-block teardown after the loop exits is
            # cleanup, not a watchdog kill: only count teardowns
            # while the supervision loop is still running.
            if worker.process is not None and not stopped.is_set():
                teardowns.append(time.monotonic())
            _teardown_worker(worker)

        threading.Timer(2.5, stopped.set).start()
        with (
            patch("live_relay.service._start_worker", side_effect=fake_start),
            patch(
                "live_relay.service._teardown_worker", side_effect=fake_teardown
            ),
            patch(
                "live_relay.service.resolve_source",
                return_value=source,
            ),
        ):
            reason, _ = _start_encoder(
                "ffmpeg", source, session, quality, settings, stopped
            )
        self.assertEqual(reason, "stopped")
        self.assertEqual(starts, [0])
        # 2.5 s in, the worker has produced no output, but the
        # stall timeout (1 s) plus the grace (3 s) has not passed:
        # the watchdog must not have killed it.
        self.assertEqual(teardowns, [])

    def _output_then_silent_worker_is_killed(self):
        stopped = threading.Event()
        settings = _settings(stall_timeout=1)
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        fresh = SimpleNamespace(urls=("fresh",), headers=(), width=0, height=0)
        session = _session([("youtube", "rtmp://a.fake/y")])
        quality = AdaptiveQuality("720p")
        starts: list[SimpleNamespace] = []
        teardowns: list[float] = []
        fed_at: list[float] = []

        def fake_start(worker, _ffmpeg, current, *_args, **_kwargs):
            starts.append(current)
            worker.process = _FakeProcess()
            worker.progress = Progress()
            worker.transport_failure = threading.Event()
            worker.reader_stop = threading.Event()
            worker.threads = []
            worker.started_at = time.monotonic()
            if len(starts) == 1:
                # One progress line, then silence: the stream is dead
                # even though the worker itself just started.
                def _feed_once() -> None:
                    fed_at.append(time.monotonic())
                    worker.progress.feed("out_time_ms=1000")

                threading.Timer(0.3, _feed_once).start()
            return True

        def fake_teardown(worker):
            if worker.process is not None and not stopped.is_set():
                teardowns.append(time.monotonic())
            _teardown_worker(worker)

        threading.Timer(7.0, stopped.set).start()
        with (
            patch("live_relay.service._start_worker", side_effect=fake_start),
            patch(
                "live_relay.service._teardown_worker", side_effect=fake_teardown
            ),
            patch(
                "live_relay.service.resolve_source",
                return_value=fresh,
            ),
        ):
            reason, _ = _start_encoder(
                "ffmpeg", source, session, quality, settings, stopped
            )
        self.assertEqual(reason, "stopped")
        self.assertEqual(len(fed_at), 1)
        # The worker produced output once and then went silent: the
        # plain stall timeout (1 s) from the last output change
        # applies, so it must be killed — and not before the timeout.
        self.assertEqual(len(teardowns), 1)
        self.assertGreaterEqual(teardowns[0] - fed_at[0], 1.0)
        # And the restart used the refreshed source.
        self.assertEqual(len(starts), 2)
        self.assertIs(starts[1], fresh)


class BackgroundResolveTests(unittest.TestCase):
    def test_supervision_loop_ticks_and_stop_returns_promptly_during_a_slow_resolve(
        self,
    ):
        self._loop_ticks_while_a_resolve_is_slow()
        self._stop_returns_promptly_during_a_resolve()

    def _run_encoder(self, resolve_delay, stop_after):
        stopped = threading.Event()
        settings = _settings(stall_timeout=15)
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        fresh = SimpleNamespace(urls=("fresh",), headers=(), width=0, height=0)
        session = _session([("youtube", "rtmp://a.fake/y")])
        quality = AdaptiveQuality("720p")
        resolve_window: list[float] = []
        status_ticks: list[float] = []
        started_with: list[SimpleNamespace] = []

        def slow_resolve(_settings):
            resolve_window.append(time.monotonic())
            time.sleep(resolve_delay)
            resolve_window.append(time.monotonic())
            return fresh

        def fake_start(worker, _ffmpeg, current, *_args, **_kwargs):
            started_with.append(current)
            # First start exits at once so the worker restarts and the
            # refresh path is taken; later starts keep running.
            code = 1 if len(started_with) == 1 else None
            worker.process = _FakeProcess(code)
            worker.progress = Progress()
            worker.transport_failure = threading.Event()
            worker.reader_stop = threading.Event()
            worker.threads = []
            worker.started_at = time.monotonic()
            return True

        class FakeStatus:
            def update(self, **_kwargs) -> None:
                status_ticks.append(time.monotonic())

        result: dict[str, object] = {}

        def _run() -> None:
            with (
                patch(
                    "live_relay.service._start_worker",
                    side_effect=fake_start,
                ),
                patch(
                    "live_relay.service.resolve_source",
                    side_effect=slow_resolve,
                ),
            ):
                result["outcome"] = _start_encoder(
                    "ffmpeg",
                    source,
                    session,
                    quality,
                    settings,
                    stopped,
                    status=FakeStatus(),
                )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        stop_at = time.monotonic() + stop_after
        threading.Timer(stop_after, stopped.set).start()
        thread.join(timeout=stop_after + 10)
        return {
            "alive": thread.is_alive(),
            "joined_in": time.monotonic() - stop_at,
            "resolve_window": resolve_window,
            "status_ticks": status_ticks,
            "started_with": started_with,
            "fresh": fresh,
            "outcome": result.get("outcome"),
        }

    def _loop_ticks_while_a_resolve_is_slow(self):
        outcome = self._run_encoder(resolve_delay=5.0, stop_after=10.5)
        self.assertFalse(outcome["alive"])
        self.assertEqual(len(outcome["resolve_window"]), 2)
        start, end = outcome["resolve_window"]
        self.assertGreaterEqual(end - start, 5.0)
        # The supervision loop kept ticking while the lookup was in
        # flight: status updates happened inside the resolve window,
        # which is impossible when the loop itself blocks on yt-dlp.
        ticks_during_resolve = [
            tick for tick in outcome["status_ticks"] if start <= tick <= end
        ]
        self.assertGreaterEqual(len(ticks_during_resolve), 2)
        # And the restart used the fresh source once it arrived.
        self.assertIn(outcome["fresh"], outcome["started_with"])
        self.assertEqual(outcome["outcome"][0], "stopped")

    def _stop_returns_promptly_during_a_resolve(self):
        outcome = self._run_encoder(resolve_delay=8.0, stop_after=5.0)
        self.assertFalse(outcome["alive"])
        # The stop was issued mid-resolve; the supervision thread must
        # not wait out the lookup (the old code blocked up to 90 s
        # here, far past a 30 s join).
        self.assertLess(outcome["joined_in"], 4.0)


class QuotaMergeTests(unittest.TestCase):
    def test_two_interleaved_trackers_do_not_lose_units(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            relay = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            relay.record(10)
            # The dashboard's short-lived verify tracker loads the
            # relay's count, records 1 unit and saves.
            verify = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            self.assertEqual(verify.used, 10)
            verify.record(1)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["used"], 11
            )
            # The running relay tracker still holds 10 in memory; its
            # next record must merge the 11 on disk, not overwrite it.
            relay.record(5)
            self.assertEqual(relay.used, 16)
            reloaded = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            self.assertEqual(reloaded.used, 16)


class AuthorizeEndpointTests(unittest.TestCase):
    def _controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def test_authorize_endpoints_use_platforms_override_and_leave_the_environment_alone(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(Path(directory))
            settings = SimpleNamespace(
                youtube_api_enabled=True,
                youtube_client_secrets="client.json",
            )
            destination = Mock()
            destination.manual_auth_url.return_value = "https://auth.example/"
            with (
                patch.dict(
                    os.environ, {"LIVE_PLATFORMS": "facebook,tiktok"}
                ),
                patch(
                    "live_relay.dashboard.load_dotenv",
                ),
                patch(
                    "live_relay.dashboard.load_settings",
                    return_value=settings,
                ) as mock_load,
                patch(
                    "live_relay.dashboard.YouTubeDestination",
                    return_value=destination,
                ) as mock_destination,
            ):
                controller.authorize_youtube()
                controller.youtube_manual_auth_url()
                controller.youtube_manual_auth_exchange("code-123")
                # All three endpoints load settings for YouTube only
                # through the override, never by mutating the
                # environment — a concurrent Start can therefore never
                # observe the wrong platform set.
                self.assertEqual(
                    mock_load.call_args_list,
                    [call(platforms_override=("youtube",))] * 3,
                )
                self.assertEqual(
                    os.environ["LIVE_PLATFORMS"], "facebook,tiktok"
                )
                self.assertEqual(mock_destination.call_args_list, [call(settings)] * 3)
            destination.authenticate.assert_called_once_with()
            destination.manual_auth_url.assert_called_once_with()
            destination.manual_auth_exchange.assert_called_once_with("code-123")


def _zip_bytes(files: dict[str, str], top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            arcname = f"{top}/{name}" if top else name
            archive.writestr(arcname, content)
    return buffer.getvalue()


class ApplyGuardTests(unittest.TestCase):
    def test_apply_refuses_when_the_state_dir_guard_skips_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            (root / "src" / "live_relay").mkdir(parents=True)
            (root / "src" / "live_relay" / "__init__.py").write_text(
                '__version__ = "4.8.2"\n', encoding="utf-8"
            )
            # The state directory IS the project tree: the guard that
            # never writes into the state directory skips every target.
            state_dir = root
            zip_path = state_dir / "updates" / "live-relay-4.8.3.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            zip_path.write_bytes(
                _zip_bytes(
                    {
                        "src/live_relay/__init__.py": (
                            '__version__ = "4.8.3"\n'
                        ),
                        "src/live_relay/updater.py": "# new updater\n",
                    },
                    top="live-relay-4.8.3",
                )
            )
            release = {"version": "4.8.3", "tag": "v4.8.3", "html_url": ""}
            updater.stage_update(state_dir, release, zip_path)
            # A file the updater installed before: the stale-file sweep
            # must not delete it when the apply is refused.
            updater.write_update_state(
                state_dir, applied_files=["src/live_relay/__init__.py"]
            )
            with self.assertRaisesRegex(RuntimeError, "applied no files"):
                updater.apply_pending_update(root, state_dir)
            # Nothing was recorded as applied, the staged update is
            # still there for a retry, and the project is untouched.
            state = updater.read_update_state(state_dir)
            self.assertIn("staged", state)
            self.assertNotEqual(state.get("last_applied_version"), "4.8.3")
            self.assertTrue(zip_path.is_file())
            self.assertEqual(
                (root / "src" / "live_relay" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
                '__version__ = "4.8.2"\n',
            )
            self.assertFalse((root / "src" / "live_relay" / "updater.py").exists())
            self.assertTrue(
                (root / "src" / "live_relay" / "__init__.py").is_file()
            )


if __name__ == "__main__":
    unittest.main()
