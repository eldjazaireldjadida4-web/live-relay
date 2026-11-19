from __future__ import annotations

import logging
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .destinations import (
    DestinationSession,
    close_destinations,
    open_destinations,
    update_destination_metadata,
    update_destination_thumbnail,
)
from .encoder import build_commands, redact_urls, resolve_ffmpeg
from .notifier import Notifier, notifier_from_settings, recording_file_path
from .metadata import (
    StreamMetadata,
    apply_metadata_overrides,
    generate_metadata,
    source_metadata,
)
from .quality import AdaptiveQuality
from .runtime import RelayStatus
from .source import LiveSource, build_pipe_command, resolve_source
from .source_platforms import source_platform_for, source_platform_label
from .thumbnail_ai import generate_auto_thumbnail_task
from .thumbnails import get_thumbnail_path


@dataclass
class Progress:
    lock: threading.Lock = field(default_factory=threading.Lock)
    values: dict[str, str] = field(default_factory=dict)
    last_output_change: float = field(default_factory=time.monotonic)
    last_output_ms: int = -1

    def feed(self, line: str) -> None:
        key, separator, value = line.partition("=")
        if not separator:
            return
        key, value = key.strip(), value.strip()
        with self.lock:
            self.values[key] = value
            if key == "out_time_ms":
                try:
                    current = int(value)
                except ValueError:
                    return
                if current != self.last_output_ms:
                    self.last_output_ms = current
                    self.last_output_change = time.monotonic()

    def snapshot(self) -> dict[str, str]:
        with self.lock:
            return dict(self.values)

    def stalled(self, timeout: float) -> bool:
        with self.lock:
            return time.monotonic() - self.last_output_change > timeout


def _pump_progress(stream, state: Progress, stopped: threading.Event) -> None:
    try:
        for line in iter(stream.readline, ""):
            if stopped.is_set():
                break
            state.feed(line.strip())
    except (OSError, ValueError):
        pass


TRANSPORT_ERROR_MARKERS = (
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "operation timed out",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "i/o error",
    "tls fatal alert",
    "tls alert",
    "error opening output",
    "error writing trailer",
    "error writing packet",
    "error muxing a packet",
    "failed to update header",
    "server returned 4",
    "server returned 5",
)


def _is_transport_error(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in TRANSPORT_ERROR_MARKERS)


def _pump_logs(
    stream,
    stopped: threading.Event,
    label: str = "ffmpeg",
    transport_failure: threading.Event | None = None,
) -> None:
    try:
        while not stopped.is_set():
            raw = stream.readline()
            if raw in ("", b""):
                break
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if label == "ffmpeg" and transport_failure and _is_transport_error(line):
                transport_failure.set()
            clean = redact_urls(line.strip())
            if clean:
                logging.info("[%s] %s", label, clean)
    except (OSError, ValueError):
        pass


def _parse_speed(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)x\s*", value)
    return float(match.group(1)) if match else None


def _slowest_speed(progresses: list[Progress]) -> float | None:
    speeds = [_parse_speed(progress.snapshot().get("speed")) for progress in progresses]
    return (
        min(speeds) if speeds and all(speed is not None for speed in speeds) else None
    )


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait(seconds: float, stopped: threading.Event) -> None:
    stopped.wait(seconds)


def _destination_status(
    session: DestinationSession,
    active_platforms: list[str] | None = None,
    failures: dict[str, str] | None = None,
) -> tuple[str, str]:
    labels = {"youtube": "YouTube", "facebook": "Facebook"}
    active_platforms = (
        session.active_platforms if active_platforms is None else active_platforms
    )
    failures = session.failures if failures is None else failures
    active = " + ".join(labels.get(platform, platform) for platform in active_platforms)
    if failures:
        unavailable = "، ".join(labels.get(platform, platform) for platform in failures)
        if active:
            return "partial", f"البث نشط على {active}؛ تعذّر تشغيل {unavailable}"
        return "reconnecting", f"إعادة الاتصال بالوجهات: {unavailable}"
    return "streaming", f"يُرسل الفيديو إلى {active}"


def _update_worker_status(
    status: RelayStatus | None,
    session: DestinationSession,
    workers: list[WorkerState],
) -> None:
    if status is None:
        return
    active_platforms = [worker.name for worker in workers if worker.running]
    failures = dict(session.failures)
    failures.update(
        {
            worker.name: worker.failure_reason
            for worker in workers
            if worker.failure_reason
        }
    )
    if active_platforms:
        mode, message = _destination_status(session, active_platforms, failures)
    elif workers and all(worker.exhausted for worker in workers):
        mode, message = "error", "توقفت جميع الوجهات بعد استنفاد محاولات إعادة الاتصال"
    elif failures:
        mode, message = _destination_status(session, active_platforms, failures)
    else:
        mode, message = "reconnecting", "يعيد البرنامج الاتصال بوجهات البث"
    status.update(
        mode=mode,
        message=message,
        active_platforms=active_platforms,
        destination_errors=failures,
        last_error="",
    )


def _format_exit_code(returncode: int | None) -> str:
    if returncode is None:
        return "unknown"
    if 2**31 <= returncode <= 2**32 - 1:
        return f"{returncode} (signed {returncode - 2**32})"
    return str(returncode)


def _transport_failure_reason(platform: str) -> str:
    if platform == "facebook":
        return (
            "Facebook RTMPS/TLS connection failed; verify its stream key, "
            "Live Producer access, and outbound network"
        )
    if platform == "youtube":
        return (
            "YouTube RTMPS connection failed; verify its stream key "
            "and outbound network"
        )
    return "RTMP(S) connection failed; verify the destination stream key and network"


WORKER_MAX_CONSECUTIVE_FAILURES = 5
WORKER_BACKOFF_CAP = 30.0
WORKER_STABLE_SECONDS = 120.0


@dataclass
class WorkerState:
    """Per-destination encoder supervision state."""

    name: str
    index: int
    process: subprocess.Popen[str] | None = None
    pipe_process: subprocess.Popen[str] | None = None
    progress: Progress = field(default_factory=Progress)
    transport_failure: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    reader_stop: threading.Event = field(default_factory=threading.Event)
    started_at: float = 0.0
    next_start: float = 0.0
    failures: int = 0
    failure_reason: str = ""
    exhausted: bool = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


def _worker_backoff(failures: int) -> float:
    return min(WORKER_BACKOFF_CAP, 2.0 * (2 ** max(0, failures - 1)))


def _teardown_worker(worker: WorkerState) -> None:
    _stop_process(worker.process)
    _stop_process(worker.pipe_process)
    streams: list[Any] = []
    if worker.process is not None:
        streams.extend((worker.process.stdout, worker.process.stderr))
    if worker.pipe_process is not None and worker.pipe_process.stderr:
        streams.append(worker.pipe_process.stderr)
    for stream in streams:
        if stream:
            try:
                stream.close()
            except OSError:
                pass
    worker.reader_stop.set()
    for thread in worker.threads:
        thread.join(timeout=2)
    worker.process = None
    worker.pipe_process = None
    worker.threads = []


def _branding_inputs(settings: Settings, portrait: bool) -> tuple[str, str]:
    frame_enabled = getattr(settings, "frame_enabled", False)
    configured_frame = getattr(settings, "frame_file", "")
    built_in_frame = frame_enabled and not configured_frame
    frame_path = ""
    if frame_enabled:
        frame_name = (
            "oussama-news-frame-portrait.png"
            if portrait
            else "oussama-news-frame-landscape.png"
        )
        frame_path = configured_frame or str(Path(__file__).with_name(frame_name))
    logo_path = ""
    if getattr(settings, "logo_enabled", False) and not built_in_frame:
        logo_path = getattr(settings, "logo_file", "")
    return logo_path, frame_path


def _start_worker(
    worker: WorkerState,
    ffmpeg: str,
    source: LiveSource,
    session: DestinationSession,
    quality: AdaptiveQuality,
    settings: Settings,
) -> bool:
    portrait = getattr(source, "width", 0) > 0 and getattr(
        source, "height", 0
    ) > getattr(source, "width", 0)
    logo_path, frame_path = _branding_inputs(settings, portrait)
    recording_path = ""
    if settings.record_enabled and worker.index == 0:
        try:
            recording_path = recording_file_path(settings.state_dir, worker.name)
            logging.info("Archiving this session to %s", recording_path)
        except OSError as exc:
            logging.warning("Recording disabled for this session: %s", exc)
    commands = build_commands(
        ffmpeg,
        source.urls,
        [session.targets[worker.index]],
        quality.profile,
        source.headers,
        pipe_mode=settings.pipe_mode,
        preset=settings.ffmpeg_preset,
        logo_path=logo_path,
        logo_position=getattr(settings, "logo_position", "bottom-right"),
        logo_size_percent=getattr(settings, "logo_size_percent", 12),
        logo_opacity=getattr(settings, "logo_opacity", 0.9),
        portrait=portrait,
        frame_path=frame_path,
        recording_path=recording_path,
    )
    pipe_process: subprocess.Popen[str] | None = None
    input_stream: Any = subprocess.DEVNULL
    if settings.pipe_mode:
        try:
            pipe_process = subprocess.Popen(
                build_pipe_command(settings),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            logging.error("Could not start yt-dlp pipe for %s: %s", worker.name, exc)
            return False
        input_stream = pipe_process.stdout
    try:
        process = subprocess.Popen(
            commands[0],
            stdin=input_stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        logging.error("Could not start FFmpeg for %s: %s", worker.name, exc)
        _stop_process(pipe_process)
        return False
    if pipe_process and pipe_process.stdout:
        pipe_process.stdout.close()
    worker.process = process
    worker.pipe_process = pipe_process
    worker.progress = Progress()
    worker.transport_failure = threading.Event()
    worker.failure_reason = ""
    worker.exhausted = False
    worker.started_at = time.monotonic()
    worker.reader_stop = threading.Event()
    reader_stop = worker.reader_stop
    worker.threads = [
        threading.Thread(
            target=_pump_progress,
            args=(process.stdout, worker.progress, reader_stop),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_logs,
            args=(process.stderr, reader_stop, "ffmpeg", worker.transport_failure),
            daemon=True,
        ),
    ]
    if pipe_process and pipe_process.stderr:
        worker.threads.append(
            threading.Thread(
                target=_pump_logs,
                args=(pipe_process.stderr, reader_stop, "yt-dlp"),
                daemon=True,
            )
        )
    for thread in worker.threads:
        thread.start()
    logging.info(
        "Encoder worker for %s started (%s video, %s audio)",
        worker.name,
        quality.profile.video_bitrate,
        quality.profile.audio_bitrate,
    )
    return True


def _start_encoder(
    ffmpeg: str,
    source: LiveSource,
    session: DestinationSession,
    quality: AdaptiveQuality,
    settings: Settings,
    stopped: threading.Event,
    status: RelayStatus | None = None,
    notifier: Notifier | None = None,
) -> tuple[str, float]:
    workers = [
        WorkerState(name=name, index=index)
        for index, (name, _) in enumerate(session.targets)
    ]
    started = time.monotonic()
    last_heartbeat = started
    reason = "stopped"
    logging.info(
        "Encoder supervision started for %d destination(s) at %s (%s video, %s audio)",
        len(workers),
        quality.profile.name,
        quality.profile.video_bitrate,
        quality.profile.audio_bitrate,
    )
    if status:
        status.update(
            mode="starting",
            message="يتم الاتصال بوجهات البث",
            quality=quality.profile.name,
            bitrate=quality.profile.video_bitrate,
            active_platforms=[],
            destination_errors=dict(session.failures),
            last_error="",
        )

    no_data_streak = 0

    def refresh_source() -> LiveSource:
        nonlocal source
        try:
            fresh = resolve_source(settings)
        except Exception as exc:
            logging.warning("Source refresh failed: %s", redact_urls(str(exc))[:200])
            return source
        if fresh is not None:
            source = fresh
        return source

    try:
        while not stopped.is_set():
            now = time.monotonic()
            any_running = False
            restartable = False
            for worker in workers:
                if worker.running:
                    any_running = True
                    restartable = True
                    if now - worker.started_at >= WORKER_STABLE_SECONDS:
                        worker.failures = 0
                    continue
                if worker.process is not None:
                    returncode = worker.process.returncode
                    _teardown_worker(worker)
                    transport_failed = worker.transport_failure.is_set()
                    worker.failure_reason = (
                        _transport_failure_reason(worker.name)
                        if transport_failed
                        else f"FFmpeg exited with code {_format_exit_code(returncode)}"
                    )
                    if notifier is not None:
                        notifier.notify(
                            "destination_failed",
                            platform=worker.name,
                            reason=worker.failure_reason,
                            reconnect_in=f"{_worker_backoff(worker.failures + 1):.0f}s",
                        )
                    logging.warning(
                        "%s encoder exited with code %s; scheduling a restart",
                        worker.name,
                        _format_exit_code(returncode),
                    )
                    worker.failures += 1
                    worker.next_start = now + _worker_backoff(worker.failures)
                if worker.failures >= WORKER_MAX_CONSECUTIVE_FAILURES:
                    if not worker.exhausted:
                        worker.exhausted = True
                        if not worker.failure_reason:
                            worker.failure_reason = (
                                "Stopped after five failed output attempts; verify "
                                "the stream key, destination access, and network"
                            )
                        logging.error(
                            "%s exhausted its %d output attempts; leaving it stopped",
                            worker.name,
                            worker.failures,
                        )
                    continue
                restartable = True
                if now >= worker.next_start:
                    current = refresh_source() if worker.failures else source
                    if _start_worker(
                        worker, ffmpeg, current, session, quality, settings
                    ):
                        any_running = True
                    else:
                        worker.failures += 1
                        worker.failure_reason = (
                            "FFmpeg or yt-dlp could not start; see the log for details"
                        )
                        worker.next_start = now + _worker_backoff(worker.failures)
            for worker in workers:
                if not worker.running:
                    continue
                if worker.transport_failure.is_set():
                    worker.failure_reason = _transport_failure_reason(worker.name)
                    logging.error(
                        "FFmpeg for %s reported an output transport failure; "
                        "restarting it",
                        worker.name,
                    )
                    _teardown_worker(worker)
                    worker.failures += 1
                    worker.next_start = time.monotonic() + _worker_backoff(
                        worker.failures
                    )
                    continue
                if worker.progress.stalled(settings.stall_timeout):
                    worker.failure_reason = (
                        f"No output progress for {settings.stall_timeout} seconds"
                    )
                    logging.error(
                        "Output to %s made no progress for %s seconds; restarting it",
                        worker.name,
                        settings.stall_timeout,
                    )
                    _teardown_worker(worker)
                    worker.failures += 1
                    worker.next_start = time.monotonic() + _worker_backoff(
                        worker.failures
                    )
            _update_worker_status(status, session, workers)
            if not restartable and not any_running:
                reason = "gave-up"
                logging.error(
                    "Every destination exhausted its restart budget; "
                    "refreshing the session"
                )
                break
            if now - last_heartbeat >= settings.heartbeat:
                last_heartbeat = now
                running = [worker for worker in workers if worker.running]
                starved = False
                snapshots = [worker.progress.snapshot() for worker in running]
                for worker, snapshot in zip(running, snapshots):
                    logging.info(
                        "Health (%s): speed=%s, output_time=%s, "
                        "dropped_frames=%s, profile=%s",
                        worker.name,
                        snapshot.get("speed", "?"),
                        snapshot.get("out_time", "?"),
                        snapshot.get("drop_frames", "?"),
                        quality.profile.name,
                    )
                stream_health = None
                if session.youtube and session.youtube.broadcast:
                    try:
                        youtube_status = session.youtube.check_status()
                        stream_health = youtube_status.get("stream_health")
                        logging.info(
                            "YouTube status: broadcast=%s, stream=%s, ingest_health=%s",
                            youtube_status["broadcast_status"],
                            youtube_status["stream_status"],
                            youtube_status["stream_health"],
                        )
                        if (
                            youtube_status["broadcast_status"] == "ready"
                            and youtube_status["stream_status"] == "active"
                        ):
                            session.youtube.ensure_live()
                        starved = youtube_status["stream_health"] == "no_data"
                    except Exception as exc:
                        logging.warning(
                            "YouTube status check failed: %s",
                            redact_urls(str(exc))[:200],
                        )
                if starved and running:
                    no_data_streak += 1
                    if no_data_streak >= 3:
                        logging.error(
                            "YouTube reports no incoming data for %d heartbeats "
                            "despite running encoders; restarting the ingest",
                            no_data_streak,
                        )
                        no_data_streak = 0
                        for worker in running:
                            worker.failure_reason = (
                                "YouTube reported no incoming data; "
                                "restarting the ingest"
                            )
                            _teardown_worker(worker)
                            worker.failures += 1
                            worker.next_start = time.monotonic() + _worker_backoff(
                                worker.failures
                            )
                        _update_worker_status(status, session, workers)
                        continue
                elif running:
                    no_data_streak = 0
                if settings.adaptive_quality and running:
                    speed = _slowest_speed([worker.progress for worker in running])
                    change = quality.observe(speed, now, stream_health)
                    if change:
                        logging.warning(
                            "%s; changing quality %s to %s",
                            (
                                "YouTube reports an unhealthy ingest"
                                if stream_health == "bad"
                                else "Sustained slow pipeline"
                            ),
                            change,
                            quality.profile.name,
                        )
                        reason = "quality-change"
                        break
            stopped.wait(2)
    except KeyboardInterrupt:
        stopped.set()
    finally:
        for worker in workers:
            _teardown_worker(worker)

    return reason, time.monotonic() - started


def _session_is_dead(session: DestinationSession) -> bool:
    """True when the platform event itself ended and must be re-created."""
    if session.youtube:
        try:
            state = session.youtube.check_status()
        except Exception as exc:
            logging.warning(
                "YouTube session check failed: %s", redact_urls(str(exc))[:200]
            )
            return False
        return state.get("broadcast_status") in {"complete", "revoked", "created"}
    return False


def _metadata_signature(source: LiveSource) -> tuple[Any, ...]:
    return (
        source.title,
        getattr(source, "description", ""),
        getattr(source, "channel", ""),
        tuple(getattr(source, "categories", ())),
        tuple(getattr(source, "tags", ())),
    )


def _close_active_destinations(
    session: DestinationSession, status: RelayStatus | None
) -> None:
    try:
        close_destinations(session)
    finally:
        if status:
            status.set_destination_session(None)


def _refresh_thumbnail(
    settings: Settings,
    session: DestinationSession,
    status: RelayStatus | None,
    source: LiveSource,
    metadata: StreamMetadata,
) -> None:
    """Generate a thumbnail with Gemini in the background and push it live."""
    state_dir = getattr(settings, "state_dir", None)
    if not state_dir or not getattr(settings, "gemini_thumbnail_enabled", False):
        return
    if not str(getattr(settings, "gemini_api_key", "")).strip():
        return

    def on_done(provider: str) -> None:
        if provider == "manual":
            logging.info(
                "Keeping the manually uploaded YouTube thumbnail; skipping auto generation"
            )
            if status:
                status.update(thumbnail_provider="manual")
            return
        if not provider:
            return
        thumbnail_path = get_thumbnail_path(state_dir)
        if not thumbnail_path:
            return
        try:
            results = update_destination_thumbnail(session, thumbnail_path)
        except Exception as exc:
            logging.warning(
                "Could not push the automatic thumbnail: %s", redact_urls(str(exc))
            )
            return
        if status:
            status.update(
                thumbnail_provider=provider,
                thumbnail_ready=True,
                destination_errors={**results, **session.failures},
            )
        logging.info("Automatic thumbnail applied to the destinations: %s", results)

    generate_auto_thumbnail_task(
        settings, source, metadata.title, Path(state_dir), on_done
    )


def run(
    settings: Settings,
    stopped: threading.Event,
    status: RelayStatus | None = None,
) -> None:
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    source_platform = source_platform_for(
        getattr(settings, "source_url", ""),
        getattr(settings, "source_platform", "auto"),
    )
    source_label = source_platform_label(source_platform)
    quality = AdaptiveQuality(settings.initial_profile)
    session: DestinationSession | None = None
    notifier = notifier_from_settings(settings)
    session_started_monotonic: float | None = None
    current_title = ""
    current_metadata: StreamMetadata | None = None
    base_metadata: StreamMetadata | None = None
    metadata_signature: tuple[Any, ...] | None = None
    missing_since: float | None = None
    retry_delay = settings.reconnect_delay
    retry_count = 0
    if status:
        status.set_destination_session(None)
        status.update(
            mode="waiting",
            message=f"Waiting for {source_label} to go live",
            source_platform=source_platform,
            title="",
            description="",
            tags=[],
            metadata_provider="",
            metadata_manual=False,
            quality=quality.profile.name,
            bitrate=quality.profile.video_bitrate,
            started_at=time.time(),
            retry_count=0,
            last_error="",
            active_platforms=[],
            destination_errors={},
        )
    logging.info("Live relay is ready; waiting for %s", source_label)

    while not stopped.is_set():
        try:
            source = resolve_source(settings)
        except Exception:
            if session:
                _close_active_destinations(session, status)
                session = None
            raise
        if source is None:
            if session is None:
                if status:
                    status.update(
                        mode="waiting",
                        message=f"Waiting for {source_label} to go live",
                        source_platform=source_platform,
                    )
                logging.info(
                    "Source is not live yet; checking again in %s seconds",
                    settings.source_retry,
                )
            else:
                if missing_since is None:
                    missing_since = time.monotonic()
                    if status:
                        status.update(
                            mode="source_offline",
                            message=(
                                f"{source_label} source lost; "
                                "keeping destination events open"
                            ),
                            source_platform=source_platform,
                            last_error="",
                        )
                    logging.warning(
                        "Source disappeared; keeping the live event open "
                        "while reconnecting"
                    )
                elif time.monotonic() - missing_since >= settings.source_end_grace:
                    logging.warning(
                        "Source stayed offline past the grace period; "
                        "closing platform events"
                    )
                    notifier.notify(
                        "relay_ended",
                        duration=(
                            time.monotonic() - session_started_monotonic
                            if session_started_monotonic is not None
                            else None
                        ),
                    )
                    session_started_monotonic = None
                    _close_active_destinations(session, status)
                    session = None
                    current_title = ""
                    current_metadata = None
                    metadata_signature = None
                    quality = AdaptiveQuality(settings.initial_profile)
                    missing_since = None
                    retry_delay = settings.reconnect_delay
                    if status:
                        status.update(
                            mode="waiting",
                            message=f"{source_label} ended; waiting for the next live",
                            source_platform=source_platform,
                            title="",
                            description="",
                            tags=[],
                            metadata_provider="",
                            metadata_manual=False,
                            quality=quality.profile.name,
                            bitrate=quality.profile.video_bitrate,
                            active_platforms=[],
                            destination_errors={},
                        )
            _wait(
                settings.source_retry
                if session is None
                else min(10, settings.source_retry),
                stopped,
            )
            continue

        missing_since = None
        signature = _metadata_signature(source)
        metadata_changed = signature != metadata_signature
        if current_metadata is None or metadata_changed:
            if session is None and status:
                status.update(
                    mode="starting",
                    message=f"Preparing metadata for {source_label}",
                    source_platform=source.platform,
                    title=source.title,
                )
            if getattr(settings, "metadata_api_enabled", True):
                base_metadata = generate_metadata(settings, source)
            else:
                base_metadata = source_metadata(
                    source, getattr(settings, "youtube_category_id", "22")
                )
            active_overrides = status.metadata_overrides if status else None
            if session is not None:
                with session.lock:
                    if session.manual_metadata_override and session.metadata:
                        session.base_metadata = base_metadata
                        current_metadata = session.metadata
                    elif session.ignore_config_overrides:
                        current_metadata = apply_metadata_overrides(
                            settings, base_metadata, active_overrides or {}
                        )
                    else:
                        current_metadata = apply_metadata_overrides(
                            settings, base_metadata, active_overrides
                        )
            else:
                current_metadata = apply_metadata_overrides(
                    settings, base_metadata, active_overrides
                )
            metadata_signature = signature
        if session is None:
            current_title = current_metadata.title
            if status:
                status.update(
                    mode="starting",
                    message=f"Preparing {source_label} for relay",
                    source_platform=source.platform,
                    title=current_title,
                    description=current_metadata.description,
                    tags=list(current_metadata.tags),
                    metadata_provider=current_metadata.provider,
                    metadata_manual=current_metadata.provider == "manual",
                    last_error="",
                )
            logging.info("%s is live: %s", source_label, source.title)
            try:
                session = open_destinations(settings, current_metadata, base_metadata)
                if status:
                    status.set_destination_session(session)
            except Exception as exc:
                message = redact_urls(str(exc))[:300]
                logging.error("Could not prepare streaming destinations: %s", message)
                retry_count += 1
                if status:
                    status.update(
                        mode="error",
                        message="Could not prepare the platforms; retrying",
                        retry_count=retry_count,
                        last_error=message,
                    )
                _wait(retry_delay, stopped)
                retry_delay = min(retry_delay * 2, 60)
                continue
            quality = AdaptiveQuality(settings.initial_profile)
            session_started_monotonic = time.monotonic()
            notifier.notify(
                "session_started",
                title=current_title,
                platforms=", ".join(
                    session.active_platforms
                    if isinstance(session.active_platforms, (list, tuple, set))
                    else []
                ),
                quality=quality.profile.name,
            )
            _refresh_thumbnail(settings, session, status, source, current_metadata)
            if status:
                status.update(
                    mode="starting",
                    message="Destinations are ready; starting video",
                    source_platform=source.platform,
                    title=current_title,
                    description=current_metadata.description,
                    tags=list(current_metadata.tags),
                    metadata_provider=current_metadata.provider,
                    metadata_manual=current_metadata.provider == "manual",
                    quality=quality.profile.name,
                    bitrate=quality.profile.video_bitrate,
                    active_platforms=session.active_platforms,
                    destination_errors=dict(session.failures),
                )
        elif metadata_changed:
            with session.lock:
                keep_manual_metadata = session.manual_metadata_override
                if keep_manual_metadata and session.metadata:
                    current_metadata = session.metadata
            current_title = current_metadata.title
            if keep_manual_metadata:
                logging.info(
                    "Source metadata changed; keeping the manually chosen live metadata"
                )
            else:
                logging.info(
                    "Source metadata changed; updating live destination metadata"
                )
                update_destination_metadata(
                    session, current_metadata, base_metadata=base_metadata
                )
            if status:
                status.update(
                    title=current_title,
                    description=current_metadata.description,
                    tags=list(current_metadata.tags),
                    metadata_provider=current_metadata.provider,
                    metadata_manual=current_metadata.provider == "manual",
                )
            if not keep_manual_metadata:
                _refresh_thumbnail(settings, session, status, source, current_metadata)

        try:
            reason, run_seconds = _start_encoder(
                ffmpeg, source, session, quality, settings, stopped, status, notifier
            )
        except Exception:
            _close_active_destinations(session, status)
            session = None
            raise
        if stopped.is_set():
            break
        if run_seconds >= 600 or reason == "gave-up":
            retry_delay = settings.reconnect_delay
        else:
            retry_delay = min(retry_delay * 2, 60)
        retry_count += 1
        if reason == "gave-up" and session is not None and _session_is_dead(session):
            logging.warning(
                "The platform live event ended; re-creating it before the next attempt"
            )
            _close_active_destinations(session, status)
            session = None
        if status:
            changed_profile = quality.profile
            runtime_status = status.snapshot()
            status.update(
                mode="reconnecting" if reason != "quality-change" else "starting",
                message="Refreshing the source and reconnecting"
                if reason != "quality-change"
                else "Applying the new quality profile",
                active_platforms=[],
                destination_errors=runtime_status.get("destination_errors", {}),
                retry_count=retry_count,
                quality=changed_profile.name,
                bitrate=changed_profile.video_bitrate,
                last_error="",
            )
        if reason != "quality-change":
            logging.warning("Refreshing source and retrying in %s seconds", retry_delay)
            _wait(retry_delay, stopped)

    if session:
        notifier.notify(
            "relay_stopped",
            duration=(
                time.monotonic() - session_started_monotonic
                if session_started_monotonic is not None
                else None
            ),
        )
        _close_active_destinations(session, status)
    if status:
        status.update(
            mode="stopped",
            message="Relay stopped",
            title="",
            description="",
            tags=[],
            metadata_provider="",
            metadata_manual=False,
            started_at=None,
            active_platforms=[],
            destination_errors={},
        )
    logging.info("Relay stopped")


def install_signal_handlers(stopped: threading.Event) -> None:
    def stop_handler(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
