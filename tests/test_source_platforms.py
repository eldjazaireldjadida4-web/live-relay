from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt_dlp.extractor import gen_extractor_classes

from live_relay.source import build_pipe_command, resolve_source
from live_relay.source_platforms import source_platform_for, source_platform_label


class SourcePlatformTests(unittest.TestCase):
    def test_installed_ytdlp_supports_tiktok_live_urls(self):
        extractor = next(
            (item for item in gen_extractor_classes() if item.ie_key() == "TikTokLive"),
            None,
        )
        self.assertIsNotNone(extractor)
        self.assertTrue(extractor.suitable("https://www.tiktok.com/@creator/live"))

    def test_detects_tiktok_profile_live_url(self):
        self.assertEqual(
            source_platform_for("https://www.tiktok.com/@creator/live"), "tiktok"
        )

    def test_detects_tiktok_mobile_share_live_url(self):
        self.assertEqual(
            source_platform_for("https://m.tiktok.com/share/live/1234567890"),
            "tiktok",
        )

    def test_detects_youtube_and_custom_sources(self):
        self.assertEqual(
            source_platform_for("https://www.youtube.com/@creator/live"), "youtube"
        )
        self.assertEqual(source_platform_for("https://example.com/live"), "custom")

    def test_platform_labels_are_user_facing(self):
        self.assertEqual(source_platform_label("tiktok"), "TikTok LIVE")
        self.assertEqual(source_platform_label("youtube"), "YouTube LIVE")

    def test_rejects_non_live_tiktok_pages(self):
        with self.assertRaisesRegex(ValueError, "TikTok LIVE"):
            source_platform_for("https://www.tiktok.com/@creator/video/123")

    def test_rejects_tiktok_lookalike_hosts(self):
        self.assertEqual(
            source_platform_for("https://www.tiktok.com.example.org/@creator/live"),
            "custom",
        )

    def test_explicit_tiktok_mode_requires_a_live_url(self):
        with self.assertRaisesRegex(ValueError, "TikTok source must be a LIVE URL"):
            source_platform_for("https://www.tiktok.com/@creator/video/123", "tiktok")

    def test_resolves_tiktok_live_metadata_with_ytdlp(self):
        settings = SimpleNamespace(
            source_url="https://www.tiktok.com/@creator/live",
            source_platform="auto",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        payload = {
            "id": "1234567890",
            "title": "Live game",
            "live_status": "is_live",
            "is_live": True,
            "url": "https://cdn.example/live.flv",
            "http_headers": {"User-Agent": "TikTokTest"},
            "description": "Live description",
            "channel": "creator",
            "tags": ["games"],
        }
        result = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("live_relay.source.subprocess.run", return_value=result) as run:
            source = resolve_source(settings)

        self.assertEqual(source.platform, "tiktok")
        self.assertEqual(source.title, "Live game")
        self.assertEqual(source.urls, ("https://cdn.example/live.flv",))
        self.assertEqual(source.headers[0]["User-Agent"], "TikTokTest")
        self.assertEqual(source.tags, ("games",))
        self.assertIn("https://www.tiktok.com/@creator/live", run.call_args.args[0])

    def test_reads_portrait_dimensions_from_requested_video_format(self):
        settings = SimpleNamespace(
            source_url="https://www.tiktok.com/@creator/live",
            source_platform="auto",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        payload = {
            "live_status": "is_live",
            "requested_formats": [
                {
                    "url": "https://cdn.example/video.flv",
                    "vcodec": "h264",
                    "width": 1080,
                    "height": 1920,
                },
                {
                    "url": "https://cdn.example/audio.m4a",
                    "vcodec": "none",
                },
            ],
        }
        result = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("live_relay.source.subprocess.run", return_value=result):
            source = resolve_source(settings)

        self.assertEqual(source.width, 1080)
        self.assertEqual(source.height, 1920)

    def test_waits_when_tiktok_live_is_offline(self):
        settings = SimpleNamespace(
            source_url="https://www.tiktok.com/@creator/live",
            source_platform="tiktok",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        result = Mock(
            returncode=0,
            stdout=json.dumps({"live_status": "is_upcoming", "is_live": False}),
            stderr="",
        )
        with patch("live_relay.source.subprocess.run", return_value=result):
            self.assertIsNone(resolve_source(settings))

    def test_reports_tiktok_offline_or_login_restricted_as_waiting_state(self):
        settings = SimpleNamespace(
            source_url="https://www.tiktok.com/@creator/live",
            source_platform="tiktok",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        result = Mock(
            returncode=1,
            stdout="",
            stderr=(
                "ERROR: [tiktok:live] creator: "
                "The channel is not currently live\\n"
            ),
        )
        with (
            patch("live_relay.source.subprocess.run", return_value=result),
            self.assertLogs(level="INFO") as logs,
        ):
            self.assertIsNone(resolve_source(settings))

        message = " ".join(logs.output)
        self.assertIn("offline or require a signed-in session", message)
        self.assertIn("configure browser cookies", message)
        self.assertNotIn("source lookup failed", message)

    def test_resolver_and_pipe_use_browser_cookies(self):
        settings = SimpleNamespace(
            source_url="https://www.tiktok.com/@creator/live",
            source_platform="tiktok",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="chrome",
        )
        payload = {
            "live_status": "is_live",
            "is_live": True,
            "url": "https://cdn.example/live.flv",
        }
        result = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("live_relay.source.subprocess.run", return_value=result) as run:
            self.assertIsNotNone(resolve_source(settings))

        resolver_command = run.call_args.args[0]
        self.assertEqual(
            resolver_command[resolver_command.index("--cookies-from-browser") + 1],
            "chrome",
        )
        pipe_command = build_pipe_command(settings)
        self.assertEqual(
            pipe_command[pipe_command.index("--cookies-from-browser") + 1],
            "chrome",
        )

if __name__ == "__main__":
    unittest.main()
