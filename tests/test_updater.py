"""Tests for the automatic update feature (live-relay 4.8.2)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import requests

from live_relay import __version__, updater
from live_relay.config import load_settings
from live_relay.dashboard import (
    PROJECT_ROOT,
    DashboardController,
    build_handler_class,
)
import live_relay.dashboard as dashboard_module


def _normalised_release(**overrides):
    """The dict updater.fetch_latest_release() returns (not the raw API payload)."""
    # 4.8.4: must stay newer than the local version, or the controller
    # treats it as "no newer release" and the flow tests fail.
    release = {
        "version": "4.8.4",
        "tag": "v4.8.4",
        "name": "4.8.4",
        "body": "Bug fixes",
        "html_url": "https://github.com/owner/live-relay/releases/tag/v4.8.4",
        "zipball_url": "https://api.github.com/repos/owner/live-relay/zipball/v4.8.4",
        "assets": [],
    }
    release.update(overrides)
    return release


def _release_payload(**overrides):
    payload = {
        "tag_name": "v4.8.3",
        "name": "4.8.3",
        "body": "Bug fixes",
        "html_url": "https://github.com/owner/live-relay/releases/tag/v4.8.3",
        "zipball_url": "https://api.github.com/repos/owner/live-relay/zipball/v4.8.3",
        "assets": [],
    }
    payload.update(overrides)
    return payload


class _FakeResponse:
    """Minimal urlopen response: context manager + read() + headers."""

    def __init__(self, data: bytes, headers=None):
        self._data = io.BytesIO(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._data.read(size)


def _zip_bytes(files: dict[str, str], top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            arcname = f"{top}/{name}" if top else name
            archive.writestr(arcname, content)
    return buffer.getvalue()


class VersionTests(unittest.TestCase):
    def test_parse_version_accepts_plain_and_v_prefixed(self):
        self.assertEqual(updater.parse_version("4.8.2"), (4, 8, 2))
        self.assertEqual(updater.parse_version("v4.8.3"), (4, 8, 3))

    def test_parse_version_rejects_anything_else(self):
        for bad in ["", "latest", "4.8", "4.8.2.1", "v4.8.x", None, 482]:
            self.assertIsNone(updater.parse_version(bad))

    def test_is_newer_is_strict_and_needs_both_versions(self):
        self.assertTrue(updater.is_newer("4.8.3", "4.8.2"))
        self.assertTrue(updater.is_newer("v5.0.0", "4.8.2"))
        self.assertFalse(updater.is_newer("4.8.2", "4.8.2"))
        self.assertFalse(updater.is_newer("4.8.1", "4.8.2"))
        self.assertFalse(updater.is_newer("latest", "4.8.2"))
        self.assertFalse(updater.is_newer("4.8.3", "latest"))


class FetchReleaseTests(unittest.TestCase):
    def _mock_open(self, payload):
        return mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(json.dumps(payload).encode("utf-8")),
        )

    def test_fetch_returns_a_normalised_release(self):
        with self._mock_open(_release_payload()):
            release = updater.fetch_latest_release("owner/live-relay")
        self.assertEqual(release["version"], "4.8.3")
        self.assertEqual(release["tag"], "v4.8.3")
        self.assertEqual(release["body"], "Bug fixes")
        self.assertTrue(release["zipball_url"].endswith("zipball/v4.8.3"))

    def test_fetch_rejects_a_bad_repo_name_without_touching_the_network(self):
        with mock.patch("live_relay.updater.urlopen") as urlopen:
            self.assertIsNone(updater.fetch_latest_release("not-a-repo"))
            self.assertIsNone(updater.fetch_latest_release(""))
        urlopen.assert_not_called()

    def test_fetch_network_failure_returns_none_instead_of_raising(self):
        with mock.patch(
            "live_relay.updater.urlopen", side_effect=URLError("no network")
        ):
            self.assertIsNone(updater.fetch_latest_release("owner/live-relay"))

    def test_fetch_bad_json_returns_none(self):
        with mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(b"not json"),
        ):
            self.assertIsNone(updater.fetch_latest_release("owner/live-relay"))

    def test_fetch_unparseable_tag_returns_none(self):
        with self._mock_open(_release_payload(tag_name="nightly")):
            self.assertIsNone(updater.fetch_latest_release("owner/live-relay"))


class DownloadTests(unittest.TestCase):
    def test_valid_zip_downloads_and_verifies(self):
        data = _zip_bytes({"a.txt": "hello"})
        with mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(data, {"Content-Length": str(len(data))}),
        ):
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "update.zip"
                result = updater.download_release_zip(
                    "https://example.invalid/x.zip", dest
                )
                self.assertEqual(result, dest)
                self.assertTrue(zipfile.is_zipfile(dest))

    def test_size_mismatch_removes_the_partial_file(self):
        data = _zip_bytes({"a.txt": "hello"})
        with mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(data, {"Content-Length": "99999"}),
        ):
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "update.zip"
                with self.assertRaisesRegex(ValueError, "does not match"):
                    updater.download_release_zip(
                        "https://example.invalid/x.zip", dest
                    )
                self.assertFalse(dest.exists())

    def test_non_zip_payload_is_rejected(self):
        data = b"this is not a zip"
        with mock.patch(
            "live_relay.updater.urlopen",
            return_value=_FakeResponse(data, {"Content-Length": str(len(data))}),
        ):
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "update.zip"
                with self.assertRaisesRegex(ValueError, "not a zip"):
                    updater.download_release_zip(
                        "https://example.invalid/x.zip", dest
                    )
                self.assertFalse(dest.exists())

    def test_download_failure_removes_the_partial_file(self):
        with mock.patch(
            "live_relay.updater.urlopen", side_effect=URLError("down")
        ):
            with tempfile.TemporaryDirectory() as directory:
                dest = Path(directory) / "update.zip"
                dest.write_bytes(b"PK partial")
                with self.assertRaises(RuntimeError):
                    updater.download_release_zip(
                        "https://example.invalid/x.zip", dest
                    )
                self.assertFalse(dest.exists())


class ApplyTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        (root / "src" / "live_relay").mkdir(parents=True)
        (root / "src" / "live_relay" / "__init__.py").write_text(
            '__version__ = "4.8.2"\n', encoding="utf-8"
        )
        (root / ".env").write_text("SECRET=keep-me\n", encoding="utf-8")
        (root / ".venv").mkdir()
        (root / ".venv" / "marker").write_text("venv\n", encoding="utf-8")
        (root / "client_secrets.json").write_text('{"installed": {}}\n', encoding="utf-8")
        (root / "auto.key").write_text("private\n", encoding="utf-8")
        return root

    def _stage(self, state_dir: Path, files, top=None, version="4.8.3"):
        zip_path = state_dir / "updates" / f"live-relay-{version}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(_zip_bytes(files, top=top))
        release = {"version": version, "tag": f"v{version}", "html_url": ""}
        updater.stage_update(state_dir, release, zip_path)
        return zip_path

    def test_apply_updates_code_and_preserves_secrets_and_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._project(Path(tmp) / "project")
            state_dir = Path(tmp) / "state"
            self._stage(
                state_dir,
                {
                    "src/live_relay/__init__.py": '__version__ = "4.8.3"\n',
                    "src/live_relay/updater.py": "# new updater\n",
                    ".env": "SECRET=overwrite-me\n",
                    "client_secrets.json": '{"installed": {"x": 1}}\n',
                },
                top="live-relay-4.8.3",
            )
            self.assertEqual(updater.apply_pending_update(root, state_dir), "4.8.3")
            self.assertEqual(
                (root / "src" / "live_relay" / "__init__.py").read_text(
                    encoding="utf-8"
                ),
                '__version__ = "4.8.3"\n',
            )
            self.assertTrue((root / "src" / "live_relay" / "updater.py").is_file())
            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"), "SECRET=keep-me\n"
            )
            self.assertEqual(
                (root / "client_secrets.json").read_text(encoding="utf-8"),
                '{"installed": {}}\n',
            )
            self.assertEqual(
                (root / ".venv" / "marker").read_text(encoding="utf-8"), "venv\n"
            )
            state = updater.read_update_state(state_dir)
            self.assertNotIn("staged", state)
            self.assertEqual(state["last_applied_version"], "4.8.3")

    def test_apply_handles_a_plain_zip_without_a_top_level_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._project(Path(tmp) / "project")
            state_dir = Path(tmp) / "state"
            self._stage(
                state_dir,
                {"src/live_relay/__init__.py": '__version__ = "4.8.3"\n'},
                top=None,
            )
            self.assertEqual(updater.apply_pending_update(root, state_dir), "4.8.3")

    def test_apply_with_nothing_staged_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._project(Path(tmp) / "project")
            self.assertIsNone(updater.apply_pending_update(root, Path(tmp) / "state"))

    def test_apply_rejects_a_zip_with_an_unsafe_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._project(Path(tmp) / "project")
            state_dir = Path(tmp) / "state"
            zip_path = state_dir / "updates" / "live-relay-4.8.3.zip"
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("../../evil.txt", "x")
            zip_path.write_bytes(buffer.getvalue())
            updater.stage_update(
                state_dir, {"version": "4.8.3", "tag": "v4.8.3"}, zip_path
            )
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                updater.apply_pending_update(root, state_dir)

    def test_apply_cli_returns_2_when_nothing_is_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = updater.main(
                ["--apply", "--project-root", tmp, "--state-dir", f"{tmp}/state"]
            )
            self.assertEqual(code, 2)

    def test_throttle_allows_one_check_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            self.assertTrue(updater.due_for_check(state_dir))
            updater.write_update_state(state_dir, last_check=1700000000.0)
            self.assertFalse(updater.due_for_check(state_dir, now=1700000000.0 + 60))
            self.assertTrue(
                updater.due_for_check(state_dir, now=1700000000.0 + 86401)
            )


class ConfigTests(unittest.TestCase):
    def base_env(self, **overrides):
        env = {
            "LIVE_PLATFORMS": "youtube,facebook",
            "YOUTUBE_STREAM_KEY": "dummy-key",
            "FACEBOOK_STREAM_KEY": "dummy-key",
        }
        env.update(overrides)
        return env

    def test_update_settings_default_to_enabled_with_the_published_repo(self):
        with mock.patch.dict(os.environ, self.base_env(), clear=True):
            settings = load_settings()
        self.assertTrue(settings.check_for_updates)
        self.assertEqual(settings.update_repo, "eldjazaireldjadida4-web/live-relay")

    def test_invalid_update_repo_is_rejected(self):
        with mock.patch.dict(
            os.environ, self.base_env(UPDATE_REPO="not-a-repo"), clear=True
        ):
            with self.assertRaisesRegex(ValueError, "owner/repo"):
                load_settings()

    def test_valid_update_repo_loads(self):
        with mock.patch.dict(
            os.environ,
            self.base_env(UPDATE_REPO="owner/live-relay", CHECK_FOR_UPDATES="false"),
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.update_repo, "owner/live-relay")
        self.assertFalse(settings.check_for_updates)


class ControllerUpdateTests(unittest.TestCase):
    def make_controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={root / 'state'}\n"
            "UPDATE_REPO=owner/live-relay\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def test_check_is_throttled_and_force_bypasses_it(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-update-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            with mock.patch.object(
                updater, "fetch_latest_release", return_value=_normalised_release()
            ) as fetch:
                first = controller.check_for_updates(force=True)
                second = controller.check_for_updates()
                third = controller.check_for_updates(force=True)
            self.assertTrue(first["checked"])
            self.assertTrue(first["update_available"])
            self.assertFalse(second["checked"])
            self.assertEqual(second["reason"], "throttled")
            self.assertTrue(third["checked"])
            self.assertEqual(fetch.call_count, 2)

    def test_check_with_no_repo_configured_reports_it(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-update-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / ".env").write_text(
                f"LIVE_RELAY_STATE_DIR={root / 'state'}\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
            with mock.patch.object(updater, "fetch_latest_release") as fetch:
                result = controller.check_for_updates(force=True)
            self.assertFalse(result["checked"])
            self.assertEqual(result["reason"], "no_repo")
            fetch.assert_not_called()

    def test_download_and_install_flow(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-update-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            release = _normalised_release()
            with (
                mock.patch.object(
                    updater, "fetch_latest_release", return_value=release
                ),
                mock.patch.object(
                    updater, "download_release_zip", return_value=Path("x.zip")
                ) as download,
            ):
                downloaded = controller.download_update()
                self.assertIsNotNone(downloaded["staged"])
                self.assertEqual(downloaded["staged"]["version"], "4.8.4")
                download.assert_called_once()
                installed = controller.install_update()
            self.assertTrue(installed["restart_required"])
            self.assertTrue(controller.update_restart_requested)

    def test_install_refuses_while_the_relay_is_running(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-update-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            controller._relay_thread = mock.Mock()
            controller._relay_thread.is_alive.return_value = True
            with self.assertRaisesRegex(RuntimeError, "Stop the relay"):
                controller.install_update()

    def test_save_config_rejects_a_bad_update_repo(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-update-", dir=PROJECT_ROOT
        ) as directory:
            controller = self.make_controller(Path(directory))
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "owner/repo"):
                    controller.save_config({"UPDATE_REPO": "not-a-repo"})


class UpdateHttpTests(unittest.TestCase):
    def make_controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={root / 'state'}\n"
            "UPDATE_REPO=owner/live-relay\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            return DashboardController(root)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-update-http-", dir=PROJECT_ROOT
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

    def test_update_status_endpoint_reports_the_current_version(self):
        response = requests.get(
            f"{self.base_url()}/api/update",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["current_version"], __version__)
        self.assertEqual(data["repo"], "owner/live-relay")
        self.assertFalse(data["update_available"])

    def test_update_check_endpoint_needs_the_control_token(self):
        response = requests.post(
            f"{self.base_url()}/api/update/check", json={}, timeout=5
        )
        self.assertEqual(response.status_code, 403)

    def test_dashboard_page_contains_the_updates_card(self):
        response = requests.get(f"{self.base_url()}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="update-check-btn"', response.text)
        self.assertIn('id="update-install-btn"', response.text)
        self.assertIn('id="update-repo"', response.text)


if __name__ == "__main__":
    unittest.main()
