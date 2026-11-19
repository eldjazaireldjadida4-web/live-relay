"""Regression tests for the 4.8.1 fix round.

- Manual YouTube auth URL was unreachable from the dashboard (500).
- Non-numeric timing settings could brick the next start.
- A failed Gemini thumbnail generation consumed the 10-minute throttle.
- The saved YouTube stream state was written non-atomically.
- The version drifted across pyproject.toml, __init__.py and README.md.
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from live_relay import __version__, thumbnail_ai
from live_relay.config import load_settings
from live_relay.dashboard import PROJECT_ROOT, DashboardController
from live_relay.destinations import YouTubeDestination

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_png(width: int = 1280, height: int = 720) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        crc = zlib.crc32(kind + content) & 0xFFFFFFFF
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\x00" + b"\x40\x80\xc0\xff" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


class VersionConsistencyTests(unittest.TestCase):
    def test_version_is_the_same_everywhere(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), __version__)
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"- {__version__}:", readme)


class NumericTimingTests(unittest.TestCase):
    def base_env(self, **overrides):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
        }
        env.update(overrides)
        return env

    def test_non_numeric_reconnect_delay_is_rejected_with_a_clear_message(self):
        with mock.patch.dict(
            os.environ, self.base_env(RECONNECT_DELAY="abc"), clear=True
        ):
            with self.assertRaisesRegex(ValueError, "whole numbers"):
                load_settings()

    def test_zero_timing_values_are_clamped_not_rejected(self):
        with mock.patch.dict(
            os.environ,
            self.base_env(RECONNECT_DELAY="0", SOURCE_RETRY="1", STALL_TIMEOUT="1"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.reconnect_delay, 1)
        self.assertEqual(settings.source_retry, 5)
        self.assertEqual(settings.stall_timeout, 15)


class DashboardSaveConfigTests(unittest.TestCase):
    def make_controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def test_save_config_rejects_non_numeric_timing_value(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-481-test-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "RECONNECT_DELAY"):
                    controller.save_config({"RECONNECT_DELAY": "abc"})

    def test_save_config_accepts_numeric_timing_value(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-481-test-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                controller.save_config({"RECONNECT_DELAY": "10"})

    def test_manual_auth_url_method_exists_and_returns_the_url(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-481-test-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            settings = SimpleNamespace(
                youtube_api_enabled=True, youtube_client_secrets="client.json"
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch(
                    "live_relay.dashboard.load_settings", return_value=settings
                ),
                mock.patch("live_relay.dashboard.YouTubeDestination") as dest_cls,
            ):
                dest_cls.return_value.manual_auth_url.return_value = (
                    "https://example.invalid/auth"
                )
                result = controller.youtube_manual_auth_url()
            self.assertEqual(
                result, {"authorization_url": "https://example.invalid/auth"}
            )


class ThumbnailThrottleTests(unittest.TestCase):
    def setUp(self):
        thumbnail_ai._last_generation.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.settings = SimpleNamespace(
            gemini_thumbnail_enabled=True,
            gemini_api_key="dummy-key",
            ffmpeg_path="ffmpeg",
            frame_file="",
        )
        self.source = SimpleNamespace(
            title="t", description="d", channel="c", categories=(), tags=()
        )

    def test_failed_generation_does_not_consume_the_throttle(self):
        with (
            mock.patch.object(
                thumbnail_ai, "generate_image_bytes", return_value=None
            ),
            mock.patch.object(thumbnail_ai, "_frame_path", return_value=None),
        ):
            self.assertEqual(
                thumbnail_ai.generate_auto_thumbnail(
                    self.settings, self.source, "t", self.state_dir
                ),
                "",
            )
        with (
            mock.patch.object(
                thumbnail_ai, "generate_image_bytes", return_value=make_png()
            ),
            mock.patch.object(thumbnail_ai, "_frame_path", return_value=None),
        ):
            self.assertEqual(
                thumbnail_ai.generate_auto_thumbnail(
                    self.settings, self.source, "t", self.state_dir
                ),
                "gemini",
            )

    def test_successful_generation_still_throttles_a_rapid_retry(self):
        with (
            mock.patch.object(
                thumbnail_ai, "generate_image_bytes", return_value=make_png()
            ),
            mock.patch.object(thumbnail_ai, "_frame_path", return_value=None),
        ):
            first = thumbnail_ai.generate_auto_thumbnail(
                self.settings, self.source, "t", self.state_dir
            )
            second = thumbnail_ai.generate_auto_thumbnail(
                self.settings, self.source, "t", self.state_dir
            )
        self.assertEqual(first, "gemini")
        self.assertEqual(second, "")


class GetStreamTests(unittest.TestCase):
    def make_destination(self, state_dir: Path, service: mock.Mock):
        settings = SimpleNamespace(state_dir=state_dir)
        destination = YouTubeDestination(settings)
        destination.service = service
        return destination

    def test_idle_saved_stream_is_reused_without_creating_a_new_one(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "youtube-stream.json").write_text(
                json.dumps({"stream_id": "saved-id"}), encoding="utf-8"
            )
            service = mock.Mock()
            service.liveStreams.return_value.list.return_value.execute.return_value = {
                "items": [
                    {"id": "saved-id", "status": {"streamStatus": "inactive"}}
                ]
            }
            destination = self.make_destination(state_dir, service)
            stream = destination._get_stream()
            self.assertEqual(stream["id"], "saved-id")
            service.liveStreams.return_value.insert.assert_not_called()

    def test_active_saved_stream_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "youtube-stream.json").write_text(
                json.dumps({"stream_id": "busy-id"}), encoding="utf-8"
            )
            service = mock.Mock()
            service.liveStreams.return_value.list.return_value.execute.return_value = {
                "items": [
                    {"id": "busy-id", "status": {"streamStatus": "active"}}
                ]
            }
            service.liveStreams.return_value.insert.return_value.execute.return_value = {
                "id": "fresh-id"
            }
            destination = self.make_destination(state_dir, service)
            stream = destination._get_stream()
            self.assertEqual(stream["id"], "fresh-id")
            saved = json.loads(
                (state_dir / "youtube-stream.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved, {"stream_id": "fresh-id"})
            leftovers = list(state_dir.glob(".youtube-stream.*.tmp"))
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
