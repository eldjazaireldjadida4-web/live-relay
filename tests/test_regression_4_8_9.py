"""Regression tests for v4.8.9: the second audit of the 4.8.8 features.

- H1: the dashboard "YouTube status" button built a fresh destination and
  asked it for a status, which could never succeed; it now reads the live
  session's destination and says plainly when nothing is live.
- M1: the YouTube quota tracker only counted status probes; session start,
  transitions, metadata updates and auth calls are now recorded at their
  published quota costs (list/read 1 unit, writes 50).
- M2: the shared yt-dlp pump blocked on one slow subscriber and starved
  every other destination; subscriber writes are now non-blocking, and a
  subscriber whose input stays full is dropped so its worker restarts.
- M3: a hand-edited bad LOGO_SIZE_PERCENT / LOGO_OPACITY /
  YOUTUBE_QUOTA_BUDGET bricked startup and blocked the dashboard; the
  settings now fall back to defaults (and the dashboard notes, rather
  than blocks on, the fallback).
- M4: the updater never removed files deleted upstream; files it installed
  before and the new release no longer ships are removed on apply.
- L1: notification-failure recording is now atomic on the status lock,
  so two failing deliveries cannot drop each other's entry.
- L2: Gemini retries now include HTTP 408 (request timeout), like the
  429/5xx statuses they already retried.
- L3: manifest verification skips files the manifest generator itself
  excludes (.DS_Store, caches), instead of failing the update.
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
from unittest.mock import Mock, patch

from live_relay import updater
from live_relay.config import load_settings
from live_relay.dashboard import PROJECT_ROOT, DashboardController
from live_relay.destinations import (
    DestinationSession,
    YouTubeDestination,
)
from live_relay.metadata import StreamMetadata, post_gemini
from live_relay.notifier import Notifier
from live_relay.runtime import RelayStatus
from live_relay.source import SharedSourcePipe

METADATA = StreamMetadata("عنوان البث", "الوصف", ("وسم",))


# ---------------------------------------------------------------------------
# Helpers: a fake YouTube API service whose calls record their quota cost
# ---------------------------------------------------------------------------


class _FakeCall:
    def __init__(self, payload=None) -> None:
        self._payload = payload

    def execute(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _FakeResource:
    def __init__(self, service: "_FakeYouTubeService", name: str) -> None:
        self._service = service
        self._name = name

    def __getattr__(self, method: str):
        def call(**kwargs):
            self._service.calls.append((self._name, method))
            payload = self._service.payloads.get((self._name, method))
            if callable(payload):
                payload = payload(**kwargs)
            return _FakeCall(payload)

        return call


class _FakeYouTubeService:
    """Stand-in for the Google API client; payloads keyed by (resource, method)."""

    def __init__(self, payloads=None) -> None:
        self.payloads = dict(payloads or {})
        self.calls: list[tuple[str, str]] = []

    def liveBroadcasts(self):
        return _FakeResource(self, "liveBroadcasts")

    def liveStreams(self):
        return _FakeResource(self, "liveStreams")

    def channels(self):
        return _FakeResource(self, "channels")

    def videos(self):
        return _FakeResource(self, "videos")

    def thumbnails(self):
        return _FakeResource(self, "thumbnails")


def _youtube_settings(state_dir: Path, **overrides):
    settings = {
        "youtube_quota_budget": 10000,
        "youtube_client_secrets": str(state_dir / "client_secrets.json"),
        "youtube_channel_id": "UC-test-channel",
        "youtube_privacy": "unlisted",
        "youtube_category_id": "22",
        "youtube_backup": False,
        "state_dir": state_dir,
    }
    settings.update(overrides)
    return SimpleNamespace(**settings)


def _live_broadcast_payload():
    return {"id": "broadcast-id", "status": {"lifeCycleStatus": "live"}}


def _live_stream_payload():
    return {
        "id": "stream-id",
        "cdn": {
            "ingestionInfo": {
                "streamName": "stream-name",
                "rtmpsIngestionAddress": "rtmps://a.rtmp.youtube.com/live2",
            }
        },
    }


# ---------------------------------------------------------------------------
# M1: quota costs for every YouTube API call
# ---------------------------------------------------------------------------


class YouTubeQuotaCostsTests(unittest.TestCase):
    def test_authenticate_records_the_channel_list(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            destination = YouTubeDestination(_youtube_settings(state_dir))
            credentials = SimpleNamespace(valid=True, to_json=lambda: "{}")
            service = _FakeYouTubeService(
                {("channels", "list"): {"items": [{"id": "UC-test-channel"}]}}
            )
            with (
                patch.object(
                    YouTubeDestination,
                    "_load_credentials",
                    return_value=(state_dir / "youtube-token.json", credentials),
                ),
                patch(
                    "live_relay.destinations._youtube_service",
                    return_value=service,
                ),
            ):
                destination.authenticate(allow_browser=False)
            self.assertEqual(destination.quota.used, 1)

    def test_check_status_records_each_list_separately(self):
        destination = YouTubeDestination(SimpleNamespace(youtube_quota_budget=10000))
        service = _FakeYouTubeService(
            {
                ("liveBroadcasts", "list"): {
                    "items": [{"status": {"lifeCycleStatus": "live"}}]
                },
                ("liveStreams", "list"): RuntimeError("streams probe failed"),
            }
        )
        destination.service = service
        destination.broadcast = {"id": "broadcast-id"}
        destination.stream = {"id": "stream-id"}
        with self.assertRaises(RuntimeError):
            destination.check_status()
        # Both lists were attempted, so both cost 1 unit: YouTube
        # charges for failed calls too, and the old code (which only
        # counted successes) under-reported the real spend.
        self.assertEqual(destination.quota.used, 2)

    def test_ensure_live_records_the_transition(self):
        destination = YouTubeDestination(SimpleNamespace(youtube_quota_budget=10000))
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "transition"): {}}
        )
        destination.broadcast = {"id": "broadcast-id"}
        self.assertTrue(destination.ensure_live())
        self.assertEqual(destination.quota.used, 50)

    def test_update_metadata_records_both_updates(self):
        destination = YouTubeDestination(
            SimpleNamespace(youtube_quota_budget=10000, youtube_category_id="22")
        )
        destination.service = _FakeYouTubeService(
            {
                ("liveBroadcasts", "update"): _live_broadcast_payload(),
                ("videos", "update"): {},
            }
        )
        destination.broadcast = {"id": "broadcast-id", "snippet": {}}
        destination.update_metadata(METADATA)
        self.assertEqual(destination.quota.used, 100)

    def test_update_thumbnail_records_the_set(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            image = state_dir / "thumb.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            destination = YouTubeDestination(_youtube_settings(state_dir))
            destination.service = _FakeYouTubeService({("thumbnails", "set"): {}})
            destination.broadcast = {"id": "broadcast-id"}
            destination.update_thumbnail(image)
            self.assertEqual(destination.quota.used, 50)

    def test_end_broadcast_records_the_transition(self):
        destination = YouTubeDestination(SimpleNamespace(youtube_quota_budget=10000))
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "transition"): {}}
        )
        destination.end_broadcast({"id": "broadcast-id"})
        self.assertEqual(destination.quota.used, 50)

    def test_get_stream_reuses_a_saved_idle_stream_for_one_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "youtube-stream.json").write_text(
                json.dumps({"stream_id": "stream-id"}), encoding="utf-8"
            )
            destination = YouTubeDestination(_youtube_settings(state_dir))
            destination.service = _FakeYouTubeService(
                {
                    ("liveStreams", "list"): {
                        "items": [
                            {"id": "stream-id", "status": {"streamStatus": "ready"}}
                        ]
                    }
                }
            )
            stream = destination._get_stream()
            self.assertEqual(stream["id"], "stream-id")
            self.assertEqual(destination.quota.used, 1)

    def test_get_stream_records_the_insert_for_a_new_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            destination = YouTubeDestination(_youtube_settings(state_dir))
            destination.service = _FakeYouTubeService(
                {("liveStreams", "insert"): _live_stream_payload()}
            )
            stream = destination._get_stream()
            self.assertEqual(stream["id"], "stream-id")
            self.assertEqual(destination.quota.used, 50)

    def test_start_records_the_full_session_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            destination = YouTubeDestination(_youtube_settings(state_dir))
            service = _FakeYouTubeService(
                {
                    ("liveBroadcasts", "insert"): _live_broadcast_payload(),
                    ("liveStreams", "insert"): _live_stream_payload(),
                    ("liveBroadcasts", "bind"): {},
                    ("videos", "update"): {},
                }
            )
            with patch.object(
                YouTubeDestination,
                "authenticate",
                lambda self, allow_browser=True: setattr(self, "service", service),
            ):
                targets = destination.start(METADATA)
            # insert 50 + stream insert 50 + bind 50 + video update 50
            self.assertEqual(destination.quota.used, 200)
            self.assertEqual(
                targets, [("youtube", "rtmps://a.rtmp.youtube.com/live2/stream-name")]
            )


# ---------------------------------------------------------------------------
# H1: dashboard YouTube status reads the live session
# ---------------------------------------------------------------------------


class YouTubeBroadcastStatusTests(unittest.TestCase):
    def _controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={Path(root) / 'state'}\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            return DashboardController(Path(root))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-status-", dir=PROJECT_ROOT
        )
        self.controller = self._controller(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_uses_the_live_session_destination(self):
        fake = SimpleNamespace(
            check_status=lambda: {
                "broadcast_status": "live",
                "stream_status": "active",
            }
        )
        session = DestinationSession(metadata=METADATA, base_metadata=METADATA)
        session.youtube = fake
        self.controller.status.set_destination_session(session)
        self.assertEqual(
            self.controller.youtube_broadcast_status(),
            {"broadcast_status": "live", "stream_status": "active"},
        )

    def test_status_without_a_live_broadcast_reports_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "No YouTube API broadcast"):
            self.controller.youtube_broadcast_status()

    def test_status_with_a_static_key_session_reports_clearly(self):
        session = DestinationSession(metadata=METADATA, base_metadata=METADATA)
        session.youtube = None
        self.controller.status.set_destination_session(session)
        with self.assertRaisesRegex(RuntimeError, "No YouTube API broadcast"):
            self.controller.youtube_broadcast_status()


# ---------------------------------------------------------------------------
# M2: a slow subscriber must not starve the shared yt-dlp pipe
# ---------------------------------------------------------------------------


class _BlockedSubscriber:
    """A subscriber backed by a real pipe fd whose buffer stays full."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._write_fd, False)
        # Fill the pipe buffer (64 KiB on Linux) so writes refuse.
        while True:
            try:
                os.write(self._write_fd, b"x" * 4096)
            except BlockingIOError:
                break

    def fileno(self) -> int:
        return self._write_fd

    def close(self) -> None:
        for fd in (self._read_fd, self._write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class SharedPipeBlockedSubscriberTests(unittest.TestCase):
    def test_blocked_subscriber_does_not_starve_the_others(self):
        settings = SimpleNamespace(
            pipe_mode=True,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
            frame_enabled=False,
            frame_file="",
            record_enabled=False,
            format_selector="best",
            source_url="https://example.com/live",
            cookie_file="",
            cookies_from_browser="",
        )
        pipe = SharedSourcePipe(settings)
        # A fake yt-dlp stdout the test drives by hand.
        read_fd, write_fd = os.pipe()
        pipe._process = SimpleNamespace(stdout=os.fdopen(read_fd, "rb", buffering=0))
        good = io.BytesIO()
        blocked = _BlockedSubscriber()
        pipe.subscribe(good)
        pipe.subscribe(blocked)
        pump = threading.Thread(target=pipe._pump_stdout, daemon=True)
        pump.start()
        try:
            # Twelve chunks, one at a time: each write is its own pump
            # read, so the blocked subscriber refuses twelve times and
            # is dropped after ten, while the good one keeps receiving.
            # With the old blocking pump the good subscriber would
            # never see chunk two.
            for index in range(12):
                os.write(write_fd, f"CHUNK-{index}\n".encode())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if good.getvalue().count(b"CHUNK-") >= index + 1:
                        break
                    time.sleep(0.01)
            self.assertEqual(good.getvalue().count(b"CHUNK-"), 12)
            self.assertEqual(pipe.subscriber_count(), 1)
        finally:
            os.close(write_fd)
            pump.join(timeout=5)
            blocked.close()
            self.assertFalse(pump.is_alive())

    def test_pump_write_reports_a_full_pipe_instead_of_blocking(self):
        settings = SimpleNamespace(
            pipe_mode=True,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
            frame_enabled=False,
            frame_file="",
            record_enabled=False,
            format_selector="best",
            source_url="https://example.com/live",
            cookie_file="",
            cookies_from_browser="",
        )
        pipe = SharedSourcePipe(settings)
        blocked = _BlockedSubscriber()
        try:
            started = time.monotonic()
            self.assertEqual(pipe._pump_write(blocked, b"data"), "blocked")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            blocked.close()

    def test_pump_write_still_works_for_streams_without_a_fd(self):
        settings = SimpleNamespace(
            pipe_mode=True,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
            frame_enabled=False,
            frame_file="",
            record_enabled=False,
            format_selector="best",
            source_url="https://example.com/live",
            cookie_file="",
            cookies_from_browser="",
        )
        pipe = SharedSourcePipe(settings)
        buffer = io.BytesIO()
        self.assertTrue(pipe._pump_write(buffer, b"data"))
        self.assertEqual(buffer.getvalue(), b"data")


# ---------------------------------------------------------------------------
# M3: bad logo / quota values fall back instead of bricking startup
# ---------------------------------------------------------------------------


class BadValueFallbackTests(unittest.TestCase):
    def _env(self, **overrides):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
        }
        env.update(overrides)
        return env

    def test_load_settings_falls_back_on_non_numeric_logo_values(self):
        with patch.dict(
            os.environ,
            self._env(LOGO_SIZE_PERCENT="abc", LOGO_OPACITY="xyz"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.logo_size_percent, 12)
        self.assertEqual(settings.logo_opacity, 0.9)

    def test_load_settings_falls_back_on_out_of_range_logo_values(self):
        # Out-of-range values fall back to the defaults the dashboard
        # reports (12% / 0.9), instead of clamping to the boundary:
        # the displayed settings must match what the encoder uses.
        with patch.dict(
            os.environ,
            self._env(LOGO_SIZE_PERCENT="99", LOGO_OPACITY="5"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.logo_size_percent, 12)
        self.assertEqual(settings.logo_opacity, 0.9)
        with patch.dict(
            os.environ,
            self._env(LOGO_SIZE_PERCENT="1", LOGO_OPACITY="0.01"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.logo_size_percent, 12)
        self.assertEqual(settings.logo_opacity, 0.9)
        with patch.dict(
            os.environ,
            self._env(LOGO_SIZE_PERCENT="nan", LOGO_OPACITY="nan"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.logo_size_percent, 12)
        self.assertEqual(settings.logo_opacity, 0.9)

    def test_load_settings_falls_back_on_a_non_numeric_quota_budget(self):
        with patch.dict(os.environ, self._env(YOUTUBE_QUOTA_BUDGET="abc"), clear=True):
            settings = load_settings()
        self.assertEqual(settings.youtube_quota_budget, 10000)

    def test_public_config_fallback_notices_are_not_blockers(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-fallback-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "LIVE_PLATFORMS=youtube,facebook\n"
                "YOUTUBE_STREAM_KEY=dummy-key\n"
                "FACEBOOK_STREAM_KEY=dummy-key\n"
                "LOGO_SIZE_PERCENT=abc\n"
                "LOGO_OPACITY=xyz\n"
                "YOUTUBE_QUOTA_BUDGET=abc\n"
                f"LIVE_RELAY_STATE_DIR={root / 'state'}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
            config = controller.public_config()
            # A fallback notice must not block starting the relay.
            self.assertNotIn(
                "LOGO_SIZE_PERCENT is not a number; using 12%.",
                config["missing"],
            )
            self.assertNotIn(
                "LOGO_OPACITY is not a number; using 0.9.",
                config["missing"],
            )
            self.assertNotIn(
                "YOUTUBE_QUOTA_BUDGET is not a number; using 10000.",
                config["missing"],
            )
            self.assertIn(
                "LOGO_SIZE_PERCENT is not a number; using 12%.",
                config["notes"],
            )
            self.assertIn(
                "LOGO_OPACITY is not a number; using 0.9.",
                config["notes"],
            )
            self.assertIn(
                "YOUTUBE_QUOTA_BUDGET is not a number; using 10000.",
                config["notes"],
            )
            self.assertEqual(config["logo_size_percent"], 12)
            self.assertEqual(config["logo_opacity"], 0.9)
            self.assertEqual(config["youtube_quota_budget"], 10000)


# ---------------------------------------------------------------------------
# M4: the updater removes files deleted upstream
# ---------------------------------------------------------------------------


def _zip_bytes(files: dict[str, str], top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            arcname = f"{top}/{name}" if top else name
            archive.writestr(arcname, content)
    return buffer.getvalue()


def _update_tree(root: Path, version: str, extra: bool = False) -> dict[str, str]:
    files = {
        "src/live_relay/__init__.py": f'__version__ = "{version}"\n',
        "src/live_relay/service.py": "# relay\n",
        "README.md": "# live-relay\n",
    }
    if extra:
        files["src/live_relay/removed_module.py"] = "# deleted upstream\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return files


class StaleFileRemovalTests(unittest.TestCase):
    def _stage_and_apply(
        self,
        project: Path,
        state: Path,
        directory: Path,
        version: str,
        extra: bool,
    ) -> None:
        # Build the release tree in a staging directory, not in the
        # project: the project still holds the previous release's
        # files, and a file deleted upstream must not sneak into the
        # new manifest through it.
        tree = directory / f"tree-{version}"
        tree.mkdir()
        files = _update_tree(tree, version, extra=extra)
        manifest = updater.build_manifest(tree, version)
        files["MANIFEST.json"] = json.dumps(manifest)
        zip_bytes = _zip_bytes(files, top=f"live-relay-{version}")
        zip_path = directory / f"update-{version}.zip"
        zip_path.write_bytes(zip_bytes)
        release = {"version": version, "tag": f"v{version}", "html_url": ""}
        updater.stage_update(state, release, zip_path)
        self.assertEqual(updater.apply_pending_update(project, state), version)

    def test_a_file_removed_upstream_is_removed_on_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            state = base / "state"
            project.mkdir()
            state.mkdir()
            self._stage_and_apply(project, state, base, "4.8.9", extra=True)
            stale = project / "src" / "live_relay" / "removed_module.py"
            self.assertTrue(stale.is_file())
            self._stage_and_apply(project, state, base, "4.8.10", extra=False)
            self.assertFalse(stale.exists())
            # The files the new release ships are still there.
            self.assertTrue((project / "src" / "live_relay" / "service.py").is_file())

    def test_user_files_and_preserved_files_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            state = base / "state"
            project.mkdir()
            state.mkdir()
            self._stage_and_apply(project, state, base, "4.8.9", extra=True)
            user_file = project / "user-notes.txt"
            user_file.write_text("mine\n", encoding="utf-8")
            env_file = project / ".env"
            env_file.write_text("SECRET=<redacted>\n", encoding="utf-8")
            self._stage_and_apply(project, state, base, "4.8.10", extra=False)
            self.assertTrue(user_file.is_file())
            self.assertTrue(env_file.is_file())


# ---------------------------------------------------------------------------
# L1: notification-failure recording is atomic
# ---------------------------------------------------------------------------


class NotificationFailureRaceTests(unittest.TestCase):
    def test_concurrent_failures_are_all_recorded(self):
        status = RelayStatus()
        failures = [
            {"kind": "telegram", "error": f"down {index}", "at": float(index)}
            for index in range(20)
        ]
        threads = [
            threading.Thread(
                target=status.record_notification_failure,
                args=(failure, index + 1),
            )
            for index, failure in enumerate(failures)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        snapshot = status.snapshot()
        self.assertEqual(snapshot["notification_failure_count"], 20)
        # Only the last five are kept, but none were lost to a race.
        self.assertEqual(len(snapshot["notification_failures"]), 5)

    def test_concurrent_deliveries_via_the_notifier(self):
        status = RelayStatus()
        notifier = Notifier(
            telegram_token="token",
            telegram_chat="chat",
            webhook_url="http://127.0.0.1:1/hook",
            on_failure=status.record_notification_failure,
        )
        # Both channels fail fast and deterministically (no real network).
        with patch(
            "live_relay.notifier.urllib.request.urlopen",
            side_effect=Exception("down"),
        ):
            threads = [
                threading.Thread(target=notifier._deliver, args=("test", {"n": index}))
                for index in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        snapshot = status.snapshot()
        # Two failing channels per delivery, four deliveries: eight
        # failures, none lost to a race; only the last five are kept.
        self.assertEqual(snapshot["notification_failure_count"], 8)
        self.assertEqual(len(snapshot["notification_failures"]), 5)


# ---------------------------------------------------------------------------
# L2: Gemini retries HTTP 408
# ---------------------------------------------------------------------------


class PostGemini408RetryTests(unittest.TestCase):
    def test_retries_a_408_then_succeeds(self):
        sleeps = []
        failed = Mock(ok=False, status_code=408)
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


# ---------------------------------------------------------------------------
# L3: manifest verification skips files the generator excludes
# ---------------------------------------------------------------------------


class ManifestExclusionTests(unittest.TestCase):
    def test_excluded_files_do_not_fail_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root, "4.8.9")
            manifest = updater.build_manifest(root, "4.8.9")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / ".DS_Store").write_bytes(b"junk")
            cache = root / ".pytest_cache"
            cache.mkdir()
            (cache / "v").write_text("cache\n", encoding="utf-8")
            (root / ".ruff_cache").mkdir()
            (root / ".ruff_cache" / "x").write_text("cache\n", encoding="utf-8")
            self.assertTrue(updater.verify_manifest(root, "4.8.9"))

    def test_a_genuinely_unlisted_file_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _update_tree(root, "4.8.9")
            manifest = updater.build_manifest(root, "4.8.9")
            (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "unexpected.py").write_text("# surprise\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unlisted file"):
                updater.verify_manifest(root, "4.8.9")


if __name__ == "__main__":
    unittest.main()
