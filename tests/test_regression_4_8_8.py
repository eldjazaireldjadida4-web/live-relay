"""Regression tests for v4.8.8: the six deferred audit items.

- M11: YouTube quota-aware polling — the status probe costs quota, so
  the interval stretches as the daily budget runs out and polling stops
  entirely once it is spent.
- M9: shared yt-dlp pipe — one yt-dlp process feeds every encoder
  worker instead of one per destination.
- M15: Gemini retries — metadata and thumbnail calls retry transient
  failures a few times with backoff; a failed thumbnail still does not
  consume the 10-minute throttle.
- M16: notification-failure visibility — a failed Telegram/webhook
  delivery is recorded and surfaced in the dashboard status instead of
  being swallowed silently.
- M20: manifest-based update sync — the updater verifies the extracted
  tree against a MANIFEST.json (file list + sha256 hashes).
- M2: the served dashboard page no longer embeds the real control
  token; each page load gets a page-scoped session token.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import requests

from live_relay import __version__, updater
from live_relay.config import load_settings
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class
import live_relay.dashboard as dashboard_module
from live_relay.destinations import (
    DestinationSession,
    YouTubeDestination,
    YouTubeQuotaTracker,
)
from live_relay.metadata import StreamMetadata, generate_metadata, post_gemini
from live_relay.notifier import Notifier, notifier_from_settings
from live_relay.quality import AdaptiveQuality
from live_relay.runtime import RelayStatus
from live_relay.service import WorkerState, _start_worker, _youtube_status_due
from live_relay.source import LiveSource, SharedSourcePipe
from live_relay.thumbnail_ai import generate_auto_thumbnail, generate_image_bytes
import live_relay.thumbnail_ai as thumbnail_ai

METADATA = StreamMetadata("عنوان البث", "الوصف", ("وسم",))


def _session_with_targets(*targets: tuple[str, str]) -> DestinationSession:
    session = DestinationSession(metadata=METADATA, base_metadata=METADATA)
    session.targets = list(targets)
    return session


def _settings_for_worker(**overrides):
    base = {
        "pipe_mode": True,
        "ffmpeg_preset": "veryfast",
        "logo_enabled": False,
        "logo_file": "",
        "logo_position": "bottom-right",
        "logo_size_percent": 12,
        "logo_opacity": 0.9,
        "frame_enabled": False,
        "frame_file": "",
        "record_enabled": False,
        "format_selector": "best",
        "source_url": "https://example.com/live",
        "cookie_file": "",
        "cookies_from_browser": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeProcess:
    """A stand-in for subprocess.Popen with text or binary streams."""

    def __init__(self, stdout=None, stderr=None, stdin=None) -> None:
        self.stdout = stdout if stdout is not None else io.StringIO("")
        self.stderr = stderr if stderr is not None else io.StringIO("")
        self.stdin = stdin if stdin is not None else io.BytesIO()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


# ---------------------------------------------------------------------------
# M11: YouTube quota-aware polling
# ---------------------------------------------------------------------------


class YouTubeQuotaTrackerTests(unittest.TestCase):
    def test_fresh_tracker_polls_at_the_base_interval(self):
        tracker = YouTubeQuotaTracker(budget=10000)
        self.assertTrue(tracker.can_poll())
        self.assertEqual(tracker.poll_interval(30), 30.0)
        self.assertEqual(tracker.remaining, 10000)
        self.assertEqual(
            tracker.snapshot(),
            {"used": 0, "budget": 10000, "remaining": 10000, "used_fraction": 0.0},
        )

    def test_interval_stretches_as_the_budget_runs_out(self):
        tracker = YouTubeQuotaTracker(budget=10000)
        tracker.record(7000)
        self.assertEqual(tracker.poll_interval(30), 60.0)
        tracker.record(1500)
        self.assertEqual(tracker.poll_interval(30), 120.0)
        tracker.record(1000)
        self.assertEqual(tracker.poll_interval(30), 240.0)

    def test_spent_budget_stops_polling(self):
        tracker = YouTubeQuotaTracker(budget=100)
        tracker.record(100)
        self.assertFalse(tracker.can_poll())
        self.assertEqual(tracker.remaining, 0)

    def test_usage_resets_on_a_new_day(self):
        tracker = YouTubeQuotaTracker(budget=100)
        tracker.record(90)
        tracker._day = "2000-01-01"
        self.assertEqual(tracker.used_fraction, 0.0)
        self.assertTrue(tracker.can_poll())

    def test_check_status_records_two_quota_units(self):
        class _ListCall:
            def __init__(self, payload):
                self._payload = payload

            def execute(self):
                return self._payload

        class _Resource:
            def __init__(self, payload):
                self._payload = payload

            def list(self, **kwargs):
                return _ListCall(self._payload)

        class _FakeService:
            def liveBroadcasts(self):
                return _Resource({"items": [{"status": {"lifeCycleStatus": "live"}}]})

            def liveStreams(self):
                return _Resource(
                    {
                        "items": [
                            {
                                "status": {
                                    "streamStatus": "active",
                                    "healthStatus": {"status": "good"},
                                }
                            }
                        ]
                    }
                )

        destination = YouTubeDestination(SimpleNamespace(youtube_quota_budget=10000))
        destination.service = _FakeService()
        destination.broadcast = {"id": "broadcast-id"}
        destination.stream = {"id": "stream-id"}
        status = destination.check_status()
        self.assertEqual(status["broadcast_status"], "live")
        self.assertEqual(status["stream_status"], "active")
        self.assertEqual(destination.quota.used, 2)

    def test_status_due_gate(self):
        tracker = YouTubeQuotaTracker(budget=100)
        youtube = SimpleNamespace(quota=tracker)
        self.assertTrue(_youtube_status_due(youtube, 1000.0, 0.0, 30))
        self.assertFalse(_youtube_status_due(youtube, 1029.0, 1000.0, 30))
        self.assertTrue(_youtube_status_due(youtube, 1030.0, 1000.0, 30))
        tracker.record(70)
        self.assertFalse(_youtube_status_due(youtube, 1059.0, 1000.0, 30))
        self.assertTrue(_youtube_status_due(youtube, 1060.0, 1000.0, 30))
        tracker.record(30)
        self.assertFalse(_youtube_status_due(youtube, 99999.0, 1000.0, 30))

    def test_status_due_without_a_tracker_uses_the_plain_heartbeat(self):
        youtube = SimpleNamespace()
        self.assertTrue(_youtube_status_due(youtube, 1000.0, 0.0, 30))
        self.assertFalse(_youtube_status_due(youtube, 1029.0, 1000.0, 30))

    def test_quota_budget_is_configurable(self):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
            "YOUTUBE_QUOTA_BUDGET": "5000",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.youtube_quota_budget, 5000)

    def test_quota_budget_defaults_to_the_youtube_daily_quota(self):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.youtube_quota_budget, 10000)

    def test_quota_budget_falls_back_on_a_non_numeric_value(self):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
            "YOUTUBE_QUOTA_BUDGET": "abc",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        # 4.8.9: a hand-edited bad value must not brick startup; the
        # dashboard already fell back to 10000 for display.
        self.assertEqual(settings.youtube_quota_budget, 10000)


# ---------------------------------------------------------------------------
# M9: shared yt-dlp pipe
# ---------------------------------------------------------------------------


class SharedSourcePipeTests(unittest.TestCase):
    def _fake_pipe_process(self, data: bytes) -> FakeProcess:
        process = FakeProcess(
            stdout=io.BytesIO(data), stderr=io.BytesIO(b""), stdin=None
        )
        process.returncode = None
        return process

    def test_one_process_feeds_every_subscriber(self):
        data = b"ts-chunk-" * 1000
        created = []
        settings = _settings_for_worker()

        def fake_popen(*args, **kwargs):
            process = self._fake_pipe_process(data)
            created.append(process)
            return process

        pipe = SharedSourcePipe(settings)
        first = io.BytesIO()
        second = io.BytesIO()
        # Subscribe before the pump starts: a subscriber that joins
        # after the pump drained the (finite) test stream would see
        # nothing, which is a property of mid-stream joins, not a bug.
        pipe.subscribe(first)
        pipe.subscribe(second)
        with patch("live_relay.source.subprocess.Popen", side_effect=fake_popen):
            self.assertTrue(pipe.start())
            self.assertTrue(pipe.alive())
            self.assertEqual(pipe.subscriber_count(), 2)
            pipe._pump.join(timeout=5)
        self.assertEqual(len(created), 1)
        self.assertEqual(first.getvalue(), data)
        self.assertEqual(second.getvalue(), data)

    def test_restart_spawns_a_fresh_process(self):
        settings = _settings_for_worker()
        created = []

        def fake_popen(*args, **kwargs):
            process = self._fake_pipe_process(b"x")
            created.append(process)
            return process

        pipe = SharedSourcePipe(settings)
        with patch("live_relay.source.subprocess.Popen", side_effect=fake_popen):
            self.assertTrue(pipe.start())
            self.assertTrue(pipe.restart())
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].terminated)

    def test_stop_terminates_the_process_and_closes_subscribers(self):
        settings = _settings_for_worker()
        process = self._fake_pipe_process(b"data")
        pipe = SharedSourcePipe(settings)
        subscriber = io.BytesIO()
        with patch("live_relay.source.subprocess.Popen", return_value=process):
            self.assertTrue(pipe.start())
            pipe.subscribe(subscriber)
            pipe.stop()
        self.assertTrue(process.terminated)
        self.assertTrue(subscriber.closed)
        self.assertFalse(pipe.alive())


class StartWorkerSharedPipeTests(unittest.TestCase):
    def _run_worker(self, shared_pipe, settings):
        source = LiveSource(
            title="t", urls=("https://source.example/stream",), headers=()
        )
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        quality = AdaptiveQuality("720p")
        worker = WorkerState(name="youtube", index=0)
        calls = []

        def fake_popen(args, **kwargs):
            calls.append(args)
            if "yt_dlp" in str(args):
                return FakeProcess(stdout=io.BytesIO(b""), stderr=io.BytesIO(b""))
            return FakeProcess()

        with patch("live_relay.service.subprocess.Popen", side_effect=fake_popen):
            started = _start_worker(
                worker, "ffmpeg", source, session, quality, settings, shared_pipe
            )
        return worker, calls, started

    def test_shared_pipe_spawns_no_per_worker_yt_dlp(self):
        settings = _settings_for_worker()
        pipe = SharedSourcePipe(settings)
        pipe_process = FakeProcess(
            stdout=io.BytesIO(b"ts-data"), stderr=io.BytesIO(b"")
        )
        with patch("live_relay.source.subprocess.Popen", return_value=pipe_process):
            self.assertTrue(pipe.start())
            worker, calls, started = self._run_worker(pipe, settings)
        try:
            self.assertTrue(started)
            # Exactly one Popen call, and it is ffmpeg — no yt-dlp.
            self.assertEqual(len(calls), 1)
            self.assertNotIn("yt_dlp", str(calls[0]))
            self.assertEqual(pipe.subscriber_count(), 1)
            self.assertIsNotNone(worker.pipe_subscription)
            self.assertIsNone(worker.pipe_process)
        finally:
            from live_relay.service import _teardown_worker

            _teardown_worker(worker)
            pipe.stop()
        self.assertEqual(pipe.subscriber_count(), 0)

    def test_without_a_shared_pipe_each_worker_keeps_its_own_yt_dlp(self):
        settings = _settings_for_worker()
        worker, calls, started = self._run_worker(None, settings)
        try:
            self.assertTrue(started)
            self.assertEqual(len(calls), 2)
            self.assertIn("yt_dlp", str(calls[0]))
            self.assertIsNotNone(worker.pipe_process)
            self.assertIsNone(worker.pipe_subscription)
        finally:
            from live_relay.service import _teardown_worker

            _teardown_worker(worker)


# ---------------------------------------------------------------------------
# M15: Gemini retries
# ---------------------------------------------------------------------------


def _gemini_metadata_response():
    generated = {
        "title_candidates": ["بث مباشر: نهائي دوري الأبطال بين فريقين"],
        "description": "وصف بث مفيد.",
        "keywords": ["دوري الأبطال", "بث مباشر", "كرة القدم"],
    }
    response = Mock(ok=True)
    response.json.return_value = {
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": json.dumps(generated)}],
            }
        ]
    }
    return response


class PostGeminiRetryTests(unittest.TestCase):
    def test_retries_a_503_then_succeeds(self):
        sleeps = []
        failed = Mock(ok=False, status_code=503)
        succeeded = Mock(ok=True)
        with patch(
            "live_relay.metadata.requests.post",
            side_effect=[failed, succeeded],
        ) as post:
            result = post_gemini(
                "https://example.com/x",
                headers={},
                body={},
                timeout=(4, 15),
                sleep=sleeps.append,
            )
        self.assertIs(result, succeeded)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleeps, [1.0])

    def test_retries_a_429_and_network_errors(self):
        sleeps = []
        failed = Mock(ok=False, status_code=429)
        succeeded = Mock(ok=True)
        with patch(
            "live_relay.metadata.requests.post",
            side_effect=[
                failed,
                requests.exceptions.ConnectionError("down"),
                succeeded,
            ],
        ) as post:
            result = post_gemini(
                "https://example.com/x",
                headers={},
                body={},
                timeout=(4, 15),
                sleep=sleeps.append,
            )
        self.assertIs(result, succeeded)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_non_retryable_400_fails_immediately(self):
        failed = Mock(ok=False, status_code=400)
        with patch("live_relay.metadata.requests.post", return_value=failed) as post:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                post_gemini(
                    "https://example.com/x",
                    headers={},
                    body={},
                    timeout=(4, 15),
                    sleep=lambda seconds: None,
                )
        self.assertEqual(post.call_count, 1)

    def test_exhausted_retries_raise_the_last_error(self):
        sleeps = []
        failed = Mock(ok=False, status_code=503)
        with patch("live_relay.metadata.requests.post", return_value=failed) as post:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                post_gemini(
                    "https://example.com/x",
                    headers={},
                    body={},
                    timeout=(4, 15),
                    sleep=sleeps.append,
                )
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleeps, [1.0, 2.0])


class GeminiRetryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.source = LiveSource(
            title="شرح بث مباشر للألعاب",
            urls=("https://source.example/private-stream",),
            headers=(),
            description="دليل عملي.",
            channel="قناة الألعاب",
            categories=("Gaming",),
            tags=("ألعاب", "بث مباشر"),
        )
        self.settings = SimpleNamespace(
            gemini_api_key="test-api-secret",
            gemini_model="gemini-3.8-flash",
            gemini_metadata_enabled=True,
            metadata_language="ar",
            youtube_category_id="20",
        )

    def test_generate_metadata_retries_a_503_then_uses_gemini(self):
        failed = Mock(ok=False, status_code=503)
        with patch(
            "live_relay.metadata.requests.post",
            side_effect=[failed, _gemini_metadata_response()],
        ) as post:
            result = generate_metadata(self.settings, self.source)
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(post.call_count, 2)

    def test_generate_image_bytes_retries_a_503_then_returns_the_image(self):
        image = base64.b64encode(b"fake-png-bytes")
        failed = Mock(ok=False, status_code=503)
        succeeded = Mock(ok=True)
        succeeded.json.return_value = {"output_image": {"data": image.decode()}}
        settings = SimpleNamespace(
            gemini_api_key="test-key",
            gemini_image_model="gemini-nano-banana-2.1",
        )
        with patch(
            "live_relay.metadata.requests.post",
            side_effect=[failed, succeeded],
        ) as post:
            result = generate_image_bytes(settings, "prompt")
        self.assertEqual(result, b"fake-png-bytes")
        self.assertEqual(post.call_count, 2)

    def test_failed_thumbnail_does_not_consume_the_throttle(self):
        settings = SimpleNamespace(
            gemini_api_key="test-key",
            gemini_thumbnail_enabled=True,
            gemini_image_model="gemini-nano-banana-2.1",
            ffmpeg_path="ffmpeg",
            frame_enabled=False,
            frame_file="",
        )
        source = SimpleNamespace(title="t", description="d", channel="c", tags=("x",))
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            # A failed generation must not stamp the 10-minute window.
            with mock.patch.object(
                thumbnail_ai, "generate_image_bytes", return_value=None
            ):
                self.assertEqual(
                    generate_auto_thumbnail(settings, source, "t", state_dir),
                    "",
                )
            # The very next attempt still runs and succeeds.
            with (
                mock.patch.object(
                    thumbnail_ai,
                    "generate_image_bytes",
                    return_value=b"fake-image",
                ),
                mock.patch.object(thumbnail_ai, "_frame_path", return_value=None),
                mock.patch("live_relay.thumbnails.save_thumbnail") as save,
            ):
                self.assertEqual(
                    generate_auto_thumbnail(settings, source, "t", state_dir),
                    "gemini",
                )
                save.assert_called_once()
            # And the success does stamp the throttle.
            with mock.patch.object(
                thumbnail_ai, "generate_image_bytes", return_value=b"fake-image"
            ):
                self.assertEqual(
                    generate_auto_thumbnail(settings, source, "t", state_dir),
                    "",
                )


# ---------------------------------------------------------------------------
# M16: notification-failure visibility
# ---------------------------------------------------------------------------


class _FakeUrlopenResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


class NotifierFailureTests(unittest.TestCase):
    def test_webhook_http_error_is_recorded(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        error = HTTPError("https://example.com/hook", 500, "Server Error", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            notifier._deliver("relay_ended", {})
        self.assertEqual(notifier.failure_count(), 1)
        failure = notifier.last_failure()
        self.assertIsNotNone(failure)
        self.assertEqual(failure["channel"], "webhook")
        self.assertEqual(failure["event"], "relay_ended")
        self.assertEqual(failure["error"], "HTTP 500")

    def test_network_error_is_recorded(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        with mock.patch("urllib.request.urlopen", side_effect=URLError("down")):
            notifier._deliver("relay_ended", {})
        self.assertEqual(notifier.failure_count(), 1)
        self.assertEqual(notifier.last_failure()["error"], "URLError")

    def test_success_records_nothing(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeUrlopenResponse(),
        ):
            notifier._deliver("relay_ended", {})
        self.assertEqual(notifier.failure_count(), 0)
        self.assertIsNone(notifier.last_failure())

    def test_on_failure_callback_receives_failure_and_total(self):
        callback = Mock()
        notifier = Notifier(webhook_url="https://example.com/hook", on_failure=callback)
        with mock.patch("urllib.request.urlopen", side_effect=URLError("down")):
            notifier._deliver("relay_ended", {})
        callback.assert_called_once()
        failure, total = callback.call_args.args
        self.assertEqual(failure["channel"], "webhook")
        self.assertEqual(total, 1)

    def test_telegram_failure_is_recorded_separately(self):
        notifier = Notifier(
            telegram_token="token",
            telegram_chat="123",
            webhook_url="https://example.com/hook",
        )
        with mock.patch("urllib.request.urlopen", side_effect=URLError("down")):
            notifier._deliver("relay_ended", {})
        # Both channels failed: the last recorded one is telegram.
        self.assertEqual(notifier.failure_count(), 2)
        self.assertEqual(notifier.last_failure()["channel"], "telegram")

    def test_notifier_from_settings_passes_the_callback(self):
        callback = Mock()
        settings = SimpleNamespace(
            notify_telegram_token="",
            notify_telegram_chat="",
            notify_webhook_url="https://example.com/hook",
            notify_language="ar",
        )
        notifier = notifier_from_settings(settings, on_failure=callback)
        self.assertIs(notifier.on_failure, callback)

    def test_relay_status_defaults_carry_notification_fields(self):
        snapshot = RelayStatus().snapshot()
        self.assertEqual(snapshot["notification_failures"], [])
        self.assertEqual(snapshot["notification_failure_count"], 0)
        self.assertEqual(snapshot["youtube_quota"], {})


# ---------------------------------------------------------------------------
# M20: manifest-based update sync
# ---------------------------------------------------------------------------


def _zip_bytes(files: dict[str, str], top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            arcname = f"{top}/{name}" if top else name
            archive.writestr(arcname, content)
    return buffer.getvalue()


def _update_tree(root: Path, version: str = "4.8.8") -> dict[str, str]:
    files = {
        "src/live_relay/__init__.py": f'__version__ = "{version}"\n',
        "src/live_relay/service.py": "# relay\n",
        "README.md": "# live-relay\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return files


class ManifestVerificationTests(unittest.TestCase):
    def test_valid_tree_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            manifest = updater.build_manifest(root, "4.8.8")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertTrue(updater.verify_manifest(root, "4.8.8"))

    def test_tampered_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            manifest = updater.build_manifest(root, "4.8.8")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "README.md").write_text("# tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                updater.verify_manifest(root, "4.8.8")

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            manifest = updater.build_manifest(root, "4.8.8")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "README.md").unlink()
            with self.assertRaisesRegex(ValueError, "missing file"):
                updater.verify_manifest(root, "4.8.8")

    def test_unlisted_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            manifest = updater.build_manifest(root, "4.8.8")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "unexpected.py").write_text("# surprise\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unlisted file"):
                updater.verify_manifest(root, "4.8.8")

    def test_wrong_version_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            manifest = updater.build_manifest(root, "4.8.8")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different version"):
                updater.verify_manifest(root, "4.8.9")

    def test_tree_without_a_manifest_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            self.assertFalse(updater.verify_manifest(root, "4.8.8"))

    def test_manifest_excludes_secrets_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root)
            (root / ".env").write_text("SECRET=x\n", encoding="utf-8")
            (root / "auto.key").write_text("key\n", encoding="utf-8")
            cache = root / ".pytest_cache"
            cache.mkdir()
            (cache / "v").write_text("cache\n", encoding="utf-8")
            manifest = updater.build_manifest(root, "4.8.8")
            paths = [entry["path"] for entry in manifest["files"]]
            self.assertNotIn(".env", paths)
            self.assertNotIn("auto.key", paths)
            self.assertNotIn(".pytest_cache/v", paths)
            self.assertNotIn("MANIFEST.json", paths)

    def test_apply_with_a_valid_manifest_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            state = Path(directory) / "state"
            project.mkdir()
            state.mkdir()
            files = _update_tree(project)
            manifest = updater.build_manifest(project, "4.8.8")
            files["MANIFEST.json"] = json.dumps(manifest)
            zip_bytes = _zip_bytes(files, top="live-relay-4.8.8")
            zip_path = Path(directory) / "update.zip"
            zip_path.write_bytes(zip_bytes)
            release = {"version": "4.8.8", "tag": "v4.8.8", "html_url": ""}
            updater.stage_update(state, release, zip_path)
            applied = updater.apply_pending_update(project, state)
            self.assertEqual(applied, "4.8.8")
            self.assertEqual(
                (project / "README.md").read_text(encoding="utf-8"),
                "# live-relay\n",
            )

    def test_apply_refuses_a_tampered_manifested_update(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            state = Path(directory) / "state"
            project.mkdir()
            state.mkdir()
            files = _update_tree(project)
            manifest = updater.build_manifest(project, "4.8.8")
            files["MANIFEST.json"] = json.dumps(manifest)
            files["README.md"] = "# tampered after the manifest\n"
            zip_bytes = _zip_bytes(files, top="live-relay-4.8.8")
            zip_path = Path(directory) / "update.zip"
            zip_path.write_bytes(zip_bytes)
            release = {"version": "4.8.8", "tag": "v4.8.8", "html_url": ""}
            updater.stage_update(state, release, zip_path)
            with self.assertRaisesRegex(ValueError, "does not match"):
                updater.apply_pending_update(project, state)

    def test_committed_manifest_matches_the_project_tree(self):
        committed = json.loads(
            (PROJECT_ROOT / "MANIFEST.json").read_text(encoding="utf-8")
        )
        regenerated = updater.build_manifest(PROJECT_ROOT, __version__)
        self.assertEqual(committed, regenerated)


# ---------------------------------------------------------------------------
# M2: session-scoped dashboard token
# ---------------------------------------------------------------------------


class SessionTokenTests(unittest.TestCase):
    def make_controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={Path(root) / 'state'}\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            return DashboardController(Path(root))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-session-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        self.controller = self.make_controller(root)
        html_path = Path(dashboard_module.__file__).with_name("dashboard.html")
        handler = build_handler_class(self.controller, html_path)
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

    def _page_session_token(self) -> str:
        response = requests.get(f"{self.base_url()}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'SESSION_TOKEN="([^"]+)"', response.text)
        self.assertIsNotNone(match, "page carries no session token")
        return match.group(1)

    def test_page_does_not_contain_the_real_control_token(self):
        response = requests.get(f"{self.base_url()}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.controller.control_token, response.text)

    def test_two_page_loads_issue_different_session_tokens(self):
        first = self._page_session_token()
        second = self._page_session_token()
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, self.controller.control_token)
        self.assertNotEqual(second, self.controller.control_token)

    def test_session_token_authorizes_api_calls(self):
        token = self._page_session_token()
        response = requests.get(
            f"{self.base_url()}/api/status",
            headers={"X-Relay-Token": token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)

    def test_bogus_token_is_rejected(self):
        response = requests.get(
            f"{self.base_url()}/api/status",
            headers={"X-Relay-Token": "bogus-token"},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)

    def test_real_control_token_still_authorizes_api_calls(self):
        response = requests.get(
            f"{self.base_url()}/api/status",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)

    def test_expired_session_token_is_rejected(self):
        token = self.controller.issue_session_token()
        self.controller._session_tokens[token] = time.monotonic() - 1
        response = requests.get(
            f"{self.base_url()}/api/status",
            headers={"X-Relay-Token": token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)

    def test_status_includes_notification_and_quota_fields(self):
        response = requests.get(
            f"{self.base_url()}/api/status",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["notification_failures"], [])
        self.assertEqual(data["notification_failure_count"], 0)
        self.assertEqual(data["youtube_quota"], {})


if __name__ == "__main__":
    unittest.main()
