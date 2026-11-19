"""Regression tests for the 4.8.4 audit round.

Covers: YouTube health/starvation detection (H1), dashboard surviving bad
logo values (H2), the .env.example stream-key line (H3), atomic and verified
update applies (H4/H5/H8), failed update checks keeping the known latest
release (H9), dependency re-check after updates (H10, launchers only),
save/start validation parity (M4/M29), the 2 MB thumbnail limit (M18),
FRAME_ENABLED on AI thumbnails (M24), worker startup failures staying
local (M26), executable bits surviving updates (M27), and four-part /
pre-release version tags (M21).
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests
from dotenv import dotenv_values

from live_relay import dashboard as dashboard_module
from live_relay import updater
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class
from live_relay.destinations import DestinationSession, YouTubeDestination
from live_relay.quality import AdaptiveQuality
from live_relay.service import WorkerState, _start_worker
from live_relay.thumbnail_ai import _frame_path
from live_relay.thumbnails import save_thumbnail


def _zip_bytes(files: dict[str, str | bytes], top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            arcname = f"{top}/{name}" if top else name
            data = content.encode("utf-8") if isinstance(content, str) else content
            archive.writestr(arcname, data)
    return buffer.getvalue()


def _zip_bytes_with_mode(
    files: dict[str, tuple[str | bytes, int]], top: str | None = None
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, (content, mode) in files.items():
            arcname = f"{top}/{name}" if top else name
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(arcname)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, data: bytes, headers=None):
        self._data = io.BytesIO(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._data.read(size)


class YouTubeHealthTests(unittest.TestCase):
    def _destination_with_status(self, stream_status: str, health: str):
        destination = YouTubeDestination(SimpleNamespace())
        service = mock.Mock()
        service.liveBroadcasts.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"lifeCycleStatus": "live"}}]
        }
        service.liveStreams.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {
                        "streamStatus": stream_status,
                        "healthStatus": {"status": health},
                    }
                }
            ]
        }
        destination.service = service
        destination.broadcast = {"id": "b1"}
        destination.stream = {"id": "s1"}
        return destination

    def test_check_status_normalises_no_data(self):
        destination = self._destination_with_status("active", "noData")
        status = destination.check_status()
        self.assertEqual(status["stream_health"], "no_data")
        self.assertEqual(status["stream_status"], "active")

    def test_check_status_keeps_real_health_values(self):
        for health in ("good", "ok", "bad"):
            destination = self._destination_with_status("active", health)
            self.assertEqual(destination.check_status()["stream_health"], health)

    def test_unknown_health_is_not_slow_for_adaptive_quality(self):
        quality = AdaptiveQuality("720p")
        self.assertIsNone(quality.observe(1.0, 0.0, "no_data"))
        self.assertIsNone(quality.observe(1.0, 1.0, "no_data"))
        self.assertIsNone(quality.observe(1.0, 2.0, "no_data"))
        # "bad" still counts as slow, but needs two slow heartbeats.
        self.assertIsNone(quality.observe(1.0, 3.0, "bad"))
        self.assertEqual(quality.observe(1.0, 4.0, "bad"), "lower")


class DashboardBadValuesTests(unittest.TestCase):
    def _controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def test_public_config_survives_bad_logo_values(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                "LOGO_SIZE_PERCENT=abc\nLOGO_OPACITY=nan\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                config = DashboardController(root).public_config()
            # No exception, JSON-safe values, defaults used, problem visible.
            self.assertEqual(config["logo_size_percent"], 12)
            self.assertEqual(config["logo_opacity"], 0.9)
            json.dumps(config)  # must not raise (NaN is not valid JSON)
            self.assertTrue(
                any("LOGO_SIZE_PERCENT" in note for note in config["missing"])
            )
            self.assertTrue(
                any("LOGO_OPACITY" in note for note in config["missing"])
            )

    def test_config_endpoint_answers_200_with_bad_logo_values(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-http-484-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                f"LIVE_RELAY_STATE_DIR={root / 'state'}\n"
                "LOGO_SIZE_PERCENT=abc\nLOGO_OPACITY=nan\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
            html_path = Path(dashboard_module.__file__).with_name("dashboard.html")
            handler = build_handler_class(controller, html_path)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = requests.get(
                    f"http://127.0.0.1:{port}/api/config",
                    headers={"X-Relay-Token": controller.control_token},
                    timeout=5,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["logo_size_percent"], 12)


class EnvExampleTests(unittest.TestCase):
    def test_stream_key_line_is_empty_with_a_real_comment(self):
        values = dotenv_values(PROJECT_ROOT / ".env.example")
        self.assertEqual(values.get("YOUTUBE_STREAM_KEY"), "")
        lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        key_line = next(i for i, line in enumerate(lines) if line.startswith("YOUTUBE_STREAM_KEY="))
        self.assertEqual(lines[key_line], "YOUTUBE_STREAM_KEY=")
        self.assertTrue(lines[key_line - 1].startswith("#"))


class SaveConfigParityTests(unittest.TestCase):
    def _controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def test_cookie_sources_conflict_is_rejected_at_save(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            controller = self._controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "not both"):
                    controller.save_config(
                        {
                            "YTDLP_COOKIE_FILE": "/tmp/cookies.txt",
                            "YTDLP_COOKIES_FROM_BROWSER": "chrome",
                        }
                    )

    def test_facebook_target_name_is_rejected_at_save(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            controller = self._controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "FACEBOOK_TARGET_ID"):
                    controller.save_config(
                        {
                            "FACEBOOK_ACCESS_TOKEN": "EAAtest",
                            "FACEBOOK_TARGET_ID": "My Page Name",
                        }
                    )

    def test_missing_logo_file_is_rejected_at_save(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            controller = self._controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "LOGO_FILE"):
                    controller.save_config(
                        {
                            "LOGO_ENABLED": "true",
                            "LOGO_FILE": "/nonexistent/logo.png",
                        }
                    )

    def test_garbage_booleans_are_rejected_at_save(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            controller = self._controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                for key in (
                    "ADAPTIVE_QUALITY",
                    "PIPE_MODE",
                    "YOUTUBE_BACKUP",
                    "CHECK_FOR_UPDATES",
                ):
                    with self.assertRaisesRegex(
                        ValueError, f"{key} must be a boolean value"
                    ):
                        controller.save_config({key: "banana"})


class UpdaterApplyTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, Path]:
        project = root / "project"
        project.mkdir()
        (project / ".env").write_text("SOURCE_URL=https://example.invalid\n")
        script = project / "start_live_relay.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o644)
        return project, root / "state"

    def _stage(
        self, state_dir: Path, version: str, data: bytes, filename: str = "update.zip"
    ) -> Path:
        zip_path = state_dir.parent / filename
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(data)
        updater.stage_update(
            state_dir,
            {"version": version, "tag": f"v{version}", "html_url": ""},
            zip_path,
        )
        return zip_path

    def test_empty_zip_is_rejected_and_stays_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            project, state_dir = self._project(Path(directory))
            self._stage(state_dir, "9.9.9", _zip_bytes({}))
            with self.assertRaisesRegex(ValueError, "does not contain"):
                updater.apply_pending_update(project, state_dir)
            state = updater.read_update_state(state_dir)
            self.assertIn("staged", state)
            self.assertNotIn("last_applied_version", state)

    def test_mixed_layout_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project, state_dir = self._project(Path(directory))
            data = _zip_bytes(
                {
                    "README.md": "stray root file",
                    "src/live_relay/__init__.py": '__version__ = "9.9.9"\n',
                },
                top="live-relay-9.9.9",
            )
            # Add a root-level file to break the single top-level prefix.
            buffer = io.BytesIO(data)
            with zipfile.ZipFile(buffer, "a") as archive:
                archive.writestr("README.md", "stray root file")
            self._stage(state_dir, "9.9.9", buffer.getvalue())
            with self.assertRaisesRegex(ValueError, "does not contain"):
                updater.apply_pending_update(project, state_dir)

    def test_version_mismatch_is_rejected_and_stays_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            project, state_dir = self._project(Path(directory))
            data = _zip_bytes(
                {"src/live_relay/__init__.py": '__version__ = "9.9.8"\n'},
                top="live-relay-9.9.9",
            )
            self._stage(state_dir, "9.9.9", data)
            with self.assertRaisesRegex(ValueError, "not the staged"):
                updater.apply_pending_update(project, state_dir)
            self.assertIn("staged", updater.read_update_state(state_dir))

    def test_tampered_staged_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project, state_dir = self._project(Path(directory))
            data = _zip_bytes(
                {"src/live_relay/__init__.py": '__version__ = "9.9.9"\n'},
                top="live-relay-9.9.9",
            )
            zip_path = self._stage(state_dir, "9.9.9", data)
            with open(zip_path, "ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "changed size|damaged"):
                updater.apply_pending_update(project, state_dir)
            self.assertIn("staged", updater.read_update_state(state_dir))

    def test_valid_update_applies_preserves_env_and_exec_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            project, state_dir = self._project(Path(directory))
            data = _zip_bytes_with_mode(
                {
                    "src/live_relay/__init__.py": (
                        '__version__ = "9.9.9"\n',
                        0o644,
                    ),
                    "start_live_relay.sh": ("#!/bin/sh\necho new\n", 0o755),
                },
                top="live-relay-9.9.9",
            )
            self._stage(state_dir, "9.9.9", data)
            result = updater.apply_pending_update(project, state_dir)
            self.assertEqual(result, "9.9.9")
            self.assertEqual(
                (project / ".env").read_text(encoding="utf-8"),
                "SOURCE_URL=https://example.invalid\n",
            )
            mode = stat.S_IMODE((project / "start_live_relay.sh").stat().st_mode)
            self.assertTrue(mode & 0o111, f"exec bit lost, mode is {oct(mode)}")
            state = updater.read_update_state(state_dir)
            self.assertNotIn("staged", state)
            self.assertEqual(state["last_applied_version"], "9.9.9")

    def test_corrupt_crc_download_is_rejected_and_file_removed(self):
        data = bytearray(_zip_bytes({"a.txt": "hello world, hello zip"}))
        index = bytes(data).find(b"hello world")
        self.assertNotEqual(index, -1)
        data[index] ^= 0xFF  # break the CRC without breaking the zip structure
        with mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(bytes(data), {"Content-Length": str(len(data))}),
        ):
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "update.zip"
                with self.assertRaisesRegex(ValueError, "corrupt"):
                    updater.download_release_zip(
                        "https://example.invalid/x.zip", dest
                    )
                self.assertFalse(dest.exists())


class UpdateCheckTests(unittest.TestCase):
    def test_failed_check_keeps_known_latest_and_throttle(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-484-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                f"LIVE_RELAY_STATE_DIR={root / 'state'}\n"
                "UPDATE_REPO=eldjazaireldjadida4-web/live-relay\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
            state_dir = root / "state"
            updater.write_update_state(
                state_dir,
                last_check=1000.0,
                latest={"version": "9.9.9", "tag": "v9.9.9"},
            )
            with mock.patch(
                "live_relay.updater.fetch_latest_release", return_value=None
            ):
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = controller.check_for_updates(force=True)
            self.assertFalse(result["checked"])
            self.assertEqual(result["reason"], "fetch_failed")
            state = updater.read_update_state(state_dir)
            self.assertEqual(state["latest"]["version"], "9.9.9")
            self.assertEqual(state["last_check"], 1000.0)


class ThumbnailLimitTests(unittest.TestCase):
    def test_thumbnail_over_two_megabytes_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "2 MB"):
                save_thumbnail(Path(directory), b"x" * (2_000_001))

    def test_frame_disabled_means_no_frame_on_ai_thumbnails(self):
        frame = Path(tempfile.gettempdir()) / "test-frame-484.png"
        frame.write_bytes(b"fake png")
        try:
            settings = SimpleNamespace(frame_enabled=False, frame_file=str(frame))
            self.assertIsNone(_frame_path(settings))
            settings = SimpleNamespace(frame_enabled=True, frame_file=str(frame))
            self.assertEqual(_frame_path(settings), frame)
        finally:
            frame.unlink(missing_ok=True)


class WorkerStartupTests(unittest.TestCase):
    def test_value_error_in_build_commands_fails_the_worker_not_the_relay(self):
        worker = WorkerState(name="youtube", index=0)
        source = SimpleNamespace(
            urls=["https://example.invalid/live.m3u8"],
            headers={},
            width=0,
            height=0,
        )
        session = DestinationSession(targets=[("youtube", "rtmp://example.invalid/app")])
        quality = AdaptiveQuality("720p")
        settings = SimpleNamespace(
            pipe_mode=False,
            record_enabled=False,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
            frame_enabled=False,
            frame_file="",
        )
        with mock.patch(
            "live_relay.service.build_commands",
            side_effect=ValueError("Frame overlay image could not be found"),
        ):
            self.assertFalse(
                _start_worker(
                    worker,
                    "ffmpeg",
                    source,
                    session,
                    quality,
                    settings,
                )
            )


class VersionParsingTests(unittest.TestCase):
    def test_four_part_hotfix_and_pre_release_tags_parse(self):
        self.assertEqual(updater.parse_version("4.8.3.1"), (4, 8, 3, 1))
        self.assertEqual(updater.parse_version("v4.8.3-rc1"), (4, 8, 3))
        self.assertTrue(updater.is_newer("4.8.3.1", "4.8.3"))
        self.assertFalse(updater.is_newer("4.8.3-rc1", "4.8.3"))


if __name__ == "__main__":
    unittest.main()
