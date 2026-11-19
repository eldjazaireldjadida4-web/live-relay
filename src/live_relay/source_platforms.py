from __future__ import annotations

import re
from urllib.parse import urlsplit

SUPPORTED_SOURCE_PLATFORMS = {"auto", "tiktok", "youtube", "custom"}
_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}
_TIKTOK_LIVE_PATHS = (
    re.compile(r"/@[\w.-]+/live/?", re.IGNORECASE),
    re.compile(r"/share/live/\d+/?", re.IGNORECASE),
)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_tiktok_host(url: str) -> bool:
    host = _host(url)
    return host in _TIKTOK_HOSTS or host.endswith(".tiktok.com")


def is_tiktok_live_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _TIKTOK_HOSTS or parsed.scheme not in {"http", "https"}:
        return False
    return any(pattern.fullmatch(parsed.path) for pattern in _TIKTOK_LIVE_PATHS)


def source_platform_for(url: str, requested: str = "auto") -> str:
    platform = (requested or "auto").strip().lower()
    if platform not in SUPPORTED_SOURCE_PLATFORMS:
        raise ValueError("SOURCE_PLATFORM must be auto, tiktok, youtube, or custom")
    host = _host(url)
    if platform == "auto":
        if is_tiktok_host(url):
            if not is_tiktok_live_url(url):
                raise ValueError(
                    "Use a TikTok LIVE profile URL (/@name/live) or a mobile share/live URL."
                )
            return "tiktok"
        if host in _YOUTUBE_HOSTS:
            return "youtube"
        return "custom"
    if platform == "tiktok" and not is_tiktok_live_url(url):
        raise ValueError(
            "TikTok source must be a LIVE URL: /@name/live or /share/live/ID."
        )
    if platform == "youtube" and host not in _YOUTUBE_HOSTS:
        raise ValueError("YouTube source must use youtube.com or youtu.be.")
    return platform


def source_platform_label(platform: str) -> str:
    labels = {
        "tiktok": "TikTok LIVE",
        "youtube": "YouTube LIVE",
        "custom": "Live source",
    }
    return labels.get(platform, "Live source")


def validate_source_platform(url: str, requested: str) -> None:
    source_platform_for(url, requested)
