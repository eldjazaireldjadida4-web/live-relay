from __future__ import annotations

import json
import os
import stat
import struct
import tempfile
import threading
import time
import unittest
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class
from live_relay.destinations import FacebookDestination, YouTubeDestination


def make_png(width: int = 2, height: int = 1) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        crc = zlib.crc32(kind + content) & 0xFFFFFFFF
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\x00" + b"\x00\x00\x00\xff" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


class DashboardControllerTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def test_packaged_template_bootstraps_an_installed_project(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-package-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            with patch.dict(os.environ, {}, clear=True):
                DashboardController(root)
            self.assertTrue((root / ".env").is_file())
            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"),
                (Path(dashboard_module.__file__).with_name(".env.example")).read_text(
                    encoding="utf-8"
                ),
            )

    def test_manual_modes_do_not_offer_unused_gemini_generation(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-metadata-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                config = DashboardController(root).public_config()
            self.assertFalse(config["metadata_api_enabled"])
            self.assertFalse(any("Gemini" in note for note in config["notes"]))

    def test_first_launch_creates_local_env_and_discovers_oauth_file(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            client_file = root / "client_secret-test.json"
            client_file.write_text(
                json.dumps({"installed": {"client_id": "dummy"}}), encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                config = controller.public_config()
                self.assertTrue((root / ".env").is_file())
                self.assertEqual(
                    config["youtube_client_secrets"], str(client_file.resolve())
                )
                self.assertTrue(config["youtube_client_file_exists"])
                if os.name != "nt":
                    self.assertEqual(
                        stat.S_IMODE((root / ".env").stat().st_mode), 0o600
                    )

    def test_saved_secret_is_never_returned_to_the_browser(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                config = controller.save_config(
                    {
                        "FACEBOOK_ACCESS_TOKEN": "dummy-local-test-token",
                        "GEMINI_API_KEY": "dummy-gemini-test-key",
                    }
                )
                self.assertTrue(config["facebook_token_set"])
                self.assertTrue(config["gemini_api_key_set"])
                serialized = json.dumps(config)
                self.assertNotIn("dummy-local-test-token", serialized)
                self.assertNotIn("dummy-gemini-test-key", serialized)

    def test_youtube_authorization_does_not_require_facebook_credentials(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            client_file = root / "client_secret-test.json"
            client_file.write_text(
                json.dumps({"installed": {"client_id": "dummy"}}), encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                with patch.object(
                    YouTubeDestination, "authenticate", Mock()
                ) as authenticate:
                    controller.authorize_youtube()
                    authenticate.assert_called_once()

    def test_facebook_static_key_allows_manual_mode_start(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                config = controller.save_config(
                    {
                        "LIVE_PLATFORMS": "facebook",
                        "FACEBOOK_STREAM_KEY": "dummy-static-key",
                    }
                )
                self.assertEqual(config["missing"], [])
                self.assertTrue(config["facebook_static_key_set"])
                self.assertTrue(
                    any("Live Producer" in item for item in config["notes"])
                )
                result = controller.preflight()
                self.assertEqual(result["platforms"], ["facebook"])

    def test_facebook_without_token_or_key_is_blocking(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                config = controller.save_config({"LIVE_PLATFORMS": "facebook"})
                self.assertTrue(config["missing"])
                with self.assertRaisesRegex(RuntimeError, "الإعداد غير جاهز"):
                    controller.preflight()

    def test_facebook_access_check_works_without_youtube_credentials(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                token = "dummy-local-test-token"
                controller.save_config({"FACEBOOK_ACCESS_TOKEN": token})
                with patch.object(
                    FacebookDestination,
                    "verify_access",
                    return_value={"id": "123456", "name": "Test Page"},
                ) as verify:
                    result = controller.verify_facebook_access()
                self.assertEqual(result, {"id": "123456", "name": "Test Page"})
                self.assertNotIn(token, json.dumps(result))
                verify.assert_called_once()

    def test_youtube_static_mode_ignores_stale_oauth_and_starts_without_token(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            stale_client = root / "missing-client-secret.json"
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                config = controller.save_config(
                    {
                        "LIVE_PLATFORMS": "youtube",
                        "YOUTUBE_MODE": "static",
                        "YOUTUBE_CLIENT_SECRETS_FILE": str(stale_client),
                        "YOUTUBE_STREAM_KEY": "dummy-static-key",
                    }
                )
                self.assertEqual(config["missing"], [])
                self.assertEqual(config["youtube_effective_mode"], "static")
                self.assertFalse(config["youtube_authorized"])

                with (
                    patch("live_relay.dashboard.resolve_ffmpeg", return_value="ffmpeg"),
                    patch(
                        "live_relay.dashboard.subprocess.run",
                        return_value=Mock(stdout="yt-dlp test"),
                    ),
                ):
                    result = controller.preflight()
                self.assertFalse(result["youtube_needs_auth"])
                self.assertEqual(result["modes"]["youtube"], "STATIC")

                entered = threading.Event()
                release = threading.Event()

                def hold_relay(_settings, _stopped):
                    entered.set()
                    release.wait(2)

                with (
                    patch("live_relay.dashboard.resolve_ffmpeg", return_value="ffmpeg"),
                    patch.object(controller, "_run_relay", side_effect=hold_relay),
                ):
                    status = controller.start_relay()
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(status["is_running"])
                    release.set()
                    controller._relay_thread.join(timeout=2)
                self.assertFalse(controller.get_status()["is_running"])

    def test_youtube_access_check_requires_a_linked_channel_first(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            client_file = root / "client_secret-test.json"
            client_file.write_text(
                json.dumps({"installed": {"client_id": "dummy"}}), encoding="utf-8"
            )
            state_dir = root / "state"
            state_dir.mkdir()
            (root / ".env").write_text(
                f"LIVE_RELAY_STATE_DIR={state_dir}\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                with self.assertRaisesRegex(ValueError, "ربط YouTube"):
                    controller.verify_youtube_access()

    def test_youtube_access_check_reports_the_linked_channel(self):
        with tempfile.TemporaryDirectory(
            prefix=".dashboard-test-", dir=PROJECT_ROOT
        ) as directory:
            root = Path(directory)
            self.make_project(root)
            client_file = root / "client_secret-test.json"
            client_file.write_text(
                json.dumps({"installed": {"client_id": "dummy"}}), encoding="utf-8"
            )
            state_dir = root / "state"
            state_dir.mkdir()
            (state_dir / "youtube-token.json").write_text("{}", encoding="utf-8")
            (root / ".env").write_text(
                f"LIVE_RELAY_STATE_DIR={state_dir}\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                controller = DashboardController(root)
                with patch.object(
                    YouTubeDestination,
                    "verify_access",
                    return_value={"id": "UC-test-channel", "name": "Test Channel"},
                ) as verify:
                    result = controller.verify_youtube_access()
                self.assertEqual(
                    result, {"id": "UC-test-channel", "name": "Test Channel"}
                )
                verify.assert_called_once()


class DashboardHttpTests(unittest.TestCase):
    def make_controller(self, root: Path) -> DashboardController:
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={Path(root) / 'state'}\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            return DashboardController(Path(root))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-http-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        self.controller = self.make_controller(root)
        self.state_dir = root / "state"
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

    def test_config_endpoint_requires_the_control_token(self):
        denied = requests.get(f"{self.base_url()}/api/config", timeout=5)
        self.assertEqual(denied.status_code, 403)
        response = requests.get(
            f"{self.base_url()}/api/config",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("missing", response.json())

    def test_unknown_route_returns_404(self):
        response = requests.get(f"{self.base_url()}/nope", timeout=5)
        self.assertEqual(response.status_code, 404)

    def test_post_rejects_invalid_control_token(self):
        response = requests.post(
            f"{self.base_url()}/api/config",
            json={"LIVE_PLATFORMS": "facebook"},
            headers={"X-Relay-Token": "wrong"},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)

    def test_post_config_with_valid_token_saves_and_records_token_age(self):
        response = requests.post(
            f"{self.base_url()}/api/config",
            json={"FACEBOOK_ACCESS_TOKEN": "dummy-http-test-token"},
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["facebook_token_set"])
        self.assertNotIn("dummy-http-test-token", response.text)
        self.assertEqual(body["facebook_token_age_days"], 0)
        self.assertFalse(body["facebook_token_stale"])
        meta_path = self.state_dir / "facebook-token-meta.json"
        self.assertTrue(meta_path.is_file())

    def test_metadata_endpoint_saves_manual_overrides_for_next_stream(self):
        response = requests.post(
            f"{self.base_url()}/api/metadata/apply",
            json={
                "title": "عنوان يدوي للبث",
                "description": "وصف مختصر",
                "tags": "أخبار، مباشر; تحليل",
            },
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["applied_now"])
        config = requests.get(
            f"{self.base_url()}/api/config",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        ).json()
        self.assertEqual(config["youtube_title_override"], "عنوان يدوي للبث")
        self.assertEqual(config["youtube_description_override"], "وصف مختصر")
        self.assertEqual(config["youtube_tags_override"], "أخبار, مباشر, تحليل")

    def test_thumbnail_upload_is_authenticated_persisted_and_previewable(self):
        image = make_png()
        headers = {
            "X-Relay-Token": self.controller.control_token,
            "Content-Type": "image/png",
        }
        denied = requests.get(f"{self.base_url()}/api/youtube/thumbnail", timeout=5)
        self.assertEqual(denied.status_code, 403)
        uploaded = requests.post(
            f"{self.base_url()}/api/youtube/thumbnail",
            data=image,
            headers=headers,
            timeout=5,
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["width"], 2)
        self.assertEqual(uploaded.json()["height"], 1)
        self.assertFalse(uploaded.json()["recommended"])
        saved = self.state_dir / "youtube-thumbnail.png"
        self.assertEqual(saved.read_bytes(), image)
        if os.name != "nt":
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        config = requests.get(
            f"{self.base_url()}/api/config",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        ).json()
        self.assertTrue(config["youtube_thumbnail_ready"])
        preview = requests.get(
            f"{self.base_url()}/api/youtube/thumbnail",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, image)
        self.assertEqual(preview.headers["Content-Type"], "image/png")

    def test_delete_clears_saved_thumbnail_and_hold(self):
        from live_relay.thumbnails import manual_hold as hold_after

        headers = {
            "X-Relay-Token": self.controller.control_token,
            "Content-Type": "image/png",
        }
        uploaded = requests.post(
            f"{self.base_url()}/api/youtube/thumbnail",
            data=make_png(),
            headers=headers,
            timeout=5,
        )
        self.assertEqual(uploaded.status_code, 200)
        saved = self.state_dir / "youtube-thumbnail.png"
        self.assertTrue(saved.exists())
        self.assertTrue(hold_after(self.state_dir))
        cleared = requests.delete(
            f"{self.base_url()}/api/youtube/thumbnail",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(saved.exists())
        self.assertFalse(hold_after(self.state_dir))
        config = requests.get(
            f"{self.base_url()}/api/config",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        ).json()
        self.assertFalse(config["youtube_thumbnail_ready"])
        self.assertEqual(config["thumbnail_provider"], "")

    def test_manual_upload_sets_hold_that_blocks_auto_generation(self):
        from live_relay.thumbnails import manual_hold

        headers = {
            "X-Relay-Token": self.controller.control_token,
            "Content-Type": "image/png",
        }
        uploaded = requests.post(
            f"{self.base_url()}/api/youtube/thumbnail",
            data=make_png(),
            headers=headers,
            timeout=5,
        )
        self.assertEqual(uploaded.status_code, 200)
        self.assertTrue(manual_hold(self.state_dir))
        requests.delete(
            f"{self.base_url()}/api/youtube/thumbnail",
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertFalse(manual_hold(self.state_dir))

    def test_live_metadata_changes_are_applied_without_stopping_the_relay(self):
        session = Mock()
        self.controller.status.set_destination_session(session)
        self.controller._relay_thread = Mock()
        self.controller._relay_thread.is_alive.return_value = True
        payload = {
            "title": "عنوان مباشر مخصص",
            "description": "وصف مباشر",
            "tags": "أخبار، بث مباشر",
        }
        with patch(
            "live_relay.dashboard.update_destination_metadata",
            return_value={"youtube": "updated"},
        ) as update:
            result = self.controller.apply_metadata(payload)
        self.assertTrue(result["applied_now"])
        self.assertEqual(update.call_args.args[0], session)
        self.assertTrue(update.call_args.kwargs["manual"])
        status = self.controller.get_status()
        self.assertEqual(status["title"], "عنوان مباشر مخصص")
        self.assertTrue(status["metadata_manual"])

    def test_dashboard_page_embeds_a_session_token_not_the_control_token(self):
        response = requests.get(f"{self.base_url()}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        # M2: the served page must never carry the real control token;
        # any local process reading the page would otherwise own the
        # dashboard. It carries a page-scoped session token instead.
        self.assertNotIn(self.controller.control_token, response.text)
        self.assertIn('id="gemini-key"', response.text)
        self.assertIn('id="logo-enabled"', response.text)
        self.assertIn('id="metadata-preview"', response.text)

    def test_foreign_host_header_is_rejected(self):
        response = requests.get(
            f"{self.base_url()}/api/status", headers={"Host": "evil.example"}, timeout=5
        )
        self.assertEqual(response.status_code, 403)

    def test_verify_facebook_records_verification_time(self):
        self.controller.save_config({"FACEBOOK_ACCESS_TOKEN": "dummy-http-test-token"})
        with patch.object(
            FacebookDestination,
            "verify_access",
            return_value={"id": "123456", "name": "Test Page"},
        ) as verify:
            response = requests.post(
                f"{self.base_url()}/api/facebook/verify",
                json={},
                headers={"X-Relay-Token": self.controller.control_token},
                timeout=5,
            )
        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        meta = json.loads(
            (self.state_dir / "facebook-token-meta.json").read_text(encoding="utf-8")
        )
        self.assertIn("last_verified_at", meta)


class FacebookTokenAgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-age-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={root / 'state'}\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            self.controller = DashboardController(root)
        self.state_dir = root / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_age_is_null_before_any_token_is_saved(self):
        config = self.controller.public_config()
        self.assertIsNone(config["facebook_token_age_days"])
        self.assertFalse(config["facebook_token_stale"])

    def test_stale_after_46_days(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        old = time.time() - 46 * 86400
        (self.state_dir / "facebook-token-meta.json").write_text(
            json.dumps({"saved_at": old}), encoding="utf-8"
        )
        config = self.controller.public_config()
        self.assertEqual(config["facebook_token_age_days"], 46)
        self.assertTrue(config["facebook_token_stale"])

    def test_save_config_records_saved_at(self):
        self.controller.save_config({"FACEBOOK_ACCESS_TOKEN": "dummy-http-test-token"})
        meta = json.loads(
            (self.state_dir / "facebook-token-meta.json").read_text(encoding="utf-8")
        )
        self.assertLess(time.time() - meta["saved_at"], 60)
        self.assertFalse(self.controller.public_config()["facebook_token_stale"])


if __name__ == "__main__":
    unittest.main()
