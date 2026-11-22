"""Regression tests for v4.8.10: the third audit round.

- M1: METADATA_LANGUAGE=auto (offered by the dashboard) bricked startup:
  NOTIFY_LANGUAGE defaults to the metadata language, and "auto" is not a
  notifier language, so load_settings raised. The default now falls back
  to ar unless the metadata language is ar/en.
- M2: a hand-edited garbage boolean (PIPE_MODE=maybe) bricked startup:
  _flag raised instead of falling back, the same family as the numeric
  fallbacks fixed in 4.8.9. It now warns and uses the default; the
  dashboard still rejects garbage booleans strictly at save time.
- M3: the shared yt-dlp pump ignored partial os.write results: a slow
  subscriber that only ever accepted part of each chunk was reported as
  a success and never counted toward the drop threshold, so it received
  truncated chunks forever. A short write now counts as blocked.
- M4: failed YouTube API calls were not counted against the quota, but
  YouTube charges quota for attempted calls, failed ones included. The
  cost is now recorded before each call, not after its success.
- M5: check_for_updates / download_update held the controller lock
  across network I/O, blocking stop/save/start for up to 10 s (check)
  or 120 s (download). The fetch/download now run outside the lock;
  a dedicated download lock serializes concurrent downloads.
- M6: YouTubeQuotaTracker lost counts under concurrent recording
  (read-modify-write without a lock); it is now guarded by a lock.
- L1: LOGO_OPACITY=nan/inf (or out of range) disagreed between the
  runtime and the dashboard: min()/max() silently mapped nan to 0.1
  and inf to 1.0, while the dashboard displayed its 0.9 fallback.
  Out-of-range logo values now fall back to the defaults (12% / 0.9)
  in both places instead of clamping to the boundary.
- L2: a corrupt base64 image item in a Gemini response discarded the
  whole response, even when a later item carried a good image. The
  parser now skips the bad item and keeps looking.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from live_relay import updater
from live_relay.config import load_settings
from live_relay.dashboard import PROJECT_ROOT, DashboardController
from live_relay.destinations import YouTubeDestination, YouTubeQuotaTracker
from live_relay.metadata import StreamMetadata
from live_relay.source import SHARED_PIPE_BLOCKED_DROP_AFTER, SharedSourcePipe
from live_relay.thumbnail_ai import _image_from_steps

METADATA = StreamMetadata("عنوان البث", "الوصف", ("وسم",))


def _env(**overrides):
    env = {
        "LIVE_PLATFORMS": "youtube,facebook",
        "YOUTUBE_STREAM_KEY": "dummy-key",
        "FACEBOOK_STREAM_KEY": "dummy-key",
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# M1: METADATA_LANGUAGE=auto must not brick startup via NOTIFY_LANGUAGE
# ---------------------------------------------------------------------------


class NotifyLanguageDefaultTests(unittest.TestCase):
    def test_metadata_language_auto_falls_back_to_ar(self):
        with mock.patch.dict(os.environ, _env(METADATA_LANGUAGE="auto"), clear=True):
            settings = load_settings()
        self.assertEqual(settings.metadata_language, "auto")
        self.assertEqual(settings.notify_language, "ar")

    def test_metadata_language_fr_falls_back_to_ar(self):
        with mock.patch.dict(os.environ, _env(METADATA_LANGUAGE="fr"), clear=True):
            settings = load_settings()
        self.assertEqual(settings.notify_language, "ar")

    def test_metadata_language_ar_is_inherited(self):
        with mock.patch.dict(os.environ, _env(METADATA_LANGUAGE="ar"), clear=True):
            settings = load_settings()
        self.assertEqual(settings.notify_language, "ar")

    def test_explicit_notify_language_is_still_respected(self):
        with mock.patch.dict(
            os.environ,
            _env(METADATA_LANGUAGE="auto", NOTIFY_LANGUAGE="en"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.notify_language, "en")

    def test_explicit_bad_notify_language_is_still_rejected(self):
        with mock.patch.dict(
            os.environ,
            _env(METADATA_LANGUAGE="ar", NOTIFY_LANGUAGE="fr"),
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "NOTIFY_LANGUAGE"):
                load_settings()


# ---------------------------------------------------------------------------
# M2: garbage booleans fall back instead of bricking startup
# ---------------------------------------------------------------------------


class BadBooleanFallbackTests(unittest.TestCase):
    def test_garbage_booleans_fall_back_to_defaults(self):
        with mock.patch.dict(
            os.environ,
            _env(
                PIPE_MODE="maybe",
                ADAPTIVE_QUALITY="banana",
                CHECK_FOR_UPDATES="nope",
                GEMINI_METADATA_ENABLED="perhaps",
            ),
            clear=True,
        ):
            settings = load_settings()
        self.assertFalse(settings.pipe_mode)
        self.assertTrue(settings.adaptive_quality)
        self.assertTrue(settings.check_for_updates)
        self.assertTrue(settings.gemini_metadata_enabled)

    def test_valid_boolean_spellings_still_parse(self):
        with mock.patch.dict(
            os.environ,
            _env(PIPE_MODE="yes", ADAPTIVE_QUALITY="0", CHECK_FOR_UPDATES="off"),
            clear=True,
        ):
            settings = load_settings()
        self.assertTrue(settings.pipe_mode)
        self.assertFalse(settings.adaptive_quality)
        self.assertFalse(settings.check_for_updates)


# ---------------------------------------------------------------------------
# L1: logo opacity/size fallbacks agree with the dashboard
# ---------------------------------------------------------------------------


class LogoFallbackConsistencyTests(unittest.TestCase):
    def test_nan_and_inf_opacity_fall_back_to_the_dashboard_default(self):
        for value in ("nan", "inf", "-inf"):
            with mock.patch.dict(os.environ, _env(LOGO_OPACITY=value), clear=True):
                settings = load_settings()
            self.assertEqual(settings.logo_opacity, 0.9, value)

    def test_out_of_range_size_falls_back_to_the_dashboard_default(self):
        for value in ("99", "1", "0"):
            with mock.patch.dict(os.environ, _env(LOGO_SIZE_PERCENT=value), clear=True):
                settings = load_settings()
            self.assertEqual(settings.logo_size_percent, 12, value)

    def test_runtime_and_dashboard_agree_on_a_bad_opacity(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-opacity-", dir=PROJECT_ROOT
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
                "LOGO_OPACITY=nan\n"
                f"LIVE_RELAY_STATE_DIR={root / 'state'}\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
            config = controller.public_config()
            self.assertEqual(config["logo_opacity"], 0.9)
            with mock.patch.dict(os.environ, _env(LOGO_OPACITY="nan"), clear=True):
                settings = load_settings()
            self.assertEqual(settings.logo_opacity, config["logo_opacity"])


# ---------------------------------------------------------------------------
# M4: failed YouTube API calls cost quota too
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


def _destination(**overrides):
    settings = {"youtube_quota_budget": 10000, "youtube_category_id": "22"}
    settings.update(overrides)
    return YouTubeDestination(SimpleNamespace(**settings))


class QuotaFailedCallsTests(unittest.TestCase):
    def test_failed_first_list_still_costs_one_unit(self):
        destination = _destination()
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "list"): RuntimeError("boom")}
        )
        destination.broadcast = {"id": "broadcast-id"}
        destination.stream = {"id": "stream-id"}
        with self.assertRaises(RuntimeError):
            destination.check_status()
        self.assertEqual(destination.quota.used, 1)

    def test_failed_transition_still_costs_fifty_units(self):
        destination = _destination()
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "transition"): RuntimeError("boom")}
        )
        destination.broadcast = {"id": "broadcast-id"}
        self.assertFalse(destination.ensure_live())
        self.assertEqual(destination.quota.used, 50)

    def test_failed_end_broadcast_still_costs_fifty_units(self):
        destination = _destination()
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "transition"): RuntimeError("boom")}
        )
        destination.end_broadcast({"id": "broadcast-id"})
        self.assertEqual(destination.quota.used, 50)

    def test_failed_metadata_update_still_costs_fifty_units(self):
        destination = _destination()
        destination.service = _FakeYouTubeService(
            {("liveBroadcasts", "update"): RuntimeError("boom")}
        )
        destination.broadcast = {"id": "broadcast-id", "snippet": {}}
        with self.assertRaises(RuntimeError):
            destination.update_metadata(METADATA)
        self.assertEqual(destination.quota.used, 50)

    def test_failed_stream_lookup_still_costs_one_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "youtube-stream.json").write_text(
                json.dumps({"stream_id": "stream-id"}), encoding="utf-8"
            )
            destination = YouTubeDestination(
                SimpleNamespace(
                    youtube_quota_budget=10000,
                    youtube_client_secrets="",
                    youtube_channel_id="UC-test",
                    youtube_privacy="public",
                    youtube_category_id="22",
                    youtube_backup=False,
                    state_dir=state_dir,
                )
            )
            destination.service = _FakeYouTubeService(
                {("liveStreams", "list"): RuntimeError("boom")}
            )
            # The lookup failure falls through to a fresh insert; the
            # attempted list still cost its unit.
            destination.service.payloads[("liveStreams", "insert")] = {
                "id": "stream-id",
                "cdn": {"ingestionInfo": {"streamName": "s"}},
            }
            destination._get_stream()
            self.assertEqual(destination.quota.used, 51)


# ---------------------------------------------------------------------------
# M6: the quota tracker must not lose counts under concurrent recording
# ---------------------------------------------------------------------------


class QuotaTrackerThreadSafetyTests(unittest.TestCase):
    def test_concurrent_recording_loses_nothing(self):
        tracker = YouTubeQuotaTracker(budget=10_000_000)

        def hammer() -> None:
            for _ in range(20000):
                tracker.record(1)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(tracker.used, 80000)

    def test_concurrent_snapshots_are_consistent(self):
        tracker = YouTubeQuotaTracker(budget=1000)
        errors: list[Exception] = []

        def record_and_read() -> None:
            try:
                for _ in range(1000):
                    tracker.record(1)
                    snapshot = tracker.snapshot()
                    assert snapshot["used"] <= 4000
                    assert snapshot["remaining"] == max(0, 1000 - snapshot["used"])
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=record_and_read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(tracker.used, 4000)


# ---------------------------------------------------------------------------
# M3: a partial write counts as blocked
# ---------------------------------------------------------------------------


class _SlowSubscriber:
    """A subscriber whose stdin accepts only part of each chunk."""

    def __init__(self, accepted: int = 100) -> None:
        self._accepted = accepted

    def fileno(self) -> int:
        return 99

    def write(self, data) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class PartialWriteTests(unittest.TestCase):
    def _pipe(self) -> SharedSourcePipe:
        settings = SimpleNamespace(
            format_selector="best",
            source_url="https://example.invalid/live",
            cookie_file="",
            cookies_from_browser="",
        )
        return SharedSourcePipe(settings)

    def test_partial_write_counts_as_blocked(self):
        pipe = self._pipe()
        subscriber = _SlowSubscriber()
        with mock.patch("live_relay.source.os.write", return_value=100):
            self.assertEqual(pipe._pump_write(subscriber, b"x" * 65536), "blocked")

    def test_full_write_still_reports_success(self):
        pipe = self._pipe()
        subscriber = _SlowSubscriber()
        with mock.patch("live_relay.source.os.write", return_value=65536):
            self.assertTrue(pipe._pump_write(subscriber, b"x" * 65536))

    def test_partially_draining_subscriber_is_dropped_by_the_pump(self):
        pipe = self._pipe()
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            # Leave only a little room in the pipe: the first write is
            # partial, the rest refuse outright. Either way the
            # subscriber is falling behind and must be dropped.
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            try:
                os.read(read_fd, 100)
            except OSError:
                pass

            class PipeSubscriber:
                def fileno(self) -> int:
                    return write_fd

                def close(self) -> None:
                    pass

            subscriber = PipeSubscriber()
            chunks = [b"x" * 65536] * (SHARED_PIPE_BLOCKED_DROP_AFTER + 5)
            stdout = io.BytesIO(b"".join(chunks))
            process = SimpleNamespace(
                stdout=stdout, stderr=io.BytesIO(), poll=lambda: None
            )
            pipe._process = process
            pipe.subscribe(subscriber)
            pump = threading.Thread(target=pipe._pump_stdout, daemon=True)
            pump.start()
            pump.join(timeout=5)
            self.assertEqual(pipe.subscriber_count(), 0)
        finally:
            for fd in (read_fd, write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# M5: update checks/downloads must not hold the controller lock
# ---------------------------------------------------------------------------


class UpdateLockTests(unittest.TestCase):
    def _controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            "SOURCE_URL=https://example.invalid\n"
            "UPDATE_REPO=owner/repo\n"
            f"LIVE_RELAY_STATE_DIR={root / 'state'}\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-lock-", dir=PROJECT_ROOT
        )
        self.controller = self._controller(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lock_is_free_within(self, seconds: float) -> bool:
        acquired = self.controller._lock.acquire(timeout=seconds)
        if acquired:
            self.controller._lock.release()
        return acquired

    def test_slow_download_does_not_block_the_controller_lock(self):
        updater.write_update_state(
            self.controller._update_state_dir(),
            latest={
                "version": "9.9.9",
                "tag": "v9.9.9",
                "html_url": "",
                "zipball_url": "https://example.invalid/x.zip",
                "assets": [],
            },
        )

        def slow_download(*args, **kwargs):
            time.sleep(2)
            return Path("/tmp/fake-4.8.10.zip")

        result: dict[str, object] = {}

        def run_download() -> None:
            try:
                result["status"] = self.controller.download_update()
            except Exception as exc:  # noqa: BLE001 - reported below
                result["error"] = exc

        with (
            mock.patch.object(updater, "download_release_zip", slow_download),
            mock.patch.object(updater, "stage_update", return_value={}),
        ):
            thread = threading.Thread(target=run_download)
            thread.start()
            try:
                time.sleep(0.3)
                # A 2 s download must not hold the lock for 2 s.
                self.assertTrue(self._lock_is_free_within(1.0))
            finally:
                thread.join(timeout=5)
        self.assertNotIn("error", result)
        self.assertIn("status", result)

    def test_slow_update_check_does_not_block_the_controller_lock(self):
        release = {
            "version": "9.9.9",
            "tag": "v9.9.9",
            "name": "v9.9.9",
            "body": "",
            "html_url": "",
            "zipball_url": "https://example.invalid/x.zip",
            "assets": [],
        }

        def slow_fetch(*args, **kwargs):
            time.sleep(2)
            return release

        result: dict[str, object] = {}

        def run_check() -> None:
            try:
                result["status"] = self.controller.check_for_updates(force=True)
            except Exception as exc:  # noqa: BLE001 - reported below
                result["error"] = exc

        with mock.patch.object(updater, "fetch_latest_release", slow_fetch):
            thread = threading.Thread(target=run_check)
            thread.start()
            try:
                time.sleep(0.3)
                self.assertTrue(self._lock_is_free_within(1.0))
            finally:
                thread.join(timeout=5)
        self.assertNotIn("error", result)
        self.assertTrue(result["status"]["checked"])

    def test_concurrent_downloads_serialize_without_corrupting(self):
        updater.write_update_state(
            self.controller._update_state_dir(),
            latest={
                "version": "9.9.9",
                "tag": "v9.9.9",
                "html_url": "",
                "zipball_url": "https://example.invalid/x.zip",
                "assets": [],
            },
        )
        calls: list[str] = []

        def slow_download(url, destination, **kwargs):
            calls.append("download")
            time.sleep(0.5)
            return Path(destination)

        results: list[object] = []

        def run_download() -> None:
            try:
                results.append(self.controller.download_update())
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                results.append(exc)

        with (
            mock.patch.object(updater, "download_release_zip", slow_download),
            mock.patch.object(updater, "stage_update", return_value={}),
        ):
            threads = [threading.Thread(target=run_download) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(len(results), 2)
        self.assertEqual([type(item) for item in results], [dict, dict], results)
        self.assertEqual(calls, ["download", "download"])


# ---------------------------------------------------------------------------
# L2: a corrupt image item must not discard the whole Gemini response
# ---------------------------------------------------------------------------


class ThumbnailImageRecoveryTests(unittest.TestCase):
    def test_corrupt_first_image_item_falls_through_to_the_good_one(self):
        import base64

        good = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
        payload = {
            "steps": [
                {
                    "content": [
                        {
                            "type": "image",
                            "image": {"data": "not-valid-base64!!!"},
                        },
                        {"type": "image", "image": {"data": good}},
                    ]
                }
            ]
        }
        self.assertEqual(_image_from_steps(payload), base64.b64decode(good))

    def test_all_corrupt_image_items_return_none(self):
        payload = {
            "steps": [
                {
                    "content": [
                        {
                            "type": "image",
                            "image": {"data": "not-valid-base64!!!"},
                        }
                    ]
                }
            ]
        }
        self.assertIsNone(_image_from_steps(payload))


if __name__ == "__main__":
    unittest.main()
