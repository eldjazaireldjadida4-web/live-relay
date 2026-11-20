from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv, set_key

from . import __version__, updater
from .config import (
    default_state_dir,
    ensure_private_dir,
    load_settings,
    resolve_project_path,
    resolve_project_root,
)
from .destinations import (
    FacebookDestination,
    YouTubeDestination,
    update_destination_metadata,
    update_destination_thumbnail,
)
from .encoder import redact_urls, resolve_ffmpeg
from .metadata import StreamMetadata, clean_description, clean_tags
from .runtime import RelayStatus
from .service import run
from .source import clean_title
from .source_platforms import validate_source_platform
from .thumbnails import (
    MAX_THUMBNAIL_SIZE,
    clear_thumbnail,
    get_thumbnail_path,
    save_thumbnail,
    set_manual_hold,
)

PROJECT_ROOT = resolve_project_root()
SECRET_KEYS = (
    "YOUTUBE_STREAM_KEY",
    "FACEBOOK_ACCESS_TOKEN",
    "FACEBOOK_STREAM_KEY",
    "TIKTOK_STREAM_KEY",
    "CUSTOM_STREAM_KEY",
    "GEMINI_API_KEY",
)
CONFIG_KEYS = (
    "SOURCE_URL",
    "SOURCE_PLATFORM",
    "LIVE_PLATFORMS",
    "YOUTUBE_CLIENT_SECRETS_FILE",
    "YOUTUBE_CHANNEL_ID",
    "YOUTUBE_MODE",
    "YOUTUBE_PRIVACY_STATUS",
    "YOUTUBE_CATEGORY_ID",
    "YOUTUBE_BACKUP",
    "YOUTUBE_TITLE_OVERRIDE",
    "YOUTUBE_DESCRIPTION_OVERRIDE",
    "YOUTUBE_TAGS_OVERRIDE",
    "FACEBOOK_TARGET_ID",
    "FACEBOOK_GRAPH_VERSION",
    "TIKTOK_RTMP_URL",
    "CUSTOM_RTMP_URL",
    "CUSTOM_RTMP_NAME",
    "GEMINI_MODEL",
    "GEMINI_METADATA_ENABLED",
    "METADATA_LANGUAGE",
    "LOGO_ENABLED",
    "LOGO_FILE",
    "LOGO_POSITION",
    "LOGO_SIZE_PERCENT",
    "LOGO_OPACITY",
    "FRAME_ENABLED",
    "FRAME_FILE",
    "INITIAL_PROFILE",
    "ADAPTIVE_QUALITY",
    "PIPE_MODE",
    "FFMPEG",
    "FFMPEG_PRESET",
    "YTDLP_FORMAT",
    "YTDLP_COOKIE_FILE",
    "YTDLP_COOKIES_FROM_BROWSER",
    "RECONNECT_DELAY",
    "SOURCE_RETRY",
    "STALL_TIMEOUT",
    "HEARTBEAT",
    "SOURCE_END_GRACE",
    "UPDATE_REPO",
    "CHECK_FOR_UPDATES",
)

METADATA_OVERRIDE_KEYS = {
    "YOUTUBE_TITLE_OVERRIDE",
    "YOUTUBE_DESCRIPTION_OVERRIDE",
    "YOUTUBE_TAGS_OVERRIDE",
}


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _default_value(key: str) -> str:
    defaults = {
        "SOURCE_URL": "https://www.youtube.com/channel/UCFmnqispHbyVZKRM6RcybWQ/live",
        "SOURCE_PLATFORM": "auto",
        "LIVE_PLATFORMS": "youtube,facebook",
        "YOUTUBE_CHANNEL_ID": "UCioeMW7bZNeyV48Z09q2vTg",
        "YOUTUBE_MODE": "auto",
        "YOUTUBE_PRIVACY_STATUS": "public",
        "YOUTUBE_CATEGORY_ID": "22",
        "YOUTUBE_TITLE_OVERRIDE": "",
        "YOUTUBE_DESCRIPTION_OVERRIDE": "",
        "YOUTUBE_TAGS_OVERRIDE": "",
        "FACEBOOK_TARGET_ID": "me",
        "FACEBOOK_GRAPH_VERSION": "v26.0",
        "TIKTOK_RTMP_URL": "rtmp://rtmp-push.tiktok.com/live",
        "CUSTOM_RTMP_URL": "rtmp://live.restream.io/live",
        "CUSTOM_RTMP_NAME": "Restream",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_METADATA_ENABLED": "true",
        "METADATA_LANGUAGE": "ar",
        "LOGO_ENABLED": "true",
        "LOGO_POSITION": "bottom-right",
        "LOGO_SIZE_PERCENT": "12",
        "LOGO_OPACITY": "0.9",
        "FRAME_ENABLED": "false",
        "INITIAL_PROFILE": "720p",
        "ADAPTIVE_QUALITY": "true",
        "FFMPEG_PRESET": "veryfast",
        "YTDLP_FORMAT": "b[height<=720]/bv*[height<=720]+ba/best",
        "YOUTUBE_BACKUP": "false",
        "PIPE_MODE": "false",
        "RECONNECT_DELAY": "5",
        "SOURCE_RETRY": "30",
        "STALL_TIMEOUT": "45",
        "HEARTBEAT": "30",
        "SOURCE_END_GRACE": "180",
    }
    return defaults.get(key, "")


class DashboardController:
    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root.resolve()
        self.env_path = self.project_root / ".env"
        self.example_path = self.project_root / ".env.example"
        self._lock = threading.RLock()
        self._relay_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self.status = RelayStatus()
        self.control_token = secrets.token_urlsafe(32)
        self.update_restart_requested = False
        self._shutdown_callback = None
        self.ensure_env_file()
        self.discover_youtube_client_file()
        load_dotenv(self.env_path, override=True)

    def ensure_env_file(self) -> None:
        if self.env_path.exists():
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
        if not self.env_path.exists():
            template_path = self.example_path
            if not template_path.is_file():
                template_path = Path(__file__).with_name(".env.example")
            if not template_path.is_file():
                raise RuntimeError("The .env.example setup template is missing")
            shutil.copyfile(template_path, self.env_path)
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass

    def discover_youtube_client_file(self) -> str:
        values = dotenv_values(self.env_path)
        configured = str(values.get("YOUTUBE_CLIENT_SECRETS_FILE") or "").strip()
        if configured:
            return configured
        candidates = sorted(self.project_root.glob("client_secret*.json"))
        candidates.extend(sorted(self.project_root.glob("oauth-client*.json")))
        for candidate in candidates:
            try:
                content = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if any(key in content for key in ("installed", "web")):
                configured = str(candidate.resolve())
                set_key(str(self.env_path), "YOUTUBE_CLIENT_SECRETS_FILE", configured)
                try:
                    os.chmod(self.env_path, 0o600)
                except OSError:
                    pass
                return configured
        return ""

    def _values(self) -> dict[str, str]:
        raw = dotenv_values(self.env_path)
        return {key: str(value) for key, value in raw.items() if value is not None}

    def _current_state_dir(self, values: dict[str, str] | None = None) -> Path:
        values = values or self._values()
        configured = values.get("LIVE_RELAY_STATE_DIR", "").strip()
        return Path(configured).expanduser() if configured else default_state_dir()

    def _token_meta_path(self, values: dict[str, str] | None = None) -> Path:
        return self._current_state_dir(values) / "facebook-token-meta.json"

    def _read_token_meta(self, values: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            data = json.loads(self._token_meta_path(values).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_token_meta(self, **fields: Any) -> None:
        path = self._token_meta_path()
        try:
            ensure_private_dir(path.parent)
            meta = self._read_token_meta()
            meta.update(fields)
            meta["updated_at"] = time.time()
            path.write_text(json.dumps(meta), encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            logging.warning("Could not update the Facebook token metadata file")

    def _facebook_token_age_days(
        self, values: dict[str, str] | None = None
    ) -> int | None:
        saved_at = self._read_token_meta(values).get("saved_at")
        if not isinstance(saved_at, (int, float)):
            return None
        return max(0, int((time.time() - saved_at) // 86400))

    def _facebook_token_stale(self, values: dict[str, str] | None = None) -> bool:
        age = self._facebook_token_age_days(values)
        return bool(age is not None and age > 45)

    def _is_running(self) -> bool:
        return bool(self._relay_thread and self._relay_thread.is_alive())

    def public_config(self) -> dict[str, Any]:
        values = self._values()
        platforms = [
            part.strip().lower()
            for part in values.get("LIVE_PLATFORMS", "youtube,facebook").split(",")
            if part.strip()
        ]
        state_dir = self._current_state_dir(values)
        thumbnail_path = get_thumbnail_path(state_dir)
        thumbnail_id = ""
        if thumbnail_path:
            try:
                thumbnail_stat = thumbnail_path.stat()
                thumbnail_id = (
                    f"{thumbnail_path.name}:{thumbnail_stat.st_mtime_ns}:"
                    f"{thumbnail_stat.st_size}"
                )
            except OSError:
                thumbnail_path = None
        client_file = (
            values.get("YOUTUBE_CLIENT_SECRETS_FILE", "").strip()
            or self.discover_youtube_client_file()
        )
        client_exists = bool(client_file and Path(client_file).expanduser().is_file())
        token_file = state_dir / "youtube-token.json"
        missing: list[str] = []
        notes: list[str] = []
        youtube_mode = values.get("YOUTUBE_MODE", "auto").strip().lower()
        youtube_api_enabled = youtube_mode == "api" or (
            youtube_mode == "auto" and bool(client_file)
        )
        facebook_api_enabled = bool(values.get("FACEBOOK_ACCESS_TOKEN", "").strip())
        metadata_api_enabled = ("youtube" in platforms and youtube_api_enabled) or (
            "facebook" in platforms and facebook_api_enabled
        )
        youtube_key_set = bool(values.get("YOUTUBE_STREAM_KEY", "").strip())
        if "youtube" in platforms:
            if youtube_api_enabled:
                if not client_file:
                    missing.append(
                        "وضع API في YouTube يتطلب ملف Google OAuth client JSON."
                    )
                elif not client_exists:
                    missing.append(
                        "تعذر العثور على ملف Google OAuth المحدد لـ YouTube."
                    )
            elif youtube_key_set:
                notes.append(
                    "وضع المفتاح الثابت لـ YouTube: أنشئ البث في Studio يدوياً (بالمفتاح نفسه)؛ لن ينشئ البرنامج البث ولا يزامن العنوان."
                )
            elif youtube_mode == "static":
                missing.append(
                    "وضع المفتاح الثابت لـ YouTube يتطلب YOUTUBE_STREAM_KEY."
                )
            else:
                missing.append(
                    "للبث إلى YouTube، أضف ملف Google OAuth client JSON أو مفتاح بث ثابت."
                )
        if (
            "facebook" in platforms
            and not values.get("FACEBOOK_ACCESS_TOKEN", "").strip()
        ):
            if values.get("FACEBOOK_STREAM_KEY", "").strip():
                notes.append(
                    "وضع المفتاح الثابت لـ Facebook: ابدأ البث من Live Producer (بالمفتاح نفسه)؛ لن ينشئ البرنامج البث ولا يزامن العنوان."
                )
            else:
                missing.append(
                    "للبث إلى Facebook، أضف رمز وصول Meta Live Video API أو مفتاح بث ثابت."
                )
        if "tiktok" in platforms:
            if values.get("TIKTOK_STREAM_KEY", "").strip():
                notes.append(
                    "وضع المفتاح الثابت لـ TikTok: أنشئ البث على TikTok أولاً وانسخ رابط الخادم والمفتاح للجلسة الحالية؛ المفتاح لمرة واحدة وينتهي، ولن ينشئ البرنامج البث ولا يزامن العنوان."
                )
            else:
                missing.append(
                    "للبث إلى TikTok، أنشئ البث على TikTok وأضف رابط خادم RTMP ومفتاح البث للجلسة."
                )
        if "custom" in platforms:
            if values.get("CUSTOM_STREAM_KEY", "").strip():
                notes.append(
                    "جسر RTMP مخصص (Restream): يُدفع البث مرة واحدة إلى Restream، فينشئ Restream بث TikTok تلقائياً دون مفتاح جلسة — فعّل قناة TikTok في Restream أولاً."
                )
            else:
                missing.append(
                    "للبث عبر جسر RTMP مخصص، أضف مفتاح بث Restream الدائم (رابط RTMP ومفتاح البث من إعدادات Restream)."
                )
        gemini_enabled = _as_bool(values.get("GEMINI_METADATA_ENABLED", "true"))
        gemini_key_set = bool(values.get("GEMINI_API_KEY", "").strip())
        if gemini_enabled and metadata_api_enabled and not gemini_key_set:
            notes.append(
                "أضف مفتاح Gemini لتفعيل إنشاء العنوان والوصف والكلمات المفتاحية تلقائياً."
            )
        logo_enabled = _as_bool(values.get("LOGO_ENABLED", "true"))
        configured_logo = values.get("LOGO_FILE", "").strip()
        logo_file = (
            resolve_project_path(configured_logo)
            if configured_logo
            else resolve_project_path("assets/live-relay-logo.png")
        )
        if not configured_logo and not logo_file.is_file():
            logo_file = Path(__file__).with_name("stream-logo.png")
        if logo_enabled and not logo_file.is_file():
            missing.append(
                "LOGO_FILE must point to an existing image, or disable the logo."
            )
        frame_enabled = _as_bool(values.get("FRAME_ENABLED", "false"))
        configured_frame = values.get("FRAME_FILE", "").strip()
        frame_file = (
            resolve_project_path(configured_frame) if configured_frame else None
        )
        if (
            frame_enabled
            and frame_file is not None
            and (frame_file.suffix.lower() != ".png" or not frame_file.is_file())
        ):
            missing.append("FRAME_FILE must point to an existing PNG image.")
        # The logo numbers come from the raw .env and may be hand-edited
        # garbage (``LOGO_SIZE_PERCENT=abc``) or a non-JSON value such as
        # ``nan``. Never let one bad value break the whole config response:
        # fall back to the defaults and say so in ``missing``.
        try:
            logo_size_percent = int(values.get("LOGO_SIZE_PERCENT", "12"))
        except (TypeError, ValueError):
            logo_size_percent = 12
            missing.append("LOGO_SIZE_PERCENT is not a number; using 12%.")
        if not 5 <= logo_size_percent <= 25:
            missing.append("LOGO_SIZE_PERCENT must be between 5 and 25; using 12%.")
            logo_size_percent = 12
        try:
            logo_opacity = float(values.get("LOGO_OPACITY", "0.9"))
        except (TypeError, ValueError):
            logo_opacity = 0.9
            missing.append("LOGO_OPACITY is not a number; using 0.9.")
        if logo_opacity != logo_opacity or not 0.1 <= logo_opacity <= 1:
            missing.append("LOGO_OPACITY must be between 0.1 and 1; using 0.9.")
            logo_opacity = 0.9
        config = {
            "source_url": values.get("SOURCE_URL", _default_value("SOURCE_URL")),
            "source_platform": values.get("SOURCE_PLATFORM", "auto"),
            "platforms": platforms,
            "youtube_client_secrets": client_file,
            "youtube_client_file_exists": client_exists,
            "youtube_channel_id": values.get(
                "YOUTUBE_CHANNEL_ID", _default_value("YOUTUBE_CHANNEL_ID")
            ),
            "youtube_mode": youtube_mode,
            "youtube_effective_mode": "api" if youtube_api_enabled else "static",
            "youtube_api_enabled": youtube_api_enabled,
            "youtube_privacy": values.get(
                "YOUTUBE_PRIVACY_STATUS", _default_value("YOUTUBE_PRIVACY_STATUS")
            ),
            "youtube_category_id": values.get("YOUTUBE_CATEGORY_ID", "22"),
            "youtube_title_override": values.get("YOUTUBE_TITLE_OVERRIDE", ""),
            "youtube_description_override": values.get(
                "YOUTUBE_DESCRIPTION_OVERRIDE", ""
            ),
            "youtube_tags_override": values.get("YOUTUBE_TAGS_OVERRIDE", ""),
            "youtube_backup": _as_bool(values.get("YOUTUBE_BACKUP", "false")),
            "youtube_authorized": token_file.is_file(),
            "youtube_static_key_set": bool(
                values.get("YOUTUBE_STREAM_KEY", "").strip()
            ),
            "facebook_target_id": values.get("FACEBOOK_TARGET_ID", "me"),
            "facebook_graph_version": values.get("FACEBOOK_GRAPH_VERSION", "v26.0"),
            "gemini_api_key_set": gemini_key_set,
            "gemini_model": values.get("GEMINI_MODEL", _default_value("GEMINI_MODEL")),
            "gemini_metadata_enabled": gemini_enabled,
            "metadata_api_enabled": metadata_api_enabled,
            "youtube_thumbnail_ready": bool(thumbnail_path),
            "youtube_thumbnail_name": thumbnail_path.name if thumbnail_path else "",
            "youtube_thumbnail_id": thumbnail_id,
            "thumbnail_provider": self.status.snapshot().get("thumbnail_provider", ""),
            "metadata_language": values.get("METADATA_LANGUAGE", "ar"),
            "logo_enabled": logo_enabled,
            "logo_file": str(logo_file),
            "logo_position": values.get("LOGO_POSITION", "bottom-right"),
            "logo_size_percent": logo_size_percent,
            "logo_opacity": logo_opacity,
            "frame_enabled": frame_enabled,
            "frame_file": str(frame_file) if frame_file else "",
            "facebook_token_set": bool(values.get("FACEBOOK_ACCESS_TOKEN", "").strip()),
            "facebook_static_key_set": bool(
                values.get("FACEBOOK_STREAM_KEY", "").strip()
            ),
            "tiktok_rtmp_url": values.get(
                "TIKTOK_RTMP_URL", _default_value("TIKTOK_RTMP_URL")
            ),
            "tiktok_static_key_set": bool(
                values.get("TIKTOK_STREAM_KEY", "").strip()
            ),
            "custom_rtmp_url": values.get(
                "CUSTOM_RTMP_URL", _default_value("CUSTOM_RTMP_URL")
            ),
            "custom_rtmp_name": values.get(
                "CUSTOM_RTMP_NAME", _default_value("CUSTOM_RTMP_NAME")
            ),
            "custom_stream_key_set": bool(
                values.get("CUSTOM_STREAM_KEY", "").strip()
            ),
            "facebook_token_age_days": self._facebook_token_age_days(values),
            "facebook_token_stale": self._facebook_token_stale(values),
            "initial_profile": values.get("INITIAL_PROFILE", "720p"),
            "adaptive_quality": _as_bool(values.get("ADAPTIVE_QUALITY", "true")),
            "pipe_mode": _as_bool(values.get("PIPE_MODE", "false")),
            "ffmpeg": values.get("FFMPEG", ""),
            "ffmpeg_preset": values.get("FFMPEG_PRESET", "veryfast"),
            "ytdlp_format": values.get("YTDLP_FORMAT", _default_value("YTDLP_FORMAT")),
            "cookie_file": values.get("YTDLP_COOKIE_FILE", ""),
            "cookies_from_browser": values.get("YTDLP_COOKIES_FROM_BROWSER", ""),
            "update_repo": values.get("UPDATE_REPO", ""),
            "check_for_updates": _as_bool(values.get("CHECK_FOR_UPDATES", "true")),
            "current_version": __version__,
            "is_running": self._is_running(),
            "missing": missing,
            "notes": notes,
        }
        return config

    def save_config(
        self, data: dict[str, Any], allow_running: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            if self._is_running() and not allow_running:
                raise RuntimeError("Stop the relay before changing settings")
            if allow_running and not set(data).issubset(METADATA_OVERRIDE_KEYS):
                raise RuntimeError("Only live metadata can be changed while streaming")
            updates: dict[str, str] = {}
            for key in CONFIG_KEYS:
                if key in data:
                    updates[key] = str(data[key]).strip()
            source_url = updates.get("SOURCE_URL", self._values().get("SOURCE_URL", ""))
            if source_url:
                parsed_source = urlsplit(source_url)
                if (
                    parsed_source.scheme not in {"http", "https"}
                    or not parsed_source.hostname
                ):
                    raise ValueError("The source URL must be a valid HTTP or HTTPS URL")
            source_platform = updates.get(
                "SOURCE_PLATFORM", self._values().get("SOURCE_PLATFORM", "auto")
            ).lower()
            if not source_url:
                source_url = self._values().get(
                    "SOURCE_URL", _default_value("SOURCE_URL")
                )
            validate_source_platform(source_url, source_platform)
            platforms_raw = updates.get(
                "LIVE_PLATFORMS",
                self._values().get("LIVE_PLATFORMS", "youtube,facebook"),
            )
            platforms = [
                part.strip().lower()
                for part in platforms_raw.split(",")
                if part.strip()
            ]
            if not platforms or any(
                item not in {"youtube", "facebook", "tiktok", "custom"}
                for item in platforms
            ):
                raise ValueError(
                    "Choose YouTube, Facebook, TikTok, Custom RTMP, or any of them"
                )
            if "YOUTUBE_MODE" in updates and updates["YOUTUBE_MODE"] not in {
                "auto",
                "api",
                "static",
            }:
                raise ValueError("Choose auto, api, or static for the YouTube mode")
            if "YOUTUBE_CHANNEL_ID" in updates and not re.fullmatch(
                r"UC[A-Za-z0-9_-]{22}", updates["YOUTUBE_CHANNEL_ID"]
            ):
                raise ValueError(
                    "The YouTube channel ID should begin with UC and contain 24 characters"
                )
            if "YOUTUBE_PRIVACY_STATUS" in updates and updates[
                "YOUTUBE_PRIVACY_STATUS"
            ] not in {"public", "unlisted", "private"}:
                raise ValueError(
                    "Choose public, unlisted, or private for YouTube privacy"
                )
            if "YOUTUBE_CATEGORY_ID" in updates and updates[
                "YOUTUBE_CATEGORY_ID"
            ] not in {
                "10",
                "17",
                "20",
                "22",
                "23",
                "24",
                "25",
                "26",
                "27",
            }:
                raise ValueError("Choose a supported YouTube category")
            if "YOUTUBE_TITLE_OVERRIDE" in updates:
                title = " ".join(updates["YOUTUBE_TITLE_OVERRIDE"].split())
                if len(title) > 100:
                    raise ValueError(
                        "The YouTube title must be 100 characters or fewer"
                    )
                updates["YOUTUBE_TITLE_OVERRIDE"] = clean_title(title) if title else ""
            if "YOUTUBE_DESCRIPTION_OVERRIDE" in updates:
                description = updates["YOUTUBE_DESCRIPTION_OVERRIDE"]
                if len(description) > 5000:
                    raise ValueError(
                        "The description must be 5,000 characters or fewer"
                    )
                updates["YOUTUBE_DESCRIPTION_OVERRIDE"] = clean_description(description)
            if "YOUTUBE_TAGS_OVERRIDE" in updates:
                raw_tags = updates["YOUTUBE_TAGS_OVERRIDE"]
                if len(raw_tags) > 500:
                    raise ValueError("The keywords must be 500 characters or fewer")
                updates["YOUTUBE_TAGS_OVERRIDE"] = ", ".join(
                    clean_tags(
                        raw_tags.replace(";", ",")
                        .replace("،", ",")
                        .replace(chr(10), ",")
                        .split(",")
                    )
                )
            if "GEMINI_MODEL" in updates and not re.fullmatch(
                r"gemini-[A-Za-z0-9._-]+", updates["GEMINI_MODEL"]
            ):
                raise ValueError("Enter a valid Gemini model ID")
            if "METADATA_LANGUAGE" in updates and updates["METADATA_LANGUAGE"] not in {
                "ar",
                "en",
                "fr",
                "es",
                "auto",
            }:
                raise ValueError(
                    "Choose Arabic, English, French, Spanish, or automatic"
                )
            for key in (
                "GEMINI_METADATA_ENABLED",
                "LOGO_ENABLED",
                "FRAME_ENABLED",
                "ADAPTIVE_QUALITY",
                "PIPE_MODE",
                "YOUTUBE_BACKUP",
                "CHECK_FOR_UPDATES",
            ):
                if key in updates and updates[key].lower() not in {
                    "1",
                    "true",
                    "yes",
                    "on",
                    "0",
                    "false",
                    "no",
                    "off",
                }:
                    raise ValueError(f"{key} must be a boolean value")
            if "LOGO_POSITION" in updates and updates["LOGO_POSITION"] not in {
                "top-left",
                "top-right",
                "bottom-left",
                "bottom-right",
            }:
                raise ValueError("Choose a corner for the logo")
            if (
                "LOGO_SIZE_PERCENT" in updates
                and not updates["LOGO_SIZE_PERCENT"].isdigit()
            ):
                raise ValueError("Logo size must be a whole-number percentage")
            if (
                "LOGO_SIZE_PERCENT" in updates
                and not 5 <= int(updates["LOGO_SIZE_PERCENT"]) <= 25
            ):
                raise ValueError("Logo size must be between 5 and 25 percent")
            if "LOGO_OPACITY" in updates:
                try:
                    opacity = float(updates["LOGO_OPACITY"])
                except ValueError as exc:
                    raise ValueError("Logo opacity must be a number") from exc
                if not 0.1 <= opacity <= 1:
                    raise ValueError("Logo opacity must be between 0.1 and 1")
            if "FRAME_FILE" in updates and updates["FRAME_FILE"].strip():
                frame_file = resolve_project_path(updates["FRAME_FILE"])
                if frame_file.suffix.lower() != ".png" or not frame_file.is_file():
                    raise ValueError("FRAME_FILE must point to an existing PNG image")
            if "INITIAL_PROFILE" in updates and updates["INITIAL_PROFILE"] not in {
                "360p",
                "480p",
                "720p",
                "1080p",
            }:
                raise ValueError("Choose 360p, 480p, 720p, or 1080p")
            if "FACEBOOK_GRAPH_VERSION" in updates and not re.fullmatch(
                r"v[0-9]+\.[0-9]+", updates["FACEBOOK_GRAPH_VERSION"]
            ):
                raise ValueError("The Facebook Graph version should look like v26.0")
            if "TIKTOK_RTMP_URL" in updates and updates["TIKTOK_RTMP_URL"]:
                parsed_tiktok = urlsplit(updates["TIKTOK_RTMP_URL"])
                if (
                    parsed_tiktok.scheme not in {"rtmp", "rtmps"}
                    or not parsed_tiktok.hostname
                ):
                    raise ValueError(
                        "TIKTOK_RTMP_URL must be an RTMP URL, for example "
                        "rtmp://rtmp-push.tiktok.com/live"
                    )
            if "CUSTOM_RTMP_URL" in updates and updates["CUSTOM_RTMP_URL"]:
                parsed_custom = urlsplit(updates["CUSTOM_RTMP_URL"])
                if (
                    parsed_custom.scheme not in {"rtmp", "rtmps"}
                    or not parsed_custom.hostname
                ):
                    raise ValueError(
                        "CUSTOM_RTMP_URL must be an RTMP URL, for example "
                        "rtmp://live.restream.io/live"
                    )
            if "FFMPEG_PRESET" in updates and updates["FFMPEG_PRESET"] not in {
                "ultrafast",
                "superfast",
                "veryfast",
                "faster",
                "fast",
            }:
                raise ValueError("Choose a supported FFmpeg preset")
            for timing_key in (
                "RECONNECT_DELAY",
                "SOURCE_RETRY",
                "STALL_TIMEOUT",
                "HEARTBEAT",
                "SOURCE_END_GRACE",
            ):
                if timing_key in updates:
                    try:
                        int(updates[timing_key])
                    except ValueError as exc:
                        raise ValueError(
                            f"{timing_key} must be a whole number"
                        ) from exc
            if "UPDATE_REPO" in updates and updates["UPDATE_REPO"]:
                if not updater.REPO_PATTERN.match(updates["UPDATE_REPO"]):
                    raise ValueError("UPDATE_REPO must look like owner/repo")
            current_values = self._values()
            youtube_mode = updates.get(
                "YOUTUBE_MODE", current_values.get("YOUTUBE_MODE", "auto")
            ).lower()
            client_path = updates.get(
                "YOUTUBE_CLIENT_SECRETS_FILE",
                current_values.get("YOUTUBE_CLIENT_SECRETS_FILE", ""),
            )
            if (
                youtube_mode != "static"
                and client_path
                and not Path(client_path).expanduser().is_file()
            ):
                raise ValueError("The selected Google OAuth JSON file does not exist")
            # Mirror the load_settings() rules here, so a save that looks
            # successful can never produce settings that refuse to start.
            cookie_file = updates.get(
                "YTDLP_COOKIE_FILE",
                current_values.get("YTDLP_COOKIE_FILE", ""),
            ).strip()
            cookies_browser = updates.get(
                "YTDLP_COOKIES_FROM_BROWSER",
                current_values.get("YTDLP_COOKIES_FROM_BROWSER", ""),
            ).strip()
            if cookie_file and cookies_browser:
                raise ValueError(
                    "Use YTDLP_COOKIE_FILE or YTDLP_COOKIES_FROM_BROWSER, not both"
                )
            # Secrets reach ``updates`` only in the loop below, so read a
            # newly typed token from the request itself, else the saved one.
            facebook_token = (
                str(data.get("FACEBOOK_ACCESS_TOKEN", "")).strip()
                or current_values.get("FACEBOOK_ACCESS_TOKEN", "").strip()
            )
            facebook_target = updates.get(
                "FACEBOOK_TARGET_ID",
                current_values.get("FACEBOOK_TARGET_ID", "me"),
            ).strip()
            if facebook_token and not (
                facebook_target == "me"
                or re.fullmatch(r"[0-9]{6,30}", facebook_target)
            ):
                raise ValueError(
                    "FACEBOOK_TARGET_ID must be 'me' or a numeric Facebook "
                    "Page/Profile ID"
                )
            logo_enabled = _as_bool(
                updates.get(
                    "LOGO_ENABLED",
                    current_values.get("LOGO_ENABLED", "true"),
                )
            )
            logo_file_value = updates.get(
                "LOGO_FILE", current_values.get("LOGO_FILE", "")
            ).strip()
            if logo_enabled and logo_file_value:
                if not resolve_project_path(logo_file_value).is_file():
                    raise ValueError("LOGO_FILE must point to an existing image")
            for key in SECRET_KEYS:
                if key in data:
                    value = str(data[key]).strip()
                    if value:
                        updates[key] = value
                    elif data.get(f"clear_{key.lower()}"):
                        updates[key] = ""
            for key in CONFIG_KEYS + SECRET_KEYS:
                if key in updates:
                    set_key(str(self.env_path), key, updates[key], quote_mode="auto")
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
            load_dotenv(self.env_path, override=True)
            if updates.get("FACEBOOK_ACCESS_TOKEN", "").strip():
                self._write_token_meta(saved_at=time.time())
            return self.public_config()

    def _metadata_from_request(self, data: dict[str, Any]) -> StreamMetadata:
        raw_title = str(data.get("title", ""))
        title = " ".join(raw_title.split())
        if len(title) > 100:
            raise ValueError("The YouTube title must be 100 characters or fewer")
        raw_description = str(data.get("description", ""))
        if len(raw_description) > 5000:
            raise ValueError("The description must be 5,000 characters or fewer")
        raw_tags = data.get("tags", "")
        tag_text = (
            ",".join(str(tag) for tag in raw_tags)
            if isinstance(raw_tags, list)
            else str(raw_tags)
        )
        if len(tag_text) > 500:
            raise ValueError("The keywords must be 500 characters or fewer")
        if isinstance(raw_tags, list):
            tags = clean_tags(raw_tags)
        else:
            tag_text = (
                tag_text.replace(";", ",").replace("،", ",").replace(chr(10), ",")
            )
            tags = clean_tags(tag_text.split(","))
        values = self._values()
        return StreamMetadata(
            title=clean_title(title) if title else "",
            description=clean_description(raw_description),
            tags=tags,
            category_id=values.get("YOUTUBE_CATEGORY_ID", "22"),
            provider="manual",
        )

    def apply_metadata(self, data: dict[str, Any]) -> dict[str, Any]:
        metadata = self._metadata_from_request(data)
        updates = {
            "YOUTUBE_TITLE_OVERRIDE": metadata.title,
            "YOUTUBE_DESCRIPTION_OVERRIDE": metadata.description,
            "YOUTUBE_TAGS_OVERRIDE": ", ".join(metadata.tags),
        }
        with self._lock:
            running = self._is_running()
            if running and not metadata.title:
                raise ValueError("Enter a title, or choose automatic metadata")
            self.save_config(updates, allow_running=True)
            self.status.set_metadata_overrides(updates if running else None)
            if not running:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "حُفظت البيانات للبث القادم. اترك الحقول فارغة لاستخدام Gemini أو بيانات المصدر.",
                }
            session = self.status.destination_session
            if session is None:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "حُفظت البيانات؛ سيُطبّقها البرنامج عند توفر وجهة بث متصلة عبر API.",
                }
            results = update_destination_metadata(session, metadata, manual=True)
            updated = [name for name, result in results.items() if result == "updated"]
            if updated:
                self.status.update(
                    title=metadata.title,
                    description=metadata.description,
                    tags=list(metadata.tags),
                    metadata_provider="manual",
                    metadata_manual=True,
                )
                message = (
                    "حُفظت البيانات وطُبّقت على البث الحالي عبر الوجهات المتصلة بـ API."
                )
            else:
                message = "حُفظت البيانات للبث القادم؛ الوجهة الحالية تعمل بمفتاح ثابت ولا تسمح بتعديل العنوان مباشرةً."
            return {
                "ok": True,
                "saved": True,
                "applied_now": bool(updated),
                "results": results,
                "message": message,
            }

    def reset_metadata(self) -> dict[str, Any]:
        updates = {key: "" for key in METADATA_OVERRIDE_KEYS}
        with self._lock:
            running = self._is_running()
            self.save_config(updates, allow_running=True)
            self.status.set_metadata_overrides({} if running else None)
            if not running:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "أُزيل التخصيص اليدوي؛ سيعود Gemini أو بيانات المصدر في البث القادم.",
                }
            session = self.status.destination_session
            if session is None or session.base_metadata is None:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "أُزيل التخصيص للبث القادم؛ لا توجد وجهة API مفتوحة لتحديث البث الحالي.",
                }
            base_metadata = session.base_metadata
            results = update_destination_metadata(
                session,
                base_metadata,
                manual=False,
                base_metadata=base_metadata,
                disable_config_overrides=True,
            )
            updated = [name for name, result in results.items() if result == "updated"]
            if updated:
                self.status.update(
                    title=base_metadata.title,
                    description=base_metadata.description,
                    tags=list(base_metadata.tags),
                    metadata_provider=base_metadata.provider,
                    metadata_manual=False,
                )
            return {
                "ok": True,
                "saved": True,
                "applied_now": bool(updated),
                "results": results,
                "message": (
                    "عاد البث الحالي إلى بيانات Gemini أو المصدر، وأُزيل التخصيص للبث القادم."
                    if updated
                    else "أُزيل التخصيص للبث القادم؛ لم تقبل أي وجهة API التحديث الحالي."
                ),
            }

    def youtube_thumbnail_path(self) -> Path | None:
        return get_thumbnail_path(self._current_state_dir())

    def upload_youtube_thumbnail(self, data: bytes) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        thumbnail_path, info = save_thumbnail(state_dir, data)
        set_manual_hold(state_dir, True)
        self.status.update(thumbnail_provider="manual")
        set_manual_hold(state_dir, True)
        session = self.status.destination_session if self._is_running() else None
        results = (
            update_destination_thumbnail(session, thumbnail_path) if session else {}
        )
        applied = results.get("youtube") == "updated"
        self.status.update(thumbnail_ready=True)
        if applied:
            message = "حُفظت الصورة وطُبّقت على بث YouTube الحالي."
        elif results.get("youtube") == "YouTube API destination is not active":
            message = (
                "حُفظت الصورة؛ وضع المفتاح الثابت يتطلب رفعها يدوياً من YouTube Studio."
            )
        elif results:
            message = "حُفظت الصورة، لكن YouTube لم يقبل تطبيقها الآن؛ سيعيد البرنامج المحاولة في البث القادم."
        else:
            message = "حُفظت الصورة للبثوث القادمة التي ينشئها البرنامج عبر YouTube API."
        return {
            "ok": True,
            "saved": True,
            "applied_now": applied,
            "results": results,
            "message": message,
            **info,
        }

    def clear_youtube_thumbnail(self) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        clear_thumbnail(state_dir)
        set_manual_hold(state_dir, False)
        self.status.update(thumbnail_provider="", thumbnail_ready=False)
        return {
            "ok": True,
            "message": "حُذفت الصورة المصغرة المحفوظة؛ سيعيد Gemini توليدها تلقائياً إذا كانت مفعّلة.",
        }

    def verify_facebook_access(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("facebook",))
        if not settings.facebook_access_token:
            raise RuntimeError(
                "Save a Facebook User Access Token or Page Access Token before checking."
            )
        result = FacebookDestination(settings).verify_access()
        self._write_token_meta(last_verified_at=time.time())
        return result

    def verify_youtube_access(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("youtube",))
        if not settings.youtube_api_enabled:
            raise RuntimeError(
                "Select YouTube API mode before checking the linked YouTube account."
            )
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Select a Google OAuth JSON file before checking YouTube."
            )
        if not (settings.state_dir / "youtube-token.json").is_file():
            raise ValueError(
                "لم يتم ربط YouTube بعد على هذا الجهاز. اضغط «ربط YouTube» أولاً."
            )
        return YouTubeDestination(settings).verify_access()

    def youtube_broadcast_status(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("youtube",))
        if not settings.youtube_api_enabled:
            raise RuntimeError(
                "Select YouTube API mode before checking the linked YouTube account."
            )
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Select a Google OAuth JSON file before checking YouTube."
            )
        if not (settings.state_dir / "youtube-token.json").is_file():
            raise ValueError(
                "لم يتم ربط YouTube بعد على هذا الجهاز. اضغط «ربط YouTube» أولاً."
            )
        destination = YouTubeDestination(settings)
        destination.authenticate(allow_browser=False)
        return destination.check_status()

    def preflight(self) -> dict[str, Any]:
        load_dotenv(self.env_path, override=True)
        config = self.public_config()
        missing = config["missing"]
        if missing:
            raise RuntimeError("الإعداد غير جاهز: " + " ".join(missing))
        if config.get("notes"):
            logging.warning(
                "Manual fallback mode active: %s", " ".join(config["notes"])
            )
        settings = load_settings()
        ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
        ytdlp = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        state_dir = settings.state_dir
        youtube_needs_auth = (
            "youtube" in settings.platforms
            and settings.youtube_api_enabled
            and not (state_dir / "youtube-token.json").is_file()
        )
        return {
            "ok": True,
            "ffmpeg": Path(ffmpeg).name,
            "yt_dlp": ytdlp,
            "platforms": list(settings.platforms),
            "youtube_needs_auth": youtube_needs_auth,
            "modes": {
                "youtube": settings.effective_youtube_mode.upper(),
                "facebook": "API" if settings.facebook_access_token else "Static key",
            },
        }

    def authorize_youtube(self) -> None:
        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        YouTubeDestination(settings).authenticate()

    def youtube_manual_auth_url(self) -> dict[str, str]:
        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        url = YouTubeDestination(settings).manual_auth_url()
        return {"authorization_url": url}

    def youtube_manual_auth_exchange(self, code: str) -> None:
        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        YouTubeDestination(settings).manual_auth_exchange(code)

    # -- Automatic updates -------------------------------------------------

    def _update_state_dir(self) -> Path:
        return self._current_state_dir()

    def update_status(self) -> dict[str, Any]:
        values = self._values()
        repo = values.get("UPDATE_REPO", "").strip()
        state = updater.read_update_state(self._update_state_dir())
        latest = state.get("latest")
        if not isinstance(latest, dict):
            latest = None
        staged = state.get("staged")
        if not isinstance(staged, dict):
            staged = None
        return {
            "current_version": __version__,
            "repo": repo,
            "enabled": _as_bool(values.get("CHECK_FOR_UPDATES", "true")),
            "configured": bool(repo),
            "last_check": state.get("last_check"),
            "latest": latest,
            "update_available": bool(
                latest
                and updater.is_newer(str(latest.get("version") or ""), __version__)
            ),
            "staged": staged,
            "last_applied_version": state.get("last_applied_version"),
        }

    def check_for_updates(self, force: bool = False) -> dict[str, Any]:
        """Check GitHub for a newer release; throttled to once a day.

        Only *reports* an available update. Nothing is downloaded here.
        Returns the update status plus ``checked`` and a ``reason`` when
        the check was skipped (``disabled``, ``no_repo``, ``throttled``).
        """
        with self._lock:
            status = self.update_status()
            if not status["enabled"]:
                return {**status, "checked": False, "reason": "disabled"}
            if not status["configured"]:
                return {**status, "checked": False, "reason": "no_repo"}
            state_dir = self._update_state_dir()
            if not force and not updater.due_for_check(state_dir):
                return {**status, "checked": False, "reason": "throttled"}
            release = updater.fetch_latest_release(status["repo"])
            if release is None:
                # A failed check must not wipe the previously known latest
                # release, and must not consume the day's throttle: the
                # update stays visible and the next check is not delayed.
                return {**status, "checked": False, "reason": "fetch_failed"}
            updater.write_update_state(
                state_dir, last_check=time.time(), latest=release
            )
            return {**self.update_status(), "checked": True}

    def download_update(self) -> dict[str, Any]:
        """Download the latest release zip and stage it for the next start."""
        with self._lock:
            status = self.update_status()
            if not status["configured"]:
                raise RuntimeError(
                    "Set UPDATE_REPO to owner/repo in the dashboard first, "
                    "then check for updates"
                )
            latest = status["latest"]
            if not latest or not updater.is_newer(
                str(latest.get("version") or ""), __version__
            ):
                latest = self.check_for_updates(force=True).get("latest")
            if not latest or not updater.is_newer(
                str(latest.get("version") or ""), __version__
            ):
                raise RuntimeError("No newer release is available to download")
            zipball_url = str(latest.get("zipball_url") or "")
            if not zipball_url:
                raise RuntimeError("The latest release has no downloadable archive")
            state_dir = self._update_state_dir()
            zip_path = (
                state_dir
                / "updates"
                / f"live-relay-{latest['version']}.zip"
            )
            updater.download_release_zip(zipball_url, zip_path)
            updater.stage_update(state_dir, latest, zip_path)
            return self.update_status()

    def install_update(self) -> dict[str, Any]:
        """Stage the update (downloading it first if needed) and restart.

        The relay must be stopped first. The dashboard process exits with
        the update exit code once this response is sent; the launcher
        script applies the staged files and starts the new version.
        """
        with self._lock:
            if self._is_running():
                raise RuntimeError("Stop the relay before installing an update")
            status = self.update_status()
            if not status["staged"]:
                status = self.download_update()
            staged = status["staged"]
            if not staged:
                raise RuntimeError("No update is staged to install")
            self.update_restart_requested = True
            if self._shutdown_callback is not None:
                threading.Timer(0.5, self._shutdown_callback).start()
            return {
                **self.update_status(),
                "restart_required": True,
                "version": staged["version"],
            }

    def start_relay(self) -> dict[str, Any]:
        with self._lock:
            if self._is_running():
                raise RuntimeError("The relay is already running")
            load_dotenv(self.env_path, override=True)
            missing = self.public_config()["missing"]
            if missing:
                raise RuntimeError("الإعداد غير جاهز: " + " ".join(missing))
            settings = load_settings()
            resolve_ffmpeg(settings.ffmpeg_path)
            if "youtube" in settings.platforms and settings.youtube_api_enabled:
                if not (settings.state_dir / "youtube-token.json").is_file():
                    raise RuntimeError(
                        "Authorize the YouTube account first using the YouTube authorization button"
                    )
            self.status.set_destination_session(None)
            self.status.set_metadata_overrides(None)
            self._stop_event = threading.Event()
            self.status.update(
                mode="starting",
                message="Starting relay",
                title="",
                description="",
                tags=[],
                metadata_provider="",
                metadata_manual=False,
                quality=settings.initial_profile,
                bitrate="",
                started_at=time.time(),
                retry_count=0,
                last_error="",
                active_platforms=[],
                destination_errors={},
            )
            self._relay_thread = threading.Thread(
                target=self._run_relay,
                args=(settings, self._stop_event),
                daemon=True,
                name="live-relay",
            )
            self._relay_thread.start()
            return self.get_status()

    def _run_relay(self, settings, stopped: threading.Event) -> None:
        try:
            run(settings, stopped, self.status)
        except Exception as exc:
            message = redact_urls(str(exc).replace("\n", " "))
            for secret in (
                getattr(settings, "youtube_static_key", ""),
                getattr(settings, "facebook_access_token", ""),
                getattr(settings, "facebook_static_key", ""),
                getattr(settings, "tiktok_static_key", ""),
                getattr(settings, "custom_stream_key", ""),
            ):
                if secret:
                    message = message.replace(secret, "[secret hidden]")
            message = message[:400]
            logging.error("Relay failed (%s): %s", type(exc).__name__, message)
            self.status.set_destination_session(None)
            self.status.update(
                mode="error",
                message="Relay stopped because of an error",
                last_error=message,
                started_at=None,
                metadata_manual=False,
                active_platforms=[],
                destination_errors={},
            )
        else:
            # Clean end (the user stopped it, or the source ended): never
            # leave the dashboard showing a stale "starting"/"stopping" mode.
            self.status.set_destination_session(None)
            self.status.update(
                mode="stopped",
                message="Relay stopped",
                started_at=None,
                active_platforms=[],
            )

    def stop_relay(self, timeout: float = 30.0) -> dict[str, Any]:
        """Signal the relay to stop and wait until it has torn down.

        The old implementation returned as soon as the stop event was set,
        so an immediate restart raced with the old thread still holding
        the encoder and the destinations (and "already running" errors).
        The join happens outside the controller lock, so a slow teardown
        can never block a concurrent status/config read. When the thread
        does not die within ``timeout`` seconds the call still returns,
        but reports ``stop_timed_out`` so the dashboard can say so
        instead of pretending the relay is stopped.
        """
        with self._lock:
            if not self._is_running() or self._stop_event is None:
                return self.get_status()
            thread = self._relay_thread
            self.status.update(mode="stopping", message="Stopping relay safely")
            self._stop_event.set()
        assert thread is not None
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        with self._lock:
            if stopped:
                self.status.update(mode="stopped", message="Relay stopped")
            result = self.get_status()
        result["stopped"] = stopped
        result["stop_timed_out"] = not stopped
        return result

    def get_status(self) -> dict[str, Any]:
        data = self.status.snapshot()
        data["is_running"] = self._is_running()
        return data

    def get_logs(self, limit: int = 120) -> list[str]:
        values = self._values()
        log_path = self._current_state_dir(values) / "live-relay.log"
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-limit:]
        except OSError:
            return []


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_handler_class(
    controller: DashboardController, html_path: Path
) -> type[BaseHTTPRequestHandler]:
    """HTTP handler for the local dashboard, bound to one controller instance."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "LiveRelayDashboard/1"

        def _host_is_local(self) -> bool:
            host = self.headers.get("Host", "")
            hostname = urlsplit(f"//{host}").hostname
            return hostname in {"127.0.0.1", "localhost", "::1"}

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _authorized(self) -> bool:
            if not self._host_is_local():
                self._json(
                    {"error": "The dashboard only accepts local connections."}, 403
                )
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host', '')}":
                self._json({"error": "Cross-origin requests are not allowed."}, 403)
                return False
            if not secrets.compare_digest(
                self.headers.get("X-Relay-Token", ""), controller.control_token
            ):
                self._json(
                    {"error": "Invalid local control token. Reload the dashboard."}, 403
                )
                return False
            return True

        def do_GET(self) -> None:
            try:
                self._do_get()
            except Exception as exc:  # pragma: no cover - safety net
                logging.exception("Dashboard GET request failed: %s", exc)
                try:
                    self._json({"error": "The dashboard could not answer."}, 500)
                except Exception:
                    pass

        def _do_get(self) -> None:
            if not self._host_is_local():
                self._json(
                    {"error": "The dashboard only accepts local connections."}, 403
                )
                return
            request_path = urlsplit(self.path).path
            if request_path == "/":
                try:
                    html = html_path.read_text(encoding="utf-8").replace(
                        "__CONTROL_TOKEN__", controller.control_token
                    )
                except OSError:
                    self._json({"error": "Dashboard page is missing."}, 500)
                    return
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            elif request_path == "/api/status":
                if not self._authorized():
                    return
                self._json(controller.get_status())
            elif request_path == "/api/config":
                if not self._authorized():
                    return
                self._json(controller.public_config())
            elif request_path == "/api/logs":
                if not self._authorized():
                    return
                self._json({"lines": controller.get_logs()})
            elif request_path == "/api/update":
                if not self._authorized():
                    return
                self._json(controller.update_status())
            elif request_path == "/api/youtube/thumbnail":
                if not self._authorized():
                    return
                thumbnail_path = controller.youtube_thumbnail_path()
                if thumbnail_path is None:
                    self._json({"error": "No saved YouTube thumbnail."}, 404)
                    return
                try:
                    content = thumbnail_path.read_bytes()
                except OSError:
                    self._json({"error": "The saved thumbnail could not be read."}, 500)
                    return
                mime_type = (
                    "image/png"
                    if thumbnail_path.suffix.lower() == ".png"
                    else "image/jpeg"
                )
                self._send(content, mime_type)
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            try:
                request_path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                limit = (
                    MAX_THUMBNAIL_SIZE
                    if request_path == "/api/youtube/thumbnail"
                    else 65536
                )
                if length < 0 or length > limit:
                    self._json({"error": "حجم الطلب أكبر من الحد المسموح."}, 413)
                    return
                raw = self.rfile.read(length) if length else b"{}"
                if len(raw) != length:
                    raise ValueError("لم يكتمل رفع الملف؛ أعد المحاولة.")
                if request_path == "/api/youtube/thumbnail":
                    self._json(controller.upload_youtube_thumbnail(raw))
                    return
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Request body must be a JSON object")
                if request_path == "/api/config":
                    self._json(controller.save_config(data))
                elif request_path == "/api/metadata/apply":
                    self._json(controller.apply_metadata(data))
                elif request_path == "/api/metadata/reset":
                    self._json(controller.reset_metadata())
                elif request_path == "/api/check":
                    self._json(controller.preflight())
                elif request_path == "/api/youtube/authorize":
                    controller.authorize_youtube()
                    self._json(
                        {
                            "ok": True,
                            "message": "YouTube account authorized on this computer.",
                        }
                    )
                elif request_path == "/api/youtube/manual-auth/url":
                    self._json(controller.youtube_manual_auth_url())
                elif request_path == "/api/youtube/manual-auth/exchange":
                    controller.youtube_manual_auth_exchange(str(data.get("code", "")))
                    self._json(
                        {
                            "ok": True,
                            "message": "YouTube account authorized with the manual code.",
                        }
                    )
                elif request_path == "/api/youtube/verify":
                    self._json({"ok": True, **controller.verify_youtube_access()})
                elif request_path == "/api/youtube/status":
                    self._json({"ok": True, **controller.youtube_broadcast_status()})
                elif request_path == "/api/facebook/verify":
                    self._json({"ok": True, **controller.verify_facebook_access()})
                elif request_path == "/api/update/check":
                    self._json(controller.check_for_updates(force=True))
                elif request_path == "/api/update/download":
                    self._json(controller.download_update())
                elif request_path == "/api/update/install":
                    self._json(controller.install_update())
                elif request_path == "/api/start":
                    self._json(controller.start_relay(), 202)
                elif request_path == "/api/stop":
                    self._json(controller.stop_relay())
                else:
                    self._json({"error": "Not found"}, 404)
            except (
                ValueError,
                RuntimeError,
                OSError,
                subprocess.SubprocessError,
            ) as exc:
                self._json({"error": redact_urls(str(exc))[:400]}, 400)
            except Exception as exc:
                logging.exception(
                    "Dashboard request failed: %s", redact_urls(str(exc))[:300]
                )
                self._json(
                    {
                        "error": "The request failed. Check the dashboard log for details."
                    },
                    500,
                )

        def do_DELETE(self) -> None:
            if not self._authorized():
                return
            try:
                request_path = urlsplit(self.path).path
                if request_path == "/api/youtube/thumbnail":
                    self._json(controller.clear_youtube_thumbnail())
                    return
                self._json({"error": "Not found"}, 404)
            except Exception as exc:
                self._json(
                    {"error": redact_urls(str(exc))[:300]},
                    500,
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def run_dashboard(
    project_root: Path = PROJECT_ROOT, port: int = 8765, open_browser: bool = True
) -> int:
    """Run the dashboard; returns the update exit code when an install
    was requested, so the launcher can apply it and restart, else 0."""
    controller = DashboardController(project_root)
    values = controller._values()
    state_dir = controller._current_state_dir(values)
    ensure_private_dir(state_dir)
    log_level = values.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                state_dir / "live-relay.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )
    html_path = Path(__file__).with_name("dashboard.html")

    Handler = build_handler_class(controller, html_path)

    server: DashboardServer | None = None
    requested_port = max(0, min(int(port), 65535))
    try:
        server = DashboardServer(("127.0.0.1", requested_port), Handler)
    except OSError:
        if requested_port == 0:
            raise
        logging.warning(
            "Dashboard port %s is busy; selecting an available local port",
            requested_port,
        )
        server = DashboardServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logging.info("Local dashboard: %s", url)
    controller._shutdown_callback = server.shutdown

    def _auto_update_check() -> None:
        # Never block startup on the network, and never download here:
        # the automatic check only reports that a release exists.
        time.sleep(5)
        try:
            controller.check_for_updates()
        except Exception:
            logging.exception("Automatic update check failed")

    threading.Thread(
        target=_auto_update_check, name="update-check", daemon=True
    ).start()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logging.info("Dashboard shutdown requested")
    finally:
        controller.stop_relay()
        relay_thread = controller._relay_thread
        if relay_thread:
            relay_thread.join(timeout=70)
        server.server_close()
    if controller.update_restart_requested:
        logging.info("Dashboard exiting to install the staged update")
        return updater.UPDATE_RESTART_EXIT_CODE
    return 0
