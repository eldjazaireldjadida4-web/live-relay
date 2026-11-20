"""Regression tests for v4.8.6: TikTok as an output destination.

TikTok has no public API to create a live (its Live API is
invitation/partner-only), so TikTok is a static RTMP destination only:
the user creates the live on TikTok, copies the Server URL + stream key
for that session, and the relay pushes the same feed there alongside
YouTube and Facebook. The key is issued per session and expires, so the
failure message says so, and the dashboard explains how to get one.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_relay.config import load_settings
from live_relay.dashboard import PROJECT_ROOT, DashboardController
from live_relay.destinations import DestinationSession, open_destinations
from live_relay.metadata import StreamMetadata
from live_relay.service import (
    _destination_status,
    _transport_failure_reason,
)

METADATA = StreamMetadata("عنوان البث", "الوصف", ("وسم",))

TIKTOK_ENV = {
    "LIVE_PLATFORMS": "tiktok",
    "TIKTOK_RTMP_URL": "rtmp://rtmp-push.tiktok.com/live",
    "TIKTOK_STREAM_KEY": "tk-live-key-123",
}


def _static_settings(**overrides):
    """A Settings-shaped namespace for open_destinations, bypassing
    load_settings validation so failure branches can be exercised."""
    base = {
        "platforms": ("tiktok",),
        "youtube_mode": "static",
        "youtube_client_secrets": "",
        "youtube_static_key": "",
        "youtube_backup": False,
        "facebook_access_token": "",
        "facebook_static_key": "",
        "tiktok_rtmp_url": "rtmp://rtmp-push.tiktok.com/live",
        "tiktok_static_key": "tk-live-key-123",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TikTokDestinationTests(unittest.TestCase):
    def test_static_key_adds_a_tiktok_target(self):
        session = open_destinations(_static_settings(), METADATA)
        self.assertEqual(
            session.targets,
            [("tiktok", "rtmp://rtmp-push.tiktok.com/live/tk-live-key-123")],
        )
        self.assertEqual(session.active_platforms, ["tiktok"])
        self.assertEqual(session.failures, {})

    def test_custom_server_url_is_used_verbatim(self):
        settings = _static_settings(
            tiktok_rtmp_url="rtmp://rtmp-push-sg.tiktok.com/live/"
        )
        session = open_destinations(settings, METADATA)
        self.assertEqual(
            session.targets,
            [("tiktok", "rtmp://rtmp-push-sg.tiktok.com/live/tk-live-key-123")],
        )

    def test_missing_key_records_a_failure_but_keeps_other_destinations(self):
        settings = _static_settings(
            platforms=("youtube", "tiktok"),
            youtube_static_key="yt-key",
            tiktok_static_key="",
        )
        session = open_destinations(settings, METADATA)
        self.assertEqual(
            session.targets,
            [("youtube", "rtmp://a.rtmp.youtube.com/live2/yt-key")],
        )
        self.assertIn("tiktok", session.failures)

    def test_missing_url_records_a_failure(self):
        settings = _static_settings(tiktok_rtmp_url="")
        with self.assertRaisesRegex(RuntimeError, "No streaming destinations"):
            open_destinations(settings, METADATA)

    def test_tiktok_joins_youtube_and_facebook_targets(self):
        settings = _static_settings(
            platforms=("youtube", "facebook", "tiktok"),
            youtube_static_key="yt-key",
            facebook_static_key="fb-key",
        )
        session = open_destinations(settings, METADATA)
        names = [name for name, _ in session.targets]
        self.assertEqual(names, ["youtube", "facebook", "tiktok"])
        self.assertEqual(
            session.active_platforms, ["youtube", "facebook", "tiktok"]
        )


class TikTokConfigTests(unittest.TestCase):
    def test_tiktok_platform_is_accepted(self):
        with patch.dict(os.environ, TIKTOK_ENV, clear=True):
            settings = load_settings()
        self.assertEqual(settings.platforms, ("tiktok",))
        self.assertEqual(
            settings.tiktok_rtmp_url, "rtmp://rtmp-push.tiktok.com/live"
        )
        self.assertEqual(settings.tiktok_static_key, "tk-live-key-123")

    def test_missing_tiktok_key_is_rejected(self):
        env = dict(TIKTOK_ENV)
        del env["TIKTOK_STREAM_KEY"]
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "TIKTOK_STREAM_KEY"):
                load_settings()

    def test_bad_tiktok_url_scheme_is_rejected(self):
        env = dict(TIKTOK_ENV)
        env["TIKTOK_RTMP_URL"] = "https://example.com/live"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "TIKTOK_RTMP_URL"):
                load_settings()

    def test_unknown_platform_still_rejected(self):
        with patch.dict(
            os.environ, {"LIVE_PLATFORMS": "youtube,twitch"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "LIVE_PLATFORMS"):
                load_settings()


class TikTokStatusTests(unittest.TestCase):
    def test_failure_reason_points_at_the_per_session_key(self):
        reason = _transport_failure_reason("tiktok")
        self.assertIn("per", reason)
        self.assertIn("fresh", reason)

    def test_status_message_names_tiktok(self):
        session = DestinationSession(
            targets=[("tiktok", "rtmp://x/tk")],
            failures={},
        )
        mode, message = _destination_status(session, ["tiktok"])
        self.assertEqual(mode, "streaming")
        self.assertIn("TikTok", message)


class TikTokDashboardTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def test_public_config_exposes_tiktok_fields(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-tiktok-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                config = DashboardController(root).public_config()
            self.assertEqual(
                config["tiktok_rtmp_url"], "rtmp://rtmp-push.tiktok.com/live"
            )
            self.assertFalse(config["tiktok_static_key_set"])

    def test_save_config_round_trip_for_tiktok(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-tiktok-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                controller.save_config(
                    {
                        "LIVE_PLATFORMS": "youtube,tiktok",
                        "YOUTUBE_STREAM_KEY": "yt-key",
                        "TIKTOK_RTMP_URL": "rtmp://rtmp-push.tiktok.com/live",
                        "TIKTOK_STREAM_KEY": "tk-live-key-123",
                    }
                )
                config = controller.public_config()
            self.assertEqual(
                config["platforms"], ["youtube", "tiktok"]
            )
            self.assertTrue(config["tiktok_static_key_set"])
            self.assertEqual(config["missing"], [])
            self.assertTrue(
                any("TikTok" in note for note in config["notes"])
            )

    def test_save_config_rejects_a_non_rtmp_tiktok_url(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-tiktok-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                with self.assertRaisesRegex(ValueError, "TIKTOK_RTMP_URL"):
                    controller.save_config(
                        {"TIKTOK_RTMP_URL": "https://example.com/live"}
                    )

    def test_tiktok_without_a_key_is_reported_missing(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-tiktok-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                controller.save_config({"LIVE_PLATFORMS": "tiktok"})
                config = controller.public_config()
            self.assertTrue(
                any("TikTok" in item for item in config["missing"])
            )


if __name__ == "__main__":
    unittest.main()
