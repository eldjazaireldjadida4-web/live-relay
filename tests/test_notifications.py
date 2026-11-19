import os
import tempfile
import unittest
from unittest import mock

from live_relay.config import load_settings
from live_relay.encoder import build_commands
from live_relay.notifier import Notifier, recording_file_path
from live_relay.quality import PROFILES


class NotifierTests(unittest.TestCase):
    def test_disabled_notifier_sends_nothing(self):
        notifier = Notifier()
        with mock.patch("live_relay.notifier.threading.Thread") as thread:
            notifier.notify("session_started", title="Test")
        thread.assert_not_called()

    def test_notify_spawns_one_daemon_thread(self):
        notifier = Notifier(telegram_token="token", telegram_chat="123")
        with mock.patch("live_relay.notifier.threading.Thread") as thread:
            notifier.notify("session_started", title="Test")
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])

    def test_telegram_payload_is_arabic_and_structured(self):
        notifier = Notifier(
            telegram_token="token",
            telegram_chat="123",
            language="ar",
        )
        payload = notifier._telegram_payload(
            "session_started",
            {"title": "عنوان", "platforms": "youtube", "quality": "720p"},
        )
        self.assertEqual(payload["chat_id"], "123")
        self.assertIn("🔴", payload["text"])
        self.assertIn("عنوان", payload["text"])
        self.assertIn("youtube", payload["text"])

    def test_telegram_payload_formats_duration(self):
        notifier = Notifier(telegram_token="token", telegram_chat="123")
        payload = notifier._telegram_payload(
            "relay_ended", {"duration": 3725}
        )
        self.assertIn("1h 02m 05s", payload["text"])

    def test_webhook_payload_contains_event_and_timestamp(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        payload = notifier._webhook_payload(
            "destination_failed",
            {"platform": "youtube", "reason": "timeout"},
        )
        self.assertEqual(payload["event"], "destination_failed")
        self.assertEqual(payload["platform"], "youtube")
        self.assertIn("timestamp", payload)

    def test_empty_fields_are_dropped(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        payload = notifier._webhook_payload("relay_ended", {"title": "", "duration": None})
        self.assertNotIn("title", payload)
        self.assertNotIn("duration", payload)

    def test_delivery_posts_to_both_channels(self):
        notifier = Notifier(
            telegram_token="token",
            telegram_chat="123",
            webhook_url="https://example.com/hook",
        )
        with mock.patch.object(Notifier, "_post_json") as post:
            notifier._deliver("relay_ended", {"duration": 60})
        self.assertEqual(post.call_count, 2)

    def test_delivery_swallows_network_errors(self):
        notifier = Notifier(webhook_url="https://example.com/hook")
        with mock.patch.object(
            Notifier, "_post_json", side_effect=OSError("down")
        ):
            notifier._deliver("relay_ended", {})


class RecordingPathTests(unittest.TestCase):
    def test_creates_directory_and_names_file(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = recording_file_path(state_dir, "YouTube")
            self.assertTrue(os.path.isdir(os.path.join(state_dir, "recordings")))
            self.assertTrue(path.endswith(".mp4"))
            filename = os.path.basename(path)
            slug = filename.split("-", 2)[-1]
            self.assertEqual(slug, "youtube.mp4")

    def test_weird_platform_names_become_slugs(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = recording_file_path(state_dir, "Facebook Page!!")
            self.assertIn("facebook-page", path)

    def test_fallback_slug_for_blank_name(self):
        with tempfile.TemporaryDirectory() as state_dir:
            path = recording_file_path(state_dir, "   ")
            self.assertIn("-relay.mp4", path)


class RecordingCommandTests(unittest.TestCase):
    def test_recording_output_added_to_first_command_only(self):
        profile = PROFILES[1]
        with tempfile.TemporaryDirectory() as rec_dir:
            commands = build_commands(
                "ffmpeg",
                ("https://example.com/stream",),
                [("youtube", "rtmp://a/key"), ("facebook", "rtmp://b/key")],
                profile,
                recording_path=os.path.join(rec_dir, "rec.mp4"),
            )
        self.assertEqual(len(commands), 2)
        first = " ".join(commands[0])
        second = " ".join(commands[1])
        self.assertIn("rec.mp4", first)
        self.assertIn("mp4", first)
        self.assertIn("frag_keyframe", first)
        self.assertNotIn("rec.mp4", second)

    def test_no_recording_output_by_default(self):
        profile = PROFILES[1]
        commands = build_commands(
            "ffmpeg",
            ("https://example.com/stream",),
            [("youtube", "rtmp://a/key")],
            profile,
        )
        self.assertNotIn("mp4", " ".join(commands[0]))

    def test_recording_path_must_be_mp4(self):
        profile = PROFILES[1]
        with self.assertRaises(ValueError):
            build_commands(
                "ffmpeg",
                ("https://example.com/stream",),
                [("youtube", "rtmp://a/key")],
                profile,
                recording_path="/tmp/rec/session.avi",
            )

    def test_recording_directory_must_exist(self):
        profile = PROFILES[1]
        with self.assertRaises(ValueError):
            build_commands(
                "ffmpeg",
                ("https://example.com/stream",),
                [("youtube", "rtmp://a/key")],
                profile,
                recording_path="/no/such/dir/session.mp4",
            )


class NotificationConfigTests(unittest.TestCase):
    def test_telegram_token_requires_chat(self):
        env = {
            "LIVE_PLATFORMS": "youtube",
            "YOUTUBE_STREAM_KEY": "key",
            "YOUTUBE_CLIENT_SECRETS_FILE": "",
            "YOUTUBE_MODE": "static",
            "NOTIFY_TELEGRAM_TOKEN": "secret-token",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                load_settings()

    def test_webhook_must_be_http(self):
        env = {
            "LIVE_PLATFORMS": "youtube",
            "YOUTUBE_STREAM_KEY": "key",
            "YOUTUBE_MODE": "static",
            "NOTIFY_WEBHOOK_URL": "ftp://example.com/hook",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                load_settings()

    def test_valid_notification_config_loads(self):
        env = {
            "LIVE_PLATFORMS": "youtube",
            "YOUTUBE_STREAM_KEY": "key",
            "YOUTUBE_MODE": "static",
            "NOTIFY_TELEGRAM_TOKEN": "secret-token",
            "NOTIFY_TELEGRAM_CHAT": "12345",
            "NOTIFY_WEBHOOK_URL": "https://example.com/hook",
            "NOTIFY_LANGUAGE": "en",
            "RECORD_ENABLED": "1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.notify_telegram_token, "secret-token")
        self.assertEqual(settings.notify_telegram_chat, "12345")
        self.assertEqual(settings.notify_language, "en")
        self.assertTrue(settings.record_enabled)


if __name__ == "__main__":
    unittest.main()
