"""Regression tests for v4.8.7: automatic TikTok via a Restream bridge.

TikTok has no public API to create a live, so the manual per-session
key (4.8.6) needs a fresh key for every broadcast. The automatic route
is a generic Custom RTMP destination: Restream is a TikTok partner, so
one push to its permanent RTMP ingest lets Restream create the TikTok
live automatically — no per-session key, no manual step per broadcast.
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

CUSTOM_ENV = {
    "LIVE_PLATFORMS": "custom",
    "CUSTOM_RTMP_URL": "rtmp://live.restream.io/live",
    "CUSTOM_STREAM_KEY": "re_abc123",
    "CUSTOM_RTMP_NAME": "Restream",
}


def _static_settings(**overrides):
    """A Settings-shaped namespace for open_destinations, bypassing
    load_settings validation so failure branches can be exercised."""
    base = {
        "platforms": ("custom",),
        "youtube_mode": "static",
        "youtube_client_secrets": "",
        "youtube_static_key": "",
        "youtube_backup": False,
        "facebook_access_token": "",
        "facebook_static_key": "",
        "tiktok_rtmp_url": "rtmp://rtmp-push.tiktok.com/live",
        "tiktok_static_key": "",
        "custom_rtmp_url": "rtmp://live.restream.io/live",
        "custom_stream_key": "re_abc123",
        "custom_rtmp_name": "Restream",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class CustomDestinationTests(unittest.TestCase):
    def test_permanent_key_adds_a_custom_target(self):
        session = open_destinations(_static_settings(), METADATA)
        self.assertEqual(
            session.targets,
            [("custom", "rtmp://live.restream.io/live/re_abc123")],
        )
        self.assertEqual(session.active_platforms, ["custom"])
        self.assertEqual(session.failures, {})

    def test_custom_joins_youtube_facebook_and_tiktok_targets(self):
        settings = _static_settings(
            platforms=("youtube", "facebook", "tiktok", "custom"),
            youtube_static_key="yt-key",
            facebook_static_key="fb-key",
            tiktok_static_key="tk-key",
        )
        session = open_destinations(settings, METADATA)
        names = [name for name, _ in session.targets]
        self.assertEqual(names, ["youtube", "facebook", "tiktok", "custom"])

    def test_missing_key_records_a_failure_but_keeps_other_destinations(self):
        settings = _static_settings(
            platforms=("youtube", "custom"),
            youtube_static_key="yt-key",
            custom_stream_key="",
        )
        session = open_destinations(settings, METADATA)
        self.assertEqual(
            session.targets,
            [("youtube", "rtmp://a.rtmp.youtube.com/live2/yt-key")],
        )
        self.assertIn("custom", session.failures)

    def test_missing_url_records_a_failure(self):
        settings = _static_settings(custom_rtmp_url="")
        with self.assertRaisesRegex(RuntimeError, "No streaming destinations"):
            open_destinations(settings, METADATA)


class CustomConfigTests(unittest.TestCase):
    def test_custom_platform_is_accepted(self):
        with patch.dict(os.environ, CUSTOM_ENV, clear=True):
            settings = load_settings()
        self.assertEqual(settings.platforms, ("custom",))
        self.assertEqual(
            settings.custom_rtmp_url, "rtmp://live.restream.io/live"
        )
        self.assertEqual(settings.custom_stream_key, "re_abc123")
        self.assertEqual(settings.custom_rtmp_name, "Restream")

    def test_missing_custom_key_is_rejected(self):
        env = dict(CUSTOM_ENV)
        del env["CUSTOM_STREAM_KEY"]
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "CUSTOM_STREAM_KEY"):
                load_settings()

    def test_bad_custom_url_scheme_is_rejected(self):
        env = dict(CUSTOM_ENV)
        env["CUSTOM_RTMP_URL"] = "https://example.com/live"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "CUSTOM_RTMP_URL"):
                load_settings()

    def test_default_restream_url_applies_when_unset(self):
        env = dict(CUSTOM_ENV)
        del env["CUSTOM_RTMP_URL"]
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(
            settings.custom_rtmp_url, "rtmp://live.restream.io/live"
        )


class CustomStatusTests(unittest.TestCase):
    def test_failure_reason_points_at_restream(self):
        reason = _transport_failure_reason("custom")
        self.assertIn("Restream", reason)

    def test_status_message_names_the_custom_destination(self):
        session = DestinationSession(
            targets=[("custom", "rtmp://live.restream.io/live/re_abc123")],
            failures={},
        )
        mode, message = _destination_status(session, ["custom"])
        self.assertEqual(mode, "streaming")
        self.assertIn("RTMP", message)


class CustomDashboardTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def test_public_config_exposes_custom_fields(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-custom-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                config = DashboardController(root).public_config()
            self.assertEqual(
                config["custom_rtmp_url"], "rtmp://live.restream.io/live"
            )
            self.assertEqual(config["custom_rtmp_name"], "Restream")
            self.assertFalse(config["custom_stream_key_set"])

    def test_save_config_round_trip_for_custom(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-custom-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                controller.save_config(
                    {
                        "LIVE_PLATFORMS": "youtube,custom",
                        "YOUTUBE_STREAM_KEY": "yt-key",
                        "CUSTOM_RTMP_URL": "rtmp://live.restream.io/live",
                        "CUSTOM_RTMP_NAME": "Restream",
                        "CUSTOM_STREAM_KEY": "re_abc123",
                    }
                )
                config = controller.public_config()
            self.assertEqual(config["platforms"], ["youtube", "custom"])
            self.assertTrue(config["custom_stream_key_set"])
            self.assertEqual(config["missing"], [])
            self.assertTrue(
                any("Restream" in note for note in config["notes"])
            )

    def test_save_config_rejects_a_non_rtmp_custom_url(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-custom-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                with self.assertRaisesRegex(ValueError, "CUSTOM_RTMP_URL"):
                    controller.save_config(
                        {"CUSTOM_RTMP_URL": "https://example.com/live"}
                    )

    def test_custom_without_a_key_is_reported_missing(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-custom-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                controller.save_config({"LIVE_PLATFORMS": "custom"})
                config = controller.public_config()
            self.assertTrue(
                any("Restream" in item for item in config["missing"])
            )


if __name__ == "__main__":
    unittest.main()
