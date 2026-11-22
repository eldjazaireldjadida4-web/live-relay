from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .quality import PROFILES
from .source_platforms import validate_source_platform


@dataclass(frozen=True)
class Settings:
    source_url: str
    source_platform: str
    platforms: tuple[str, ...]
    format_selector: str
    cookie_file: str
    cookies_from_browser: str
    pipe_mode: bool
    ffmpeg_path: str
    ffmpeg_preset: str
    youtube_client_secrets: str
    youtube_channel_id: str
    youtube_mode: str
    youtube_static_key: str
    youtube_backup: bool
    youtube_quota_budget: int
    youtube_manual_auth: bool
    youtube_manual_auth_code: str
    youtube_privacy: str
    youtube_category_id: str
    youtube_title_override: str
    youtube_description_override: str
    youtube_tags_override: str
    facebook_access_token: str
    facebook_target_id: str
    facebook_graph_version: str
    facebook_static_key: str
    # TikTok has no public API to create a live (its Live API is
    # invitation/partner-only), so TikTok is a static RTMP destination
    # only: the user creates the live on TikTok and pastes the Server
    # URL + stream key TikTok shows for that session.
    tiktok_rtmp_url: str
    tiktok_static_key: str
    # Generic RTMP destination (Restream bridge): Restream is a TikTok
    # partner, so one push to its permanent RTMP ingest lets Restream
    # create the TikTok live automatically — no per-session TikTok key.
    custom_rtmp_url: str
    custom_stream_key: str
    custom_rtmp_name: str
    gemini_api_key: str
    gemini_model: str
    gemini_metadata_enabled: bool
    gemini_thumbnail_enabled: bool
    gemini_image_model: str
    metadata_language: str
    notify_telegram_token: str
    notify_telegram_chat: str
    notify_webhook_url: str
    notify_language: str
    record_enabled: bool
    logo_enabled: bool
    logo_file: str
    logo_position: str
    logo_size_percent: int
    logo_opacity: float
    frame_enabled: bool
    frame_file: str
    initial_profile: str
    adaptive_quality: bool
    reconnect_delay: int
    source_retry: int
    stall_timeout: int
    heartbeat: int
    source_end_grace: int
    log_level: str
    check_for_updates: bool
    update_repo: str
    state_dir: Path

    @property
    def effective_youtube_mode(self) -> str:
        if self.youtube_mode == "auto":
            return "api" if self.youtube_client_secrets else "static"
        return self.youtube_mode

    @property
    def youtube_api_enabled(self) -> bool:
        return self.effective_youtube_mode == "api"

    @property
    def metadata_api_enabled(self) -> bool:
        return ("youtube" in self.platforms and self.youtube_api_enabled) or (
            "facebook" in self.platforms and bool(self.facebook_access_token)
        )


def default_state_dir() -> Path:
    if os.name == "nt":
        return (
            Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming")))
            / "live-relay"
        )
    return Path.home() / ".config" / "live-relay"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def resolve_project_root() -> Path:
    working_dir = Path.cwd().resolve()
    if (working_dir / ".env").is_file() or (working_dir / ".env.example").is_file():
        return working_dir
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / ".env.example").is_file():
        return source_root
    return working_dir


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return (
        path.resolve()
        if path.is_absolute()
        else (resolve_project_root() / path).resolve()
    )


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def load_settings(platforms_override: tuple[str, ...] | None = None) -> Settings:
    if platforms_override is None:
        raw_platforms = os.getenv("LIVE_PLATFORMS", "youtube,facebook")
    else:
        raw_platforms = ",".join(platforms_override)
    platforms = tuple(
        dict.fromkeys(
            part.strip().lower() for part in raw_platforms.split(",") if part.strip()
        )
    )
    if not platforms or any(
        item not in {"youtube", "facebook", "tiktok", "custom"}
        for item in platforms
    ):
        raise ValueError(
            "LIVE_PLATFORMS must be a comma-separated list of youtube, "
            "facebook, tiktok, and/or custom"
        )
    source_url = os.getenv(
        "SOURCE_URL",
        "https://www.youtube.com/channel/UCFmnqispHbyVZKRM6RcybWQ/live",
    ).strip()
    parsed_source = urlsplit(source_url)
    if parsed_source.scheme not in {"http", "https"} or not parsed_source.hostname:
        raise ValueError("SOURCE_URL must be a valid HTTP or HTTPS URL")
    source_platform = os.getenv("SOURCE_PLATFORM", "auto").strip().lower()
    validate_source_platform(source_url, source_platform)
    initial = os.getenv("INITIAL_PROFILE", "720p").strip().lower()
    if initial not in {profile.name for profile in PROFILES}:
        raise ValueError("INITIAL_PROFILE must be 360p, 480p, 720p, or 1080p")
    privacy = os.getenv("YOUTUBE_PRIVACY_STATUS", "public").strip().lower()
    if privacy not in {"public", "unlisted", "private"}:
        raise ValueError("YOUTUBE_PRIVACY_STATUS must be public, unlisted, or private")
    youtube_category_id = os.getenv("YOUTUBE_CATEGORY_ID", "22").strip()
    if youtube_category_id not in {
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
        raise ValueError("YOUTUBE_CATEGORY_ID must be one of the supported categories")
    youtube_title_override = os.getenv("YOUTUBE_TITLE_OVERRIDE", "").strip()
    if len(" ".join(youtube_title_override.split())) > 100:
        raise ValueError("YOUTUBE_TITLE_OVERRIDE must be at most 100 characters")
    youtube_description_override = os.getenv("YOUTUBE_DESCRIPTION_OVERRIDE", "").strip()
    if len(youtube_description_override) > 5000:
        raise ValueError("YOUTUBE_DESCRIPTION_OVERRIDE must be at most 5000 characters")
    youtube_tags_override = os.getenv("YOUTUBE_TAGS_OVERRIDE", "").strip()
    if len(youtube_tags_override) > 500:
        raise ValueError("YOUTUBE_TAGS_OVERRIDE must be at most 500 characters")
    youtube_client_secrets = os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "").strip()
    youtube_mode = os.getenv("YOUTUBE_MODE", "auto").strip().lower()
    youtube_static_key = os.getenv("YOUTUBE_STREAM_KEY", "").strip()
    youtube_manual_auth = _flag("YOUTUBE_MANUAL_AUTH")
    youtube_manual_auth_code = os.getenv("YOUTUBE_MANUAL_AUTH_CODE", "").strip()
    if youtube_mode not in {"auto", "api", "static"}:
        raise ValueError("YOUTUBE_MODE must be auto, api, or static")
    youtube_api_enabled = youtube_mode == "api" or (
        youtube_mode == "auto" and bool(youtube_client_secrets)
    )
    youtube_channel_id = os.getenv(
        "YOUTUBE_CHANNEL_ID", "UCioeMW7bZNeyV48Z09q2vTg"
    ).strip()
    if "youtube" in platforms:
        if youtube_api_enabled:
            if not youtube_client_secrets:
                raise ValueError(
                    "YOUTUBE_MODE=api requires YOUTUBE_CLIENT_SECRETS_FILE"
                )
            if not Path(youtube_client_secrets).expanduser().is_file():
                raise ValueError(
                    "YOUTUBE_CLIENT_SECRETS_FILE does not point to an existing JSON file"
                )
            if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", youtube_channel_id):
                raise ValueError(
                    "YOUTUBE_CHANNEL_ID must be a 24-character YouTube channel ID"
                )
        elif not youtube_static_key:
            raise ValueError("YouTube static mode requires YOUTUBE_STREAM_KEY")

    facebook_token = os.getenv("FACEBOOK_ACCESS_TOKEN", "").strip()
    facebook_static_key = os.getenv("FACEBOOK_STREAM_KEY", "").strip()
    facebook_target = os.getenv("FACEBOOK_TARGET_ID", "me").strip()
    facebook_version = os.getenv("FACEBOOK_GRAPH_VERSION", "v26.0").strip()
    if "facebook" in platforms and not (facebook_token or facebook_static_key):
        raise ValueError("Configure FACEBOOK_ACCESS_TOKEN or FACEBOOK_STREAM_KEY")
    if facebook_token and not (
        facebook_target == "me" or re.fullmatch(r"[0-9]{6,30}", facebook_target)
    ):
        raise ValueError(
            "FACEBOOK_TARGET_ID must be 'me' or a numeric Facebook Page/Profile ID"
        )
    if not re.fullmatch(r"v[0-9]+\.[0-9]+", facebook_version):
        raise ValueError("FACEBOOK_GRAPH_VERSION must look like v26.0")
    # TikTok: paste the exact Server URL TikTok shows for the session.
    # The default is the ingest host most commonly reported for TikTok
    # RTMP; TikTok picks a regional host per session, so always prefer
    # the URL it displays over this default.
    tiktok_rtmp_url = os.getenv(
        "TIKTOK_RTMP_URL", "rtmp://rtmp-push.tiktok.com/live"
    ).strip()
    tiktok_static_key = os.getenv("TIKTOK_STREAM_KEY", "").strip()
    if "tiktok" in platforms:
        parsed_tiktok = urlsplit(tiktok_rtmp_url)
        if (
            parsed_tiktok.scheme not in {"rtmp", "rtmps"}
            or not parsed_tiktok.hostname
        ):
            raise ValueError(
                "TIKTOK_RTMP_URL must be an RTMP URL, for example "
                "rtmp://rtmp-push.tiktok.com/live"
            )
        if not tiktok_static_key:
            raise ValueError(
                "TikTok requires TIKTOK_STREAM_KEY (and TIKTOK_RTMP_URL): "
                "create the live on TikTok first and paste the Server URL "
                "and stream key it shows for that session"
            )
    # Restream bridge: its RTMP URL + key are permanent for the account
    # (unlike TikTok's per-session key), so paste them once and every
    # relay start goes live on TikTok through Restream automatically.
    custom_rtmp_url = os.getenv(
        "CUSTOM_RTMP_URL", "rtmp://live.restream.io/live"
    ).strip()
    custom_stream_key = os.getenv("CUSTOM_STREAM_KEY", "").strip()
    custom_rtmp_name = os.getenv("CUSTOM_RTMP_NAME", "Restream").strip()
    if "custom" in platforms:
        parsed_custom = urlsplit(custom_rtmp_url)
        if (
            parsed_custom.scheme not in {"rtmp", "rtmps"}
            or not parsed_custom.hostname
        ):
            raise ValueError(
                "CUSTOM_RTMP_URL must be an RTMP URL, for example "
                "rtmp://live.restream.io/live"
            )
        if not custom_stream_key:
            raise ValueError(
                "Custom RTMP requires CUSTOM_STREAM_KEY: copy the RTMP URL "
                "and stream key from Restream (Settings > RTMP) and paste "
                "them once — they are permanent for the account"
            )
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", gemini_model):
        raise ValueError("GEMINI_MODEL must be a valid Gemini model ID")
    gemini_metadata_enabled = _flag("GEMINI_METADATA_ENABLED", True)
    gemini_thumbnail_enabled = _flag("GEMINI_THUMBNAIL_ENABLED", True)
    gemini_image_model = os.getenv(
        "GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1"
    ).strip()
    if not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", gemini_image_model):
        raise ValueError("GEMINI_IMAGE_MODEL must be a valid Gemini model ID")
    metadata_language = os.getenv("METADATA_LANGUAGE", "ar").strip().lower()
    if metadata_language not in {"ar", "en", "fr", "es", "auto"}:
        raise ValueError("METADATA_LANGUAGE must be ar, en, fr, es, or auto")
    logo_enabled = _flag("LOGO_ENABLED", True)
    configured_logo = os.getenv("LOGO_FILE", "").strip()
    project_logo = resolve_project_path("assets/live-relay-logo.png")
    packaged_logo = Path(__file__).with_name("stream-logo.png")
    logo_file = (
        resolve_project_path(configured_logo)
        if configured_logo
        else project_logo
        if project_logo.is_file()
        else packaged_logo
    )
    logo_position = os.getenv("LOGO_POSITION", "bottom-right").strip().lower()
    if logo_position not in {"top-left", "top-right", "bottom-left", "bottom-right"}:
        raise ValueError(
            "LOGO_POSITION must be top-left, top-right, bottom-left, or bottom-right"
        )
    try:
        logo_size_percent = int(os.getenv("LOGO_SIZE_PERCENT", "12"))
        logo_opacity = float(os.getenv("LOGO_OPACITY", "0.9"))
    except ValueError as exc:
        raise ValueError("LOGO_SIZE_PERCENT and LOGO_OPACITY must be numeric") from exc
    if not 5 <= logo_size_percent <= 25:
        raise ValueError("LOGO_SIZE_PERCENT must be between 5 and 25")
    if not 0.1 <= logo_opacity <= 1.0:
        raise ValueError("LOGO_OPACITY must be between 0.1 and 1")
    if logo_enabled and not logo_file.is_file():
        raise ValueError("LOGO_FILE does not point to an existing image")
    frame_enabled = _flag("FRAME_ENABLED", False)
    configured_frame = os.getenv("FRAME_FILE", "").strip()
    frame_file = resolve_project_path(configured_frame) if configured_frame else None
    if frame_enabled and frame_file is not None:
        if frame_file.suffix.lower() != ".png" or not frame_file.is_file():
            raise ValueError("FRAME_FILE must point to an existing PNG image")
    notify_telegram_token = os.getenv("NOTIFY_TELEGRAM_TOKEN", "").strip()
    notify_telegram_chat = os.getenv("NOTIFY_TELEGRAM_CHAT", "").strip()
    notify_webhook_url = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
    if notify_telegram_token and not notify_telegram_chat:
        raise ValueError("NOTIFY_TELEGRAM_TOKEN requires NOTIFY_TELEGRAM_CHAT")
    if notify_telegram_chat and not notify_telegram_token:
        raise ValueError("NOTIFY_TELEGRAM_CHAT requires NOTIFY_TELEGRAM_TOKEN")
    if notify_webhook_url and not re.fullmatch(r"https?://[^\s]+", notify_webhook_url):
        raise ValueError("NOTIFY_WEBHOOK_URL must be an HTTP or HTTPS URL")
    notify_language = os.getenv("NOTIFY_LANGUAGE", metadata_language).strip().lower()
    if notify_language not in {"ar", "en"}:
        raise ValueError("NOTIFY_LANGUAGE must be ar or en")
    record_enabled = _flag("RECORD_ENABLED")
    try:
        youtube_quota_budget = int(os.getenv("YOUTUBE_QUOTA_BUDGET", "10000"))
    except ValueError as exc:
        raise ValueError("YOUTUBE_QUOTA_BUDGET must be a whole number") from exc
    try:
        reconnect_delay = int(os.getenv("RECONNECT_DELAY", "5"))
        source_retry = int(os.getenv("SOURCE_RETRY", "30"))
        stall_timeout = int(os.getenv("STALL_TIMEOUT", "45"))
        heartbeat = int(os.getenv("HEARTBEAT", "30"))
        source_end_grace = int(os.getenv("SOURCE_END_GRACE", "180"))
    except ValueError as exc:
        raise ValueError(
            "RECONNECT_DELAY, SOURCE_RETRY, STALL_TIMEOUT, HEARTBEAT and "
            "SOURCE_END_GRACE must be whole numbers"
        ) from exc
    preset = os.getenv("FFMPEG_PRESET", "veryfast").strip().lower()
    if preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast"}:
        raise ValueError(
            "FFMPEG_PRESET must be ultrafast, superfast, veryfast, faster, or fast"
        )
    if (
        os.getenv("YTDLP_COOKIE_FILE", "").strip()
        and os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    ):
        raise ValueError(
            "Use YTDLP_COOKIE_FILE or YTDLP_COOKIES_FROM_BROWSER, not both"
        )
    state_dir = Path(
        os.getenv("LIVE_RELAY_STATE_DIR") or str(default_state_dir())
    ).expanduser()
    check_for_updates = _flag("CHECK_FOR_UPDATES", True)
    update_repo = os.getenv(
        "UPDATE_REPO", "eldjazaireldjadida4-web/live-relay"
    ).strip()
    if update_repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", update_repo):
        raise ValueError("UPDATE_REPO must look like owner/repo")
    return Settings(
        source_url=source_url,
        source_platform=source_platform,
        platforms=platforms,
        format_selector=os.getenv(
            "YTDLP_FORMAT", "b[height<=720]/bv*[height<=720]+ba/best"
        ),
        cookie_file=os.getenv("YTDLP_COOKIE_FILE", "").strip(),
        cookies_from_browser=os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip(),
        pipe_mode=_flag("PIPE_MODE"),
        ffmpeg_path=os.getenv("FFMPEG", "").strip(),
        ffmpeg_preset=preset,
        youtube_client_secrets=youtube_client_secrets,
        youtube_channel_id=youtube_channel_id,
        youtube_mode=youtube_mode,
        youtube_static_key=youtube_static_key,
        youtube_backup=_flag("YOUTUBE_BACKUP"),
        youtube_quota_budget=max(100, youtube_quota_budget),
        youtube_manual_auth=youtube_manual_auth,
        youtube_manual_auth_code=youtube_manual_auth_code,
        youtube_privacy=privacy,
        youtube_category_id=youtube_category_id,
        youtube_title_override=youtube_title_override,
        youtube_description_override=youtube_description_override,
        youtube_tags_override=youtube_tags_override,
        facebook_access_token=facebook_token,
        facebook_target_id=facebook_target,
        facebook_graph_version=facebook_version,
        facebook_static_key=facebook_static_key,
        tiktok_rtmp_url=tiktok_rtmp_url,
        tiktok_static_key=tiktok_static_key,
        custom_rtmp_url=custom_rtmp_url,
        custom_stream_key=custom_stream_key,
        custom_rtmp_name=custom_rtmp_name,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        gemini_metadata_enabled=gemini_metadata_enabled,
        gemini_thumbnail_enabled=gemini_thumbnail_enabled,
        gemini_image_model=gemini_image_model,
        metadata_language=metadata_language,
        notify_telegram_token=notify_telegram_token,
        notify_telegram_chat=notify_telegram_chat,
        notify_webhook_url=notify_webhook_url,
        notify_language=notify_language,
        record_enabled=record_enabled,
        logo_enabled=logo_enabled,
        logo_file=str(logo_file),
        logo_position=logo_position,
        logo_size_percent=logo_size_percent,
        logo_opacity=logo_opacity,
        frame_enabled=frame_enabled,
        frame_file=str(frame_file) if frame_file else "",
        initial_profile=initial,
        adaptive_quality=_flag("ADAPTIVE_QUALITY", True),
        reconnect_delay=max(1, reconnect_delay),
        source_retry=max(5, source_retry),
        stall_timeout=max(15, stall_timeout),
        heartbeat=max(10, heartbeat),
        source_end_grace=max(60, source_end_grace),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        check_for_updates=check_for_updates,
        update_repo=update_repo,
        state_dir=state_dir,
    )
