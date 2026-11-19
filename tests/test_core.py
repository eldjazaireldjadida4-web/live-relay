from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests import RequestException

from live_relay.config import load_settings
from live_relay.destinations import (
    DestinationSession,
    FacebookDestination,
    YouTubeDestination,
    open_destinations,
    update_destination_metadata,
)
from live_relay.encoder import build_commands, redact_urls
from live_relay.metadata import StreamMetadata
from live_relay.quality import PROFILES, AdaptiveQuality
from live_relay.runtime import RelayStatus
from live_relay.service import (
    Progress,
    WorkerState,
    _branding_inputs,
    _destination_status,
    _format_exit_code,
    _transport_failure_reason,
    _update_worker_status,
    _teardown_worker,
    _is_transport_error,
    _parse_speed,
    _slowest_speed,
    _start_encoder,
    _worker_backoff,
    run,
)
from live_relay.source import clean_title, resolve_source


class BrandingFrameTests(unittest.TestCase):
    def test_builtin_oussama_frame_uses_its_logo_without_duplicate_watermark(self):
        settings = SimpleNamespace(
            frame_enabled=True,
            frame_file="",
            logo_enabled=True,
            logo_file="assets/live-relay-logo.png",
        )
        logo_path, frame_path = _branding_inputs(settings, portrait=False)
        expected_frame = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "live_relay"
            / "oussama-news-frame-landscape.png"
        )
        self.assertEqual(logo_path, "")
        self.assertEqual(frame_path, str(expected_frame))

    def test_custom_frame_keeps_the_selected_logo_watermark(self):
        settings = SimpleNamespace(
            frame_enabled=True,
            frame_file="custom-frame.png",
            logo_enabled=True,
            logo_file="custom-logo.png",
        )
        self.assertEqual(
            _branding_inputs(settings, portrait=True),
            ("custom-logo.png", "custom-frame.png"),
        )

    def test_official_logo_source_and_packaged_copy_match(self):
        project_root = Path(__file__).resolve().parents[1]
        source_logo = project_root / "assets" / "live-relay-logo.png"
        packaged_logo = project_root / "src" / "live_relay" / "stream-logo.png"
        self.assertEqual(source_logo.read_bytes(), packaged_logo.read_bytes())


class QualityTests(unittest.TestCase):
    def test_sustained_slowdown_lowers_profile(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(0.8, 0))
        self.assertIsNone(quality.observe(0.8, 30))
        self.assertEqual(quality.observe(0.8, 60), "lower")
        self.assertEqual(quality.profile.name, "480p")

    def test_long_stability_raises_profile_one_step(self):
        quality = AdaptiveQuality("360p")
        self.assertIsNone(quality.observe(1.2, 0))
        self.assertEqual(quality.observe(1.2, 600), "raise")
        self.assertEqual(quality.profile.name, "480p")

    def test_bad_youtube_health_lowers_profile_quickly(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(None, 0, "bad"))
        self.assertEqual(quality.observe(None, 30, "bad"), "lower")
        self.assertEqual(quality.profile.name, "480p")

    def test_good_health_with_fast_speed_keeps_profile(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(1.02, 0, "good"))
        self.assertIsNone(quality.observe(1.02, 2, "good"))
        self.assertEqual(quality.profile.name, "720p")

    def test_near_realtime_speed_does_not_trigger_quality_upgrade(self):
        quality = AdaptiveQuality("480p")
        self.assertIsNone(quality.observe(1.03, 0, "good"))
        self.assertIsNone(quality.observe(1.03, 600, "good"))
        self.assertEqual(quality.profile.name, "480p")

    def test_1080p_profile_is_top_of_ladder(self):
        names = [profile.name for profile in PROFILES]
        self.assertEqual(names, ["360p", "480p", "720p", "1080p"])
        rates = {profile.name: profile.video_bitrate for profile in PROFILES}
        self.assertEqual(
            rates,
            {
                "360p": "1000k",
                "480p": "2000k",
                "720p": "4000k",
                "1080p": "6000k",
            },
        )

    def test_unknown_speed_does_not_change_profile(self):
        quality = AdaptiveQuality("480p")
        self.assertIsNone(quality.observe(None, 100))
        self.assertEqual(quality.profile.name, "480p")


    def test_youtube_bad_health_lowers_profile_quickly(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(None, 0, "bad"))
        self.assertEqual(quality.observe(None, 30, "bad"), "lower")
        self.assertEqual(quality.profile.name, "480p")

    def test_good_health_with_normal_speed_keeps_profile(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(1.0, 0, "good"))
        self.assertEqual(quality.profile.name, "720p")

    def test_profiles_match_youtube_recommended_bitrates(self):
        by_name = {profile.name: profile for profile in PROFILES}
        self.assertEqual(by_name["360p"].video_bitrate, "1000k")
        self.assertEqual(by_name["480p"].video_bitrate, "2000k")
        self.assertEqual(by_name["720p"].video_bitrate, "4000k")
        self.assertEqual(by_name["1080p"].video_bitrate, "6000k")
        self.assertEqual(by_name["1080p"].width, 1920)
        self.assertEqual(by_name["1080p"].height, 1080)


class SourceAndEncoderTests(unittest.TestCase):
    def test_title_is_clean_and_limited_to_youtube_length(self):
        self.assertEqual(clean_title("  Live\nNow  "), "Live Now")
        self.assertEqual(len(clean_title("x" * 200)), 100)
        self.assertEqual(clean_title(""), "Live broadcast")

    def test_source_lookup_captures_metadata_for_generation(self):
        settings = SimpleNamespace(
            source_url="https://youtube.example/live",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        result = Mock(
            returncode=0,
            stderr="",
            stdout=(
                '{"title":"لعبة مباشرة","url":"https://cdn.example/live.m3u8",'
                '"live_status":"is_live","description":"وصف البث",'
                '"channel":"قناة الألعاب","categories":["Gaming"],'
                '"tags":["ألعاب","بث مباشر"],"width":1920,"height":1080}'
            ),
        )
        with patch("live_relay.source.subprocess.run", return_value=result):
            source = resolve_source(settings)
        self.assertEqual(source.title, "لعبة مباشرة")
        self.assertEqual(source.description, "وصف البث")
        self.assertEqual(source.channel, "قناة الألعاب")
        self.assertEqual(source.categories, ("Gaming",))
        self.assertEqual(source.tags, ("ألعاب", "بث مباشر"))
        self.assertEqual(source.width, 1920)
        self.assertEqual(source.height, 1080)

    def test_direct_and_pipe_encoder_inputs(self):
        direct = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest/key")],
            PROFILES[-1],
        )[0]
        piped = build_commands(
            "ffmpeg",
            (),
            [("facebook", "rtmps://ingest/key")],
            PROFILES[0],
            pipe_mode=True,
        )[0]
        self.assertIn("https://cdn.example/live.m3u8", direct)
        self.assertIn("pipe:0", piped)
        self.assertIn(
            f"{PROFILES[-1].width}:{PROFILES[-1].height}",
            direct[direct.index("-vf") + 1],
        )
        self.assertIn("640:360", piped[piped.index("-vf") + 1])
        local = build_commands(
            "ffmpeg",
            ("/tmp/source.mp4",),
            [("youtube", "rtmps://ingest/key")],
            PROFILES[0],
        )[0]
        self.assertNotIn("-user_agent", local)
        self.assertNotIn("-reconnect", local)

    def test_portrait_source_uses_vertical_output_dimensions(self):
        command = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest/key")],
            PROFILES[0],
            portrait=True,
        )[0]
        scale = command[command.index("-vf") + 1]
        self.assertIn("scale=360:640", scale)
        self.assertIn("pad=360:640", scale)

    def test_logo_overlay_uses_transparent_logo_and_selected_position(self):
        command = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest.example/key")],
            PROFILES[1],
            logo_path="assets/live-relay-logo.png",
            logo_position="top-left",
            logo_size_percent=15,
            logo_opacity=0.8,
        )[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("assets/live-relay-logo.png", command)
        margin = PROFILES[1].width * 2 // 100
        self.assertIn(f"overlay={margin}:{margin}", filter_graph)
        self.assertIn("colorchannelmixer=aa=0.800", filter_graph)
        self.assertIn("-map", command)
        self.assertIn("[v]", command)
        self.assertNotIn("-vf", command)

    def test_oussama_frame_overlay_uses_portrait_dimensions(self):
        frame_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "live_relay"
            / "oussama-news-frame-portrait.png"
        )
        command = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest.example/key")],
            PROFILES[0],
            portrait=True,
            frame_path=str(frame_path),
        )[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn(str(frame_path), command)
        self.assertIn("scale=360:640:flags=lanczos,format=rgba[frame]", filter_graph)
        self.assertIn("overlay=0:0:format=auto:shortest=1[framed]", filter_graph)
        self.assertIn("[framed]format=yuv420p[v]", filter_graph)
        self.assertNotIn("-vf", command)

    def test_frame_and_logo_share_one_compositing_graph(self):
        frame_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "live_relay"
            / "oussama-news-frame-landscape.png"
        )
        logo_path = Path(__file__).resolve().parents[1] / "assets" / "live-relay-logo.png"
        command = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest.example/key")],
            PROFILES[2],
            logo_path=str(logo_path),
            frame_path=str(frame_path),
        )[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("[1:v]scale=1280:720:flags=lanczos,format=rgba[frame]", filter_graph)
        self.assertIn("[2:v]scale=153:-1:flags=lanczos,format=rgba", filter_graph)
        self.assertIn("overlay=0:0:format=auto:shortest=1[framed]", filter_graph)
        self.assertIn("[framed][logo]overlay=", filter_graph)
        self.assertIn("[framed][logo]overlay=W-w-38:H-h-79", filter_graph)

    def test_frame_and_logo_compose_in_one_portrait_filter_graph(self):
        project_root = Path(__file__).resolve().parents[1]
        frame_path = project_root / "src" / "live_relay" / "oussama-news-frame-portrait.png"
        logo_path = project_root / "assets" / "live-relay-logo.png"
        command = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [("youtube", "rtmps://ingest.example/key")],
            PROFILES[2],
            logo_path=str(logo_path),
            logo_position="top-left",
            logo_size_percent=12,
            logo_opacity=0.9,
            portrait=True,
            frame_path=str(frame_path),
        )[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn(str(frame_path), command)
        self.assertIn("[1:v]scale=720:1280", filter_graph)
        self.assertIn("overlay=0:0", filter_graph)
        self.assertIn("[2:v]scale=86:-1", filter_graph)
        self.assertIn("[framed][logo]overlay=21:153", filter_graph)
        self.assertIn("format=yuv420p[v]", filter_graph)
        self.assertEqual(command.count("-map"), 2)

    def test_frame_path_must_exist(self):
        with self.assertRaisesRegex(ValueError, "Frame overlay image"):
            build_commands(
                "ffmpeg",
                ("https://cdn.example/live.m3u8",),
                [("youtube", "rtmps://ingest.example/key")],
                PROFILES[2],
                frame_path="missing-frame.png",
            )

    def test_logo_position_is_validated(self):
        with self.assertRaisesRegex(ValueError, "logo position"):
            build_commands(
                "ffmpeg",
                ("https://cdn.example/live.m3u8",),
                [("youtube", "rtmps://ingest.example/key")],
                PROFILES[1],
                logo_path="logo.png",
                logo_position="center",
            )

    def test_one_encoder_process_per_destination_without_tee(self):
        commands = build_commands(
            "ffmpeg",
            ("https://cdn.example/live.m3u8",),
            [
                ("youtube", "rtmps://youtube-ingest/key"),
                ("facebook", "rtmps://facebook-ingest/key"),
            ],
            PROFILES[1],
        )
        self.assertEqual(len(commands), 2)
        for command, (_, url) in zip(
            commands,
            [
                ("youtube", "rtmps://youtube-ingest/key"),
                ("facebook", "rtmps://facebook-ingest/key"),
            ],
        ):
            self.assertIn("-f", command)
            self.assertEqual(command[command.index("-f") + 1], "flv")
            self.assertIn(url, command)
            self.assertNotIn("tee", command)

    def test_header_injection_is_rejected(self):
        with self.assertRaises(ValueError):
            build_commands(
                "ffmpeg",
                ("https://cdn.example/live.m3u8",),
                [("youtube", "rtmps://ingest/key")],
                PROFILES[0],
                ({"Cookie": "safe\r\nInjected: value"},),
            )

    def test_logs_redact_http_and_rtmp_urls(self):
        message = redact_urls(
            "bad https://cdn.example/a?token=secret rtmps://ingest/key"
        )
        self.assertNotIn("secret", message)
        self.assertNotIn("ingest/key", message)
        self.assertEqual(message.count("[stream-url-hidden]"), 2)

    def test_source_lookup_logs_a_safe_diagnostic(self):
        settings = SimpleNamespace(
            source_url="https://youtube.example/live?token=private",
            format_selector="best",
            cookie_file="",
            cookies_from_browser="",
        )
        result = Mock(
            returncode=1,
            stderr="ERROR: Could not open https://cdn.example/live?key=private\n",
        )
        with (
            patch("live_relay.source.subprocess.run", return_value=result),
            self.assertLogs(level="INFO") as logs,
        ):
            self.assertIsNone(resolve_source(settings))
        message = " ".join(logs.output)
        self.assertIn("Could not open", message)
        self.assertNotIn("cdn.example", message)
        self.assertNotIn("private", message)
        self.assertIn("[stream-url-hidden]", message)


class HealthTests(unittest.TestCase):
    def test_ffmpeg_speed_parser(self):
        self.assertEqual(_parse_speed("1.25x"), 1.25)
        self.assertIsNone(_parse_speed("N/A"))

    def test_progress_watchdog_tracks_output_time(self):
        progress = Progress()
        progress.feed("out_time_ms=1000")
        self.assertEqual(progress.snapshot()["out_time_ms"], "1000")
        progress.feed("out_time_ms=1000")
        self.assertFalse(progress.stalled(5))
    def test_worker_backoff_grows_and_is_capped(self):
        self.assertEqual(_worker_backoff(1), 2.0)
        self.assertEqual(_worker_backoff(3), 8.0)
        self.assertEqual(_worker_backoff(9), 30.0)

    def test_failed_worker_restarts_alone_and_others_keep_streaming(self):
        stopped = threading.Event()
        settings = SimpleNamespace(
            stall_timeout=15,
            heartbeat=10,
            adaptive_quality=False,
            pipe_mode=False,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
        )
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        session = SimpleNamespace(
            targets=[("youtube", "rtmp://a.fake/y"), ("facebook", "rtmp://a.fake/f")],
            youtube=None,
            facebook=None,
            active_platforms=["youtube", "facebook"],
            failures={},
        )
        quality = AdaptiveQuality("720p")

        class FakeProcess:
            def __init__(self, index: int, code=None) -> None:
                self.index = index
                self.returncode = code
                self.stdout = None
                self.stderr = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        processes: dict[int, FakeProcess] = {}
        starts: list[int] = []

        def fake_start(worker, *_args, **_kwargs):
            starts.append(worker.index)
            code = 1 if (worker.index == 1 and starts.count(1) == 1) else None
            process = FakeProcess(worker.index, code)
            processes[worker.index] = process
            worker.process = process
            worker.started_at = time.monotonic()
            if starts.count(1) >= 2:
                stopped.set()
            return True

        teardown_calls: list[str] = []

        def fake_teardown(worker):
            if worker.process is not None and not stopped.is_set():
                teardown_calls.append(worker.name)
            _teardown_worker(worker)

        with (
            patch("live_relay.service._start_worker", side_effect=fake_start),
            patch("live_relay.service._teardown_worker", side_effect=fake_teardown),
        ):
            reason, _ = _start_encoder(
                "ffmpeg", source, session, quality, settings, stopped
            )

        self.assertEqual(reason, "stopped")
        self.assertEqual(starts, [0, 1, 1])
        self.assertEqual(teardown_calls.count("facebook"), 1)
        self.assertEqual(teardown_calls.count("youtube"), 0)

    def test_multi_destination_quality_uses_the_slowest_rate(self):
        first = Progress()
        second = Progress()
        first.feed("speed=1.4x")
        second.feed("speed=0.7x")
        self.assertEqual(_slowest_speed([first, second]), 0.7)
        self.assertIsNone(_slowest_speed([first, Progress()]))


class ServiceTests(unittest.TestCase):
    def test_unexpected_encoder_failure_closes_destination_events(self):
        settings = SimpleNamespace(
            ffmpeg_path="",
            initial_profile="720p",
            reconnect_delay=1,
        )
        source = SimpleNamespace(title="Demo", urls=(), headers=())
        session = Mock()
        with (
            patch("live_relay.service.resolve_ffmpeg", return_value="ffmpeg"),
            patch("live_relay.service.resolve_source", return_value=source),
            patch("live_relay.service.open_destinations", return_value=session),
            patch(
                "live_relay.service._start_encoder",
                side_effect=RuntimeError("encoder failed"),
            ),
            patch("live_relay.service.close_destinations") as close,
            self.assertRaisesRegex(RuntimeError, "encoder failed"),
        ):
            run(settings, threading.Event())
        close.assert_called_once_with(session)

    def test_metadata_is_generated_once_before_destination_creation(self):
        settings = SimpleNamespace(
            ffmpeg_path="",
            initial_profile="720p",
            reconnect_delay=1,
            source_retry=5,
            source_end_grace=60,
            metadata_api_enabled=True,
            youtube_category_id="22",
        )
        source = SimpleNamespace(
            title="Source title",
            description="Source description",
            channel="Source channel",
            categories=("Gaming",),
            tags=("Games",),
            urls=(),
            headers=(),
        )
        metadata = StreamMetadata("Improved title", "Description", ("tag",))
        session = Mock()

        def stop_after_start(
            ffmpeg,
            source,
            session,
            quality,
            settings,
            stopped,
            status=None,
            notifier=None,
        ):
            stopped.set()
            return "stopped", 1

        with (
            patch("live_relay.service.resolve_ffmpeg", return_value="ffmpeg"),
            patch("live_relay.service.resolve_source", return_value=source),
            patch(
                "live_relay.service.generate_metadata", return_value=metadata
            ) as generate,
            patch(
                "live_relay.service.open_destinations", return_value=session
            ) as open_destinations,
            patch("live_relay.service._start_encoder", side_effect=stop_after_start),
            patch("live_relay.service.close_destinations"),
        ):
            run(settings, threading.Event())

        generate.assert_called_once_with(settings, source)
        open_destinations.assert_called_once_with(settings, metadata, metadata)

    def test_manual_destination_skips_gemini_and_uses_source_metadata(self):
        settings = SimpleNamespace(
            ffmpeg_path="",
            initial_profile="720p",
            reconnect_delay=1,
            source_retry=5,
            source_end_grace=60,
            metadata_api_enabled=False,
            youtube_category_id="22",
        )
        source = SimpleNamespace(
            title="Source title",
            description="Source description",
            channel="Source channel",
            categories=("Gaming",),
            tags=("Games",),
            urls=(),
            headers=(),
        )
        metadata = StreamMetadata("Source title", "Source description", ("Games",))
        session = Mock()

        def stop_after_start(
            ffmpeg,
            source,
            session,
            quality,
            settings,
            stopped,
            status=None,
            notifier=None,
        ):
            stopped.set()
            return "stopped", 1

        with (
            patch("live_relay.service.resolve_ffmpeg", return_value="ffmpeg"),
            patch("live_relay.service.resolve_source", return_value=source),
            patch("live_relay.service.generate_metadata") as generate,
            patch(
                "live_relay.service.source_metadata", return_value=metadata
            ) as fallback,
            patch(
                "live_relay.service.open_destinations", return_value=session
            ) as open_destinations,
            patch("live_relay.service._start_encoder", side_effect=stop_after_start),
            patch("live_relay.service.close_destinations"),
        ):
            run(settings, threading.Event())

        generate.assert_not_called()
        fallback.assert_called_once_with(source, "22")
        open_destinations.assert_called_once_with(settings, metadata, metadata)


class DestinationTests(unittest.TestCase):
    def test_youtube_creates_autostart_broadcast_and_uses_rtmps(self):
        settings = SimpleNamespace(
            youtube_privacy="public", youtube_backup=False, state_dir=None
        )
        destination = YouTubeDestination(settings)
        destination.authenticate = Mock()
        destination._get_stream = Mock(
            return_value={
                "id": "stream-id",
                "cdn": {
                    "ingestionInfo": {
                        "streamName": "stream-key",
                        "rtmpsIngestionAddress": "rtmps://ingest.example/live",
                    }
                },
            }
        )
        destination.service = Mock()
        broadcast = destination.service.liveBroadcasts().insert.return_value.execute
        broadcast.return_value = {
            "id": "broadcast-id",
            "snippet": {
                "title": "Source title",
                "scheduledStartTime": "2030-01-01T00:00:00Z",
            },
        }

        targets = destination.start("Source title")

        body = destination.service.liveBroadcasts().insert.call_args.kwargs["body"]
        self.assertTrue(body["contentDetails"]["enableAutoStart"])
        self.assertFalse(body["contentDetails"]["enableAutoStop"])
        self.assertEqual(body["snippet"]["title"], "Source title")
        self.assertEqual(
            targets, [("youtube", "rtmps://ingest.example/live/stream-key")]
        )
        destination.authenticate.assert_called_once_with(allow_browser=False)
        destination.service.liveBroadcasts().bind.assert_called_once()

    def test_saved_thumbnail_is_applied_to_youtube_broadcast(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            thumbnail = state_dir / "youtube-thumbnail.png"
            thumbnail.write_bytes(b"png fixture")
            settings = SimpleNamespace(
                youtube_privacy="public",
                youtube_backup=False,
                state_dir=state_dir,
            )
            destination = YouTubeDestination(settings)
            destination.authenticate = Mock()
            destination._get_stream = Mock(
                return_value={
                    "id": "stream-id",
                    "cdn": {
                        "ingestionInfo": {
                            "streamName": "stream-key",
                            "rtmpsIngestionAddress": "rtmps://ingest.example/live",
                        }
                    },
                }
            )
            destination.service = Mock()
            destination.service.liveBroadcasts().insert.return_value.execute.return_value = {
                "id": "broadcast-id",
                "snippet": {},
            }

            destination.start("Source title")

            request = destination.service.thumbnails().set.call_args.kwargs
            self.assertEqual(request["videoId"], "broadcast-id")
            self.assertEqual(request["media_body"].mimetype(), "image/png")

    def test_youtube_ready_to_live_transition_runs_once(self):
        destination = YouTubeDestination(SimpleNamespace())
        destination.service = Mock()
        destination.broadcast = {"id": "broadcast-id"}

        self.assertTrue(destination.ensure_live())
        self.assertEqual(
            destination.service.liveBroadcasts().transition.call_count, 1
        )

    def test_manual_destination_metadata_marks_session_override(self):
        destination = Mock()
        session = DestinationSession(youtube=destination)
        metadata = StreamMetadata("Manual title", "Manual description", ("news",))

        results = update_destination_metadata(session, metadata, manual=True)

        self.assertEqual(results, {"youtube": "updated"})
        self.assertTrue(session.manual_metadata_override)
        self.assertIs(session.metadata, metadata)

    def test_youtube_token_is_saved_atomically_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            token_file = state_dir / "youtube-token.json"
            credentials = Mock()
            credentials.to_json.return_value = '{"access":"fixture"}'
            destination = YouTubeDestination(SimpleNamespace(state_dir=state_dir))

            destination._save_credentials(token_file, credentials)

            self.assertEqual(
                token_file.read_text(encoding="utf-8"), '{"access":"fixture"}'
            )
            self.assertFalse(any(path.suffix == ".tmp" for path in state_dir.iterdir()))
            if os.name != "nt":
                self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)

    def test_facebook_creates_live_now_from_source_title(self):
        settings = SimpleNamespace(
            facebook_graph_version="v26.0",
            facebook_target_id="me",
            facebook_access_token="test-token",
        )
        destination = FacebookDestination(settings)
        destination._post = Mock(
            return_value={
                "id": "video-id",
                "secure_stream_url": "rtmps://ingest.example/live/opaque-key",
            }
        )

        targets = destination.start("Source title")

        request = destination._post.call_args
        self.assertEqual(request.args[0], "me/live_videos")
        self.assertEqual(request.args[1]["status"], "LIVE_NOW")
        self.assertEqual(request.args[1]["title"], "Source title")
        self.assertEqual(targets[0][1], "rtmps://ingest.example/live/opaque-key")

    def test_youtube_updates_title_description_category_and_keywords(self):
        destination = YouTubeDestination(SimpleNamespace(youtube_category_id="25"))
        destination.service = Mock()
        destination.broadcast = {
            "id": "broadcast-id",
            "snippet": {"scheduledStartTime": "2030-01-01T00:00:00Z"},
        }
        updated = {"id": "broadcast-id", "snippet": {}}
        destination.service.liveBroadcasts().update.return_value.execute.return_value = updated
        metadata = StreamMetadata(
            title="عنوان محسّن",
            description="وصف مفيد",
            tags=("أخبار", "تحليل"),
            category_id="25",
            provider="gemini",
        )

        destination.update_metadata(metadata)

        broadcast_body = destination.service.liveBroadcasts().update.call_args.kwargs[
            "body"
        ]
        video_body = destination.service.videos().update.call_args.kwargs["body"]
        self.assertEqual(broadcast_body["snippet"]["title"], metadata.title)
        self.assertEqual(broadcast_body["snippet"]["description"], metadata.description)
        self.assertEqual(
            broadcast_body["snippet"]["scheduledStartTime"], "2030-01-01T00:00:00Z"
        )
        self.assertEqual(video_body["snippet"]["categoryId"], "25")
        self.assertEqual(video_body["snippet"]["tags"], ["أخبار", "تحليل"])

    def test_facebook_updates_title_and_description(self):
        destination = FacebookDestination(
            SimpleNamespace(
                facebook_graph_version="v26.0",
                facebook_target_id="me",
                facebook_access_token="test-token",
            )
        )
        destination.live_video_id = "video-id"
        destination._post = Mock(return_value={})
        metadata = StreamMetadata(
            title="عنوان محسّن",
            description="وصف مفيد",
            tags=("أخبار",),
            provider="gemini",
        )

        destination.update_metadata(metadata)

        video_id, body = destination._post.call_args.args
        self.assertEqual(video_id, "video-id")
        self.assertEqual(body["title"], metadata.title)
        self.assertEqual(body["description"], metadata.description)
        self.assertEqual(destination.title, metadata.title)

    def test_youtube_metadata_updates_broadcast_and_video_tags(self):
        destination = YouTubeDestination(SimpleNamespace(youtube_category_id="25"))
        destination.service = Mock()
        destination.broadcast = {
            "id": "broadcast-id",
            "snippet": {"scheduledStartTime": "2030-01-01T00:00:00Z"},
        }
        updated = {"id": "broadcast-id", "snippet": {"title": "الأخبار اليوم"}}
        destination.service.liveBroadcasts().update.return_value.execute.return_value = updated
        metadata = StreamMetadata(
            title="الأخبار اليوم",
            description="وصف موثوق للبث المباشر.",
            tags=("أخبار", "سياسة"),
            category_id="25",
            provider="gemini",
        )

        destination.update_metadata(metadata)

        broadcast = destination.service.liveBroadcasts().update.call_args.kwargs["body"]
        video = destination.service.videos().update.call_args.kwargs["body"]
        self.assertEqual(broadcast["snippet"]["description"], metadata.description)
        self.assertEqual(broadcast["snippet"]["categoryId"], "25")
        self.assertEqual(video["snippet"]["tags"], ["أخبار", "سياسة"])

    def test_facebook_metadata_updates_title_and_description(self):
        destination = FacebookDestination(
            SimpleNamespace(
                facebook_graph_version="v26.0",
                facebook_target_id="me",
                facebook_access_token="test-token",
            )
        )
        destination.live_video_id = "video-id"
        destination._post = Mock(return_value={"success": True})
        metadata = StreamMetadata(
            title="عنوان محسّن",
            description="وصف البث",
            tags=("كلمات",),
            category_id="22",
            provider="gemini",
        )

        destination.update_metadata(metadata)

        self.assertEqual(destination._post.call_args.args[0], "video-id")
        self.assertEqual(destination._post.call_args.args[1]["title"], "عنوان محسّن")
        self.assertEqual(destination._post.call_args.args[1]["description"], "وصف البث")

    @patch("live_relay.destinations.requests.get")
    def test_facebook_access_check_uses_bearer_token_and_returns_target_identity(
        self, get
    ):
        token = "dummy-test-token"
        settings = SimpleNamespace(
            facebook_graph_version="v26.0",
            facebook_target_id="123456789",
            facebook_access_token=token,
        )
        response = Mock(ok=True)
        response.json.return_value = {"id": "123456789", "name": "Test Page"}
        get.return_value = response

        result = FacebookDestination(settings).verify_access()

        self.assertEqual(result, {"id": "123456789", "name": "Test Page"})
        self.assertEqual(
            get.call_args.args[0], "https://graph.facebook.com/v26.0/123456789"
        )
        self.assertEqual(
            get.call_args.kwargs["headers"]["Authorization"], f"Bearer {token}"
        )
        self.assertNotIn(token, str(result))

    @patch("live_relay.destinations.requests.get")
    def test_facebook_access_check_redacts_token_from_api_errors(self, get):
        token = "dummy-test-token"
        settings = SimpleNamespace(
            facebook_graph_version="v26.0",
            facebook_target_id="me",
            facebook_access_token=token,
        )
        response = Mock(ok=False)
        response.json.return_value = {"error": {"message": f"Invalid token {token}"}}
        get.return_value = response

        with self.assertRaises(RuntimeError) as raised:
            FacebookDestination(settings).verify_access()

        self.assertNotIn(token, str(raised.exception))

    @patch("live_relay.destinations.requests.get")
    def test_facebook_access_check_hides_token_from_transport_errors(self, get):
        token = "dummy-test-token"
        settings = SimpleNamespace(
            facebook_graph_version="v26.0",
            facebook_target_id="me",
            facebook_access_token=token,
        )
        get.side_effect = RequestException(f"request failed with {token}")

        with self.assertRaises(RuntimeError) as raised:
            FacebookDestination(settings).verify_access()

        self.assertNotIn(token, str(raised.exception))

    @patch("live_relay.destinations.requests.post")
    def test_facebook_live_errors_hide_access_token(self, post):
        token = "dummy-live-token"
        settings = SimpleNamespace(
            facebook_graph_version="v26.0",
            facebook_access_token=token,
        )
        response = Mock(ok=False)
        response.json.return_value = {"error": {"message": f"Rejected token {token}"}}
        post.return_value = response

        with self.assertRaises(RuntimeError) as raised:
            FacebookDestination(settings)._post(
                "me/live_videos", {"access_token": token}
            )

        self.assertNotIn(token, str(raised.exception))

    def test_transport_errors_are_detected(self):
        self.assertTrue(_is_transport_error("RTMP server returned 403"))
        self.assertTrue(_is_transport_error("Broken pipe while writing"))
        self.assertTrue(_is_transport_error("Connection timed out"))
        self.assertFalse(_is_transport_error("frame=900 fps=30 speed=1.0x"))

    def test_youtube_access_check_returns_linked_channel_identity(self):
        settings = SimpleNamespace(
            youtube_client_secrets="client_secret.json",
            youtube_channel_id="UCioeMW7bZNeyV48Z09q2vTg",
            state_dir=None,
        )
        destination = YouTubeDestination(settings)
        destination._load_credentials = Mock(return_value=(Mock(), Mock(valid=True)))
        service = Mock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {"id": "UCioeMW7bZNeyV48Z09q2vTg", "snippet": {"title": "Test Channel"}}
            ]
        }
        destination._save_credentials = Mock()
        with patch("live_relay.destinations.build", return_value=service) as build:
            result = destination.verify_access()

        self.assertEqual(
            result, {"id": "UCioeMW7bZNeyV48Z09q2vTg", "name": "Test Channel"}
        )
        destination._load_credentials.assert_called_once_with(allow_browser=False)
        build.assert_called_once()
        destination._save_credentials.assert_called_once()

    def test_youtube_access_check_rejects_a_different_channel(self):
        settings = SimpleNamespace(
            youtube_client_secrets="client_secret.json",
            youtube_channel_id="UCioeMW7bZNeyV48Z09q2vTg",
            state_dir=None,
        )
        destination = YouTubeDestination(settings)
        destination._load_credentials = Mock(return_value=(Mock(), Mock(valid=True)))
        service = Mock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "UCDifferentChannel0000000",
                    "snippet": {"title": "Other Channel"},
                }
            ]
        }
        with (
            patch("live_relay.destinations.build", return_value=service),
            self.assertRaisesRegex(RuntimeError, "UCDifferentChannel0000000"),
        ):
            destination.verify_access()

    def test_youtube_access_check_requires_a_saved_authorization(self):
        settings = SimpleNamespace(
            youtube_client_secrets="client_secret.json",
            youtube_channel_id="UCioeMW7bZNeyV48Z09q2vTg",
            state_dir=None,
        )
        destination = YouTubeDestination(settings)
        destination._load_credentials = Mock(return_value=(Mock(), Mock(valid=False)))

        with self.assertRaisesRegex(RuntimeError, "ربط YouTube"):
            destination.verify_access()


class ConfigValidationTests(unittest.TestCase):
    def base_env(self, **overrides):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
        }
        env.update(overrides)
        return env

    def test_settings_load_with_static_keys(self):
        with patch.dict(os.environ, self.base_env(), clear=True):
            settings = load_settings()
        self.assertEqual(settings.platforms, ("youtube", "facebook"))
        self.assertEqual(settings.initial_profile, "720p")
        self.assertFalse(settings.metadata_api_enabled)

    def test_frame_is_disabled_by_default(self):
        with patch.dict(os.environ, self.base_env(), clear=True):
            settings = load_settings()
        self.assertFalse(settings.frame_enabled)
        self.assertEqual(settings.frame_file, "")

    def test_enabled_custom_frame_must_be_an_existing_png(self):
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "frame.png"
            frame_path.write_bytes(b"png")
            with patch.dict(
                os.environ,
                self.base_env(FRAME_ENABLED="true", FRAME_FILE=str(frame_path)),
                clear=True,
            ):
                settings = load_settings()
            self.assertTrue(settings.frame_enabled)
            self.assertEqual(settings.frame_file, str(frame_path.resolve()))

    def test_enabled_custom_frame_rejects_missing_or_non_png_files(self):
        with tempfile.TemporaryDirectory() as directory:
            frame_path = Path(directory) / "frame.jpg"
            frame_path.write_bytes(b"image")
            with (
                patch.dict(
                    os.environ,
                    self.base_env(FRAME_ENABLED="true", FRAME_FILE=str(frame_path)),
                    clear=True,
                ),
                self.assertRaisesRegex(ValueError, "FRAME_FILE"),
            ):
                load_settings()

    def test_facebook_api_enables_metadata_sync_without_youtube_api(self):
        with patch.dict(
            os.environ,
            self.base_env(
                LIVE_PLATFORMS="facebook",
                FACEBOOK_ACCESS_TOKEN="dummy-meta-token",
            ),
            clear=True,
        ):
            settings = load_settings()
        self.assertTrue(settings.metadata_api_enabled)

    def test_unknown_platform_is_rejected(self):
        with (
            patch.dict(os.environ, self.base_env(LIVE_PLATFORMS="tiktok"), clear=True),
            self.assertRaises(ValueError),
        ):
            load_settings()

    def test_cookie_file_and_browser_cookies_conflict(self):
        with (
            patch.dict(
                os.environ,
                self.base_env(
                    YTDLP_COOKIE_FILE="cookies.txt", YTDLP_COOKIES_FROM_BROWSER="chrome"
                ),
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            load_settings()

    def test_invalid_privacy_status_is_rejected(self):
        with (
            patch.dict(
                os.environ, self.base_env(YOUTUBE_PRIVACY_STATUS="secret"), clear=True
            ),
            self.assertRaises(ValueError),
        ):
            load_settings()

    def test_invalid_initial_profile_is_rejected(self):
        with (
            patch.dict(os.environ, self.base_env(INITIAL_PROFILE="1440p"), clear=True),
            self.assertRaises(ValueError),
        ):
            load_settings()

    def test_non_http_source_url_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                self.base_env(SOURCE_URL="ftp://example.com/live"),
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            load_settings()

    def test_extreme_timing_values_are_clamped(self):
        with patch.dict(
            os.environ,
            self.base_env(RECONNECT_DELAY="0", SOURCE_RETRY="1", STALL_TIMEOUT="1"),
            clear=True,
        ):
            settings = load_settings()
        self.assertGreaterEqual(settings.reconnect_delay, 1)
        self.assertGreaterEqual(settings.source_retry, 5)
        self.assertGreaterEqual(settings.stall_timeout, 15)

    def test_youtube_mode_rejects_unknown_value(self):
        env = {
            **os.environ,
            "SOURCE_URL": "https://example.com/live",
            "LIVE_PLATFORMS": "youtube",
            "YOUTUBE_MODE": "bogus",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            self.assertRaisesRegex(ValueError, "YOUTUBE_MODE"),
        ):
            load_settings()

    def test_static_mode_ignores_a_stale_oauth_path(self):
        env = self.base_env(
            LIVE_PLATFORMS="youtube",
            YOUTUBE_MODE="static",
            YOUTUBE_CLIENT_SECRETS_FILE="/missing/client-secret.json",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.effective_youtube_mode, "static")
        self.assertFalse(settings.youtube_api_enabled)

    def test_api_mode_does_not_fall_back_to_a_static_key(self):
        env = self.base_env(LIVE_PLATFORMS="youtube", YOUTUBE_MODE="api")
        with (
            patch.dict(os.environ, env, clear=True),
            self.assertRaisesRegex(ValueError, "YOUTUBE_MODE=api requires"),
        ):
            load_settings()

    def test_auto_mode_selects_api_when_a_valid_oauth_file_is_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            client_file = Path(directory) / "client-secret.json"
            client_file.write_text("{}", encoding="utf-8")
            env = self.base_env(
                LIVE_PLATFORMS="youtube",
                YOUTUBE_MODE="auto",
                YOUTUBE_CLIENT_SECRETS_FILE=str(client_file),
            )
            with patch.dict(os.environ, env, clear=True):
                settings = load_settings()
        self.assertEqual(settings.effective_youtube_mode, "api")
        self.assertTrue(settings.youtube_api_enabled)

    def test_invalid_boolean_setting_is_rejected(self):
        with (
            patch.dict(os.environ, self.base_env(YOUTUBE_BACKUP="maybe"), clear=True),
            self.assertRaisesRegex(ValueError, "YOUTUBE_BACKUP"),
        ):
            load_settings()

    def test_youtube_static_mode_overrides_linked_oauth(self):
        settings = SimpleNamespace(
            platforms=("youtube",),
            youtube_mode="static",
            youtube_client_secrets="client.json",
            youtube_static_key="demo-key",
            youtube_backup=True,
            facebook_access_token="",
            facebook_static_key="",
            facebook_target_id="me",
            facebook_graph_version="v26.0",
        )
        with patch("live_relay.destinations.YouTubeDestination") as api:
            session = open_destinations(settings, "Demo")
        api.assert_not_called()
        self.assertEqual(
            dict(session.targets),
            {
                "youtube": "rtmp://a.rtmp.youtube.com/live2/demo-key",
                "youtube-backup": "rtmp://b.rtmp.youtube.com/live2/demo-key?backup=1",
            },
        )

    def test_youtube_api_mode_is_used_when_explicitly_selected(self):
        settings = SimpleNamespace(
            platforms=("youtube",),
            youtube_mode="api",
            youtube_client_secrets="client.json",
            youtube_static_key="demo-key",
            facebook_access_token="",
            facebook_static_key="",
        )
        destination = Mock()
        destination.start.return_value = [("youtube", "rtmps://api.example/live")]
        with patch(
            "live_relay.destinations.YouTubeDestination", return_value=destination
        ) as api:
            session = open_destinations(settings, "Demo")
        api.assert_called_once_with(settings)
        self.assertEqual(session.targets, [("youtube", "rtmps://api.example/live")])

    def test_youtube_api_mode_does_not_fall_back_to_static_key(self):
        settings = SimpleNamespace(
            platforms=("youtube",),
            youtube_mode="api",
            youtube_client_secrets="",
            youtube_static_key="demo-key",
            facebook_access_token="",
            facebook_static_key="",
        )
        with self.assertRaisesRegex(RuntimeError, "No streaming destinations"):
            open_destinations(settings, "Demo")

    def test_partial_destination_status_names_active_and_failed_platforms(self):
        settings = SimpleNamespace(
            platforms=("youtube", "facebook"),
            youtube_mode="api",
            youtube_client_secrets="client.json",
            youtube_static_key="youtube-secret",
            youtube_backup=False,
            facebook_access_token="facebook-secret",
            facebook_static_key="",
            facebook_target_id="me",
            facebook_graph_version="v26.0",
        )
        youtube = Mock()
        youtube.start.side_effect = RuntimeError(
            "rejected youtube-secret facebook-secret"
        )
        facebook = Mock()
        facebook.start.return_value = [("facebook", "rtmps://ingest.example/live/key")]
        with (
            self.assertLogs(level="ERROR") as logs,
            patch("live_relay.destinations.YouTubeDestination", return_value=youtube),
            patch("live_relay.destinations.FacebookDestination", return_value=facebook),
        ):
            session = open_destinations(settings, "Demo")

        log_output = " ".join(logs.output)
        self.assertNotIn("youtube-secret", log_output)
        self.assertNotIn("facebook-secret", log_output)
        self.assertEqual(session.active_platforms, ["facebook"])
        self.assertEqual(set(session.failures), {"youtube"})
        self.assertNotIn("youtube-secret", session.failures["youtube"])
        self.assertNotIn("facebook-secret", session.failures["youtube"])
        mode, message = _destination_status(session)
        self.assertEqual(mode, "partial")
        self.assertIn("Facebook", message)
        self.assertIn("YouTube", message)


class CbrAndStarvationTests(unittest.TestCase):
    def test_cbr_signaling_present(self):
        profile = next(p for p in PROFILES if p.name == "720p")
        command = build_commands(
            "ffmpeg",
            ("https://example.com/live.m3u8",),
            [("youtube", "rtmp://example.com/app")],
            profile,
        )
        joined = " ".join(command[0])
        self.assertIn("nal-hrd=cbr", joined)
        self.assertIn("thread_queue_size", joined)
        self.assertIn("-max_interleave_delta 1M", joined)

    def test_pipe_mode_has_input_queue(self):
        profile = next(p for p in PROFILES if p.name == "720p")
        command = build_commands(
            "ffmpeg",
            (),
            [("youtube", "rtmp://example.com/app")],
            profile,
            pipe_mode=True,
        )
        self.assertIn("thread_queue_size", " ".join(command[0]))

    def test_quality_bad_health_counts_as_slow(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(1.0, 0.0, "bad"))
        self.assertEqual(quality.observe(1.0, 1.0, "bad"), "lower")

    def test_quality_no_data_health_is_unknown_not_slow(self):
        # YouTube's ``noData`` (normalised to ``no_data``) means YouTube has
        # no health information, not that the ingest is slow.
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(1.0, 0.0, "no_data"))
        self.assertIsNone(quality.observe(1.0, 1.0, "no_data"))
        self.assertIsNone(quality.observe(1.0, 2.0, "no_data"))


class WorkerDiagnosticsTests(unittest.TestCase):
    def test_facebook_failure_is_shown_without_hiding_youtube(self):
        session = SimpleNamespace(active_platforms=["youtube"], failures={})
        youtube = WorkerState(name="youtube", index=0, process=Mock())
        youtube.process.poll.return_value = None
        facebook = WorkerState(
            name="facebook",
            index=1,
            failure_reason=_transport_failure_reason("facebook"),
            exhausted=True,
        )
        status = RelayStatus()

        _update_worker_status(status, session, [youtube, facebook])
        snapshot = status.snapshot()

        self.assertEqual(snapshot["mode"], "partial")
        self.assertEqual(snapshot["active_platforms"], ["youtube"])
        self.assertIn("facebook", snapshot["destination_errors"])
        self.assertIn("TLS", snapshot["destination_errors"]["facebook"])

    def test_all_exhausted_destinations_show_error(self):
        session = SimpleNamespace(active_platforms=[], failures={})
        workers = [
            WorkerState(name="youtube", index=0, exhausted=True),
            WorkerState(name="facebook", index=1, exhausted=True),
        ]
        status = RelayStatus()

        _update_worker_status(status, session, workers)

        self.assertEqual(status.snapshot()["mode"], "error")

    def test_windows_unsigned_exit_code_has_signed_form(self):
        self.assertEqual(_format_exit_code(4294967291), "4294967291 (signed -5)")

    def test_tls_alert_is_detected_as_transport_failure(self):
        self.assertTrue(_is_transport_error("A TLS fatal alert has been received"))
        self.assertTrue(_is_transport_error("Error opening output file: I/O error"))


class ManualAuthTests(unittest.TestCase):
    def test_manual_auth_url_uses_local_redirect(self):
        with tempfile.TemporaryDirectory() as state_dir:
            client = Path(state_dir) / "client_secret.json"
            client.write_text(
                json.dumps({"installed": {"client_id": "id", "client_secret": "s",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token"}}),
                encoding="utf-8",
            )
            settings = SimpleNamespace(
                youtube_client_secrets=str(client),
                state_dir=Path(state_dir),
            )
            destination = YouTubeDestination(settings)
            url = destination.manual_auth_url()
            self.assertIn("redirect_uri=http%3A%2F%2Flocalhost", url)
            self.assertIn("access_type=offline", url)


    def test_manual_auth_exchange_without_url_creates_fresh_flow(self):
        with tempfile.TemporaryDirectory() as state_dir:
            settings = SimpleNamespace(
                youtube_client_secrets=str(Path(state_dir) / "missing.json"),
                state_dir=Path(state_dir),
            )
            destination = YouTubeDestination(settings)
            with self.assertRaises(RuntimeError) as context:
                destination.manual_auth_exchange("some-code")
            self.assertIn("does not exist", str(context.exception))


if __name__ == "__main__":
    unittest.main()
