from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .encoder import redact_urls
from .source_platforms import source_platform_for, source_platform_label


@dataclass(frozen=True)
class LiveSource:
    title: str
    urls: tuple[str, ...]
    headers: tuple[dict[str, str], ...]
    description: str = ""
    channel: str = ""
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    platform: str = "custom"
    width: int = 0
    height: int = 0


def clean_title(value: str) -> str:
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value or "").split())
    return cleaned[:100].strip() or "Live broadcast"


def _clean_metadata_text(value: object, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))
    return text.strip()[:limit].strip()


def _positive_dimension(value: object) -> int:
    try:
        dimension = int(value)
    except (TypeError, ValueError):
        return 0
    return dimension if dimension > 0 else 0


def _tiktok_reports_offline(detail: str) -> bool:
    normalized = detail.casefold()
    return any(
        phrase in normalized
        for phrase in (
            "not currently live",
            "channel is not live",
            "channel is currently offline",
        )
    )


def resolve_source(settings: Settings) -> LiveSource | None:
    source_platform = source_platform_for(
        settings.source_url, getattr(settings, "source_platform", "auto")
    )
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        "-f",
        settings.format_selector,
        settings.source_url,
    ]
    if settings.cookie_file:
        if not Path(settings.cookie_file).is_file():
            logging.error("YTDLP_COOKIE_FILE was configured but the file is missing")
            return None
        command[-1:-1] = ["--cookies", settings.cookie_file]
    elif settings.cookies_from_browser:
        command[-1:-1] = ["--cookies-from-browser", settings.cookies_from_browser]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=90, check=False
        )
    except subprocess.TimeoutExpired:
        logging.warning("Source metadata lookup timed out")
        return None
    except OSError as exc:
        logging.error("Could not start yt-dlp: %s", exc)
        return None
    if result.returncode != 0:
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        detail = re.sub(r"[\x00-\x1f\x7f]+", " ", lines[-1]) if lines else ""
        detail = redact_urls(detail)[:300]
        if source_platform == "tiktok" and _tiktok_reports_offline(detail):
            if settings.cookie_file or settings.cookies_from_browser:
                logging.info(
                    "TikTok LIVE is not visible to yt-dlp; it may be offline, or the signed-in session may need renewal"
                )
            else:
                logging.info(
                    "TikTok LIVE is not visible to yt-dlp; it may be offline or require a signed-in session. If it is visible only after login, configure browser cookies"
                )
        else:
            logging.warning(
                "%s source lookup failed%s",
                source_platform_label(source_platform),
                f": {detail}" if detail else "",
            )
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        logging.warning("yt-dlp returned invalid metadata")
        return None
    if data.get("live_status") != "is_live" and data.get("is_live") is not True:
        return None
    requested = data.get("requested_formats") or []
    selected = requested if requested else [data]
    urls = [str(item.get("url", "")).strip() for item in selected if item.get("url")]
    if not urls:
        logging.warning("Live source has no usable media URLs")
        return None
    video_format = next(
        (
            item
            for item in selected
            if item.get("vcodec") != "none"
            and (item.get("width") or item.get("height"))
        ),
        data,
    )
    width = _positive_dimension(video_format.get("width") or data.get("width"))
    height = _positive_dimension(video_format.get("height") or data.get("height"))
    selected = selected[:2]
    urls = urls[:2]
    headers = tuple(
        {
            str(key): str(value)
            for key, value in (
                item.get("http_headers") or data.get("http_headers") or {}
            ).items()
        }
        for item in selected
    )
    raw_tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    raw_categories = (
        data.get("categories") if isinstance(data.get("categories"), list) else []
    )
    return LiveSource(
        title=clean_title(str(data.get("title", ""))),
        urls=tuple(urls),
        headers=headers,
        description=_clean_metadata_text(data.get("description", ""), 3500),
        channel=_clean_metadata_text(
            data.get("channel") or data.get("uploader", ""), 200
        ),
        categories=tuple(_clean_metadata_text(item, 80) for item in raw_categories[:8]),
        tags=tuple(_clean_metadata_text(item, 80) for item in raw_tags[:20]),
        platform=source_platform,
        width=width,
        height=height,
    )


def build_pipe_command(settings: Settings) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-o",
        "-",
        "--hls-use-mpegts",
        "--no-part",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-f",
        settings.format_selector,
        settings.source_url,
    ]
    if settings.cookie_file:
        command[-1:-1] = ["--cookies", settings.cookie_file]
    elif settings.cookies_from_browser:
        command[-1:-1] = ["--cookies-from-browser", settings.cookies_from_browser]
    return command
