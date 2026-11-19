from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from .config import ensure_private_dir, load_settings, resolve_project_root
from .dashboard import DashboardController, run_dashboard
from .destinations import YouTubeDestination
from .encoder import redact_urls, resolve_ffmpeg
from .service import install_signal_handlers, run

PROJECT_ROOT = resolve_project_root()


def _configure_logging(settings) -> None:
    ensure_private_dir(settings.state_dir)
    file_handler = RotatingFileHandler(
        settings.state_dir / "live-relay.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )


def _check(settings) -> int:
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    try:
        ytdlp = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "yt-dlp is unavailable; reinstall the project dependencies"
        ) from exc
    logging.info("Configuration is valid")
    logging.info("FFmpeg: %s", ffmpeg)
    logging.info("yt-dlp: %s", ytdlp)
    logging.info("Platforms: %s", ", ".join(settings.platforms))
    platform_modes = []
    if "youtube" in settings.platforms:
        platform_modes.append(
            f"YouTube={'API' if settings.youtube_api_enabled else 'static key'}"
        )
        if (
            settings.youtube_api_enabled
            and not (settings.state_dir / "youtube-token.json").is_file()
        ):
            logging.warning(
                "YouTube API is selected but not authorized; use the dashboard or --authorize-youtube before broadcasting"
            )
    if "facebook" in settings.platforms:
        platform_modes.append(
            f"Facebook={'API' if settings.facebook_access_token else 'static key'}"
        )
    logging.info("Platform modes: %s", ", ".join(platform_modes))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Relay TikTok or YouTube LIVE to YouTube and Facebook"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check", action="store_true", help="validate settings and required programs"
    )
    group.add_argument(
        "--authorize-youtube",
        action="store_true",
        help="sign in to YouTube before the first broadcast",
    )
    group.add_argument(
        "--headless",
        action="store_true",
        help="run the relay without the local dashboard",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=8765, help="local dashboard port"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open the dashboard in a browser",
    )
    args = parser.parse_args()
    try:
        if not (args.check or args.authorize_youtube or args.headless):
            return run_dashboard(PROJECT_ROOT, args.dashboard_port, not args.no_browser)
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        if args.authorize_youtube:
            controller = DashboardController(PROJECT_ROOT)
            controller.discover_youtube_client_file()
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
                raise ValueError(
                    "Select YouTube API mode before authorizing an account"
                )
            if not settings.youtube_client_secrets:
                raise ValueError(
                    "Set YOUTUBE_CLIENT_SECRETS_FILE before authorizing YouTube"
                )
            YouTubeDestination(settings).authenticate()
            logging.info("YouTube authorization saved securely on this device")
            return 0
        settings = load_settings()
        _configure_logging(settings)
        if args.check:
            return _check(settings)
        if (
            "youtube" in settings.platforms
            and settings.youtube_api_enabled
            and not (settings.state_dir / "youtube-token.json").is_file()
        ):
            raise RuntimeError(
                "Authorize YouTube from the dashboard or with --authorize-youtube before running headless"
            )
        stopped = threading.Event()
        install_signal_handlers(stopped)
        run(settings, stopped)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        logging.error("%s", redact_urls(str(exc))[:500])
        return 1
    except Exception as exc:
        logging.error("Unexpected failure: %s", redact_urls(str(exc))[:500])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
