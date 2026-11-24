from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _stop_pipe_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# How many consecutive chunks a subscriber may refuse (its ffmpeg stdin
# buffer full) before the pump drops it: a momentary stall must not
# kill a healthy destination, but one wedged ffmpeg must never starve
# the others. A dropped worker stalls, the supervisor restarts it, and
# it re-subscribes with a fresh stdin.
SHARED_PIPE_BLOCKED_DROP_AFTER = 10


class SharedSourcePipe:
    """One yt-dlp process feeding every encoder worker.

    In pipe mode each destination used to spawn its own yt-dlp process,
    all downloading the same source. The shared pipe spawns yt-dlp once
    and a pump thread copies its stdout to every subscribed ffmpeg
    stdin, so N destinations cost one download instead of N. A worker
    that joins mid-stream reads from the current position of the
    MPEG-TS stream; ffmpeg re-syncs on the next keyframe.
    """

    def __init__(self, settings: Settings) -> None:
        self._command = build_pipe_command(settings)
        self._cookie_file = getattr(settings, "cookie_file", "") or ""
        # The last start failure, for the supervisor to report: a pipe
        # that cannot start because its cookie file vanished must say
        # so, instead of the generic "FFmpeg exited" after yt-dlp dies
        # on every restart.
        self.last_error = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._pump: threading.Thread | None = None
        self._subscribers: set[Any] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        # Consecutive refused chunks per subscriber id; reset on every
        # successful write.
        self._blocked_counts: dict[int, int] = {}

    def start(self) -> bool:
        if self.alive():
            return True
        self.last_error = ""
        if self._cookie_file and not Path(self._cookie_file).is_file():
            # Unlike resolve_source(), the pipe path never validated
            # the cookie file: a file that vanished mid-run made every
            # restart spawn a yt-dlp that exited at once, while
            # restart() still returned True and the worker looped with
            # a generic "FFmpeg exited" reason.
            self.last_error = (
                f"YTDLP_COOKIE_FILE is missing: {self._cookie_file}"
            )
            logging.error(
                "Cannot start the shared yt-dlp pipe: %s", self.last_error
            )
            return False
        self._stopped.clear()
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            logging.error("Could not start the shared yt-dlp pipe: %s", exc)
            return False
        self._process = process
        self._pump = threading.Thread(
            target=self._pump_stdout, name="yt-dlp-pump", daemon=True
        )
        self._pump.start()
        return True

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def subscribe(self, stream: Any) -> None:
        with self._lock:
            self._subscribers.add(stream)
            self._blocked_counts.pop(id(stream), None)
        # Writes go straight to the fd (see _pump_write), so make it
        # non-blocking: a full ffmpeg stdin then refuses a chunk instead
        # of wedging the pump thread for every other destination.
        # Streams without a real fd (test doubles) keep the old path.
        try:
            os.set_blocking(stream.fileno(), False)
        except (OSError, ValueError):
            pass

    def unsubscribe(self, stream: Any) -> None:
        with self._lock:
            self._subscribers.discard(stream)
            self._blocked_counts.pop(id(stream), None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _pump_write(self, subscriber: Any, chunk: bytes) -> bool | str:
        """Feed one chunk to a subscriber.

        Returns True when written, "blocked" when the subscriber's
        stdin buffer is momentarily full, False when the subscriber is
        dead and should be dropped. A partial write drops the tail of
        the chunk (MPEG-TS resyncs on the next keyframe) but counts as
        blocked: a reader that only ever accepts part of each chunk is
        falling behind just like one that accepts nothing, and must
        reach the drop threshold instead of receiving truncated chunks
        forever.
        """
        try:
            fd = subscriber.fileno()
        except (OSError, ValueError):
            fd = None
        if fd is None:
            # No real file descriptor (test doubles): blocking write,
            # as before.
            try:
                subscriber.write(chunk)
                subscriber.flush()
            except (OSError, ValueError):
                return False
            return True
        try:
            written = os.write(fd, chunk)
        except BlockingIOError:
            return "blocked"
        except (OSError, ValueError):
            return False
        if written < len(chunk):
            return "blocked"
        return True

    def _pump_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._stopped.is_set():
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                with self._lock:
                    subscribers = list(self._subscribers)
                for subscriber in subscribers:
                    outcome = self._pump_write(subscriber, chunk)
                    if outcome is True:
                        with self._lock:
                            self._blocked_counts.pop(id(subscriber), None)
                    elif outcome == "blocked":
                        with self._lock:
                            count = self._blocked_counts.get(id(subscriber), 0) + 1
                            self._blocked_counts[id(subscriber)] = count
                        if count >= SHARED_PIPE_BLOCKED_DROP_AFTER:
                            logging.warning(
                                "Shared yt-dlp pipe: dropping a subscriber "
                                "whose input stayed full for %d chunks; "
                                "its worker will restart and re-subscribe",
                                count,
                            )
                            self.unsubscribe(subscriber)
                    else:
                        self.unsubscribe(subscriber)
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stopped.set()
        process, pump = self._process, self._pump
        self._process = None
        self._pump = None
        if process is not None:
            _stop_pipe_process(process)
            for stream in (process.stdout, process.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if pump is not None:
            pump.join(timeout=2)
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            try:
                subscriber.close()
            except (OSError, ValueError):
                pass
