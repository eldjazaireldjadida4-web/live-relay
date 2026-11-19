from __future__ import annotations

import os
import re
import shutil

from .quality import QualityProfile

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"


def resolve_ffmpeg(configured: str = "") -> str:
    if configured:
        candidate = os.path.expanduser(configured)
        if os.path.isfile(candidate):
            return candidate
        found = shutil.which(configured)
        if found:
            return found
        raise RuntimeError("FFMPEG does not point to an executable")
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _input_arguments(url: str, headers: dict[str, str] | None = None) -> list[str]:
    headers = headers or {}
    normalized = {key.lower(): value for key, value in headers.items()}
    is_http = url.lower().startswith(("http://", "https://"))
    args = ["-thread_queue_size", "1024"]
    if is_http:
        if "user-agent" not in normalized:
            args.extend(["-user_agent", USER_AGENT])
        args.extend(
            [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_on_network_error",
                "1",
                "-reconnect_on_http_error",
                "4xx,5xx",
                "-reconnect_delay_max",
                "5",
                "-rw_timeout",
                "15000000",
            ]
        )
    args.extend(["-fflags", "+genpts+discardcorrupt"])
    if headers:
        if not is_http:
            raise ValueError("Source headers can only be applied to HTTP(S) URLs")
        if any(
            any(char in key + value for char in "\r\n")
            for key, value in headers.items()
        ):
            raise ValueError("Source headers must not contain line breaks")
        args.extend(
            [
                "-headers",
                "".join(f"{key}: {value}\r\n" for key, value in headers.items()),
            ]
        )
    args.extend(["-i", url])
    return args


def _single_output_args(destination_url: str) -> list[str]:
    return ["-f", "flv", "-rtmp_live", "live", "-flvflags", "no_duration_filesize", destination_url]


def _recording_output_args(recording_path: str) -> list[str]:
    return [
        "-f",
        "mp4",
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        recording_path,
    ]


def build_commands(
    ffmpeg: str,
    stream_urls: tuple[str, ...],
    destinations: list[tuple[str, str]],
    profile: QualityProfile,
    source_headers: tuple[dict[str, str], ...] = (),
    pipe_mode: bool = False,
    preset: str = "veryfast",
    logo_path: str = "",
    logo_position: str = "bottom-right",
    logo_size_percent: int = 12,
    logo_opacity: float = 0.9,
    portrait: bool = False,
    frame_path: str = "",
    recording_path: str = "",
) -> list[list[str]]:

    if not pipe_mode and not stream_urls:
        raise ValueError("No source stream URLs were provided")
    if recording_path:
        if not recording_path.endswith(".mp4"):
            raise ValueError("recording_path must point to an .mp4 file")
        if not os.path.isdir(os.path.dirname(recording_path) or "."):
            raise ValueError("The recording directory does not exist")
    destinations = list(destinations)
    if not destinations:
        raise ValueError("At least one RTMP destination is required")
    if logo_path:
        if logo_position not in {
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        }:
            raise ValueError("Unsupported logo position")
        if not 5 <= logo_size_percent <= 25 or not 0.1 <= logo_opacity <= 1.0:
            raise ValueError("Logo size or opacity is outside supported limits")
    if pipe_mode:
        input_args = [
            "-thread_queue_size",
            "1024",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            "pipe:0",
        ]
        source_input_count = 1
        audio_map = "0:a:0?"
    else:
        input_args = []
        source_input_count = len(stream_urls[:2])
        for index, url in enumerate(stream_urls[:2]):
            headers = source_headers[index] if index < len(source_headers) else {}
            input_args.extend(_input_arguments(url, headers))
        audio_map = "1:a:0" if len(stream_urls) > 1 else "0:a:0?"
    output_width, output_height = profile.width, profile.height
    if portrait and output_width > output_height:
        output_width, output_height = output_height, output_width
    scale = (
        f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
        f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={profile.fps}"
    )
    filter_args = ["-vf", f"{scale},format=yuv420p"]
    maps = ["-map", "0:v:0", "-map", audio_map]
    frame_index = None
    logo_index = None
    next_input_index = source_input_count
    if frame_path:
        if not os.path.isfile(frame_path):
            raise ValueError("Frame overlay image could not be found")
        frame_index = next_input_index
        next_input_index += 1
        input_args.extend(
            ["-loop", "1", "-framerate", str(profile.fps), "-i", frame_path]
        )
    if logo_path:
        logo_index = next_input_index
        input_args.extend(
            ["-loop", "1", "-framerate", str(profile.fps), "-i", logo_path]
        )
    if frame_index is not None or logo_index is not None:
        filters = [f"[0:v]{scale},format=rgba[base]"]
        current = "base"
        if frame_index is not None:
            filters.extend(
                [
                    f"[{frame_index}:v]scale={output_width}:{output_height}:flags=lanczos,format=rgba[frame]",
                    f"[{current}][frame]overlay=0:0:format=auto:shortest=1[framed]",
                ]
            )
            current = "framed"
        if logo_index is not None:
            logo_width = max(32, output_width * logo_size_percent // 100)
            margin = max(20, output_width * 3 // 100) if frame_path else max(
                12, output_width * 2 // 100
            )
            top_margin = max(margin, output_height * 12 // 100) if frame_path else margin
            bottom_margin = max(margin, output_height * 11 // 100) if frame_path else margin
            positions = {
                "top-left": (str(margin), str(top_margin)),
                "top-right": (f"W-w-{margin}", str(top_margin)),
                "bottom-left": (str(margin), f"H-h-{bottom_margin}"),
                "bottom-right": (f"W-w-{margin}", f"H-h-{bottom_margin}"),
            }
            x, y = positions[logo_position]
            filters.extend(
                [
                    f"[{logo_index}:v]scale={logo_width}:-1:flags=lanczos,format=rgba,"
                    f"colorchannelmixer=aa={logo_opacity:.3f}[logo]",
                    f"[{current}][logo]overlay={x}:{y}:format=auto:shortest=1[composed]",
                ]
            )
            current = "composed"
        filters.append(f"[{current}]format=yuv420p[v]")
        filter_args = ["-filter_complex", ";".join(filters)]
        maps = ["-map", "[v]", "-map", audio_map]
    video_kbps = int(profile.video_bitrate.removesuffix("k"))
    encode_args = [
        *filter_args,
        *maps,
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-profile:v",
        "high",
        "-b:v",
        profile.video_bitrate,
        "-minrate",
        profile.video_bitrate,
        "-maxrate",
        profile.video_bitrate,
        "-bufsize",
        f"{video_kbps * 2}k",
        "-x264-params",
        "nal-hrd=cbr:force-cfr=1",
        "-g",
        str(profile.fps * 2),
        "-keyint_min",
        str(profile.fps * 2),
        "-sc_threshold",
        "0",
        "-af",
        "aresample=async=1:first_pts=0",
        "-c:a",
        "aac",
        "-b:a",
        profile.audio_bitrate,
        "-ar",
        "44100",
        "-ac",
        "2",
        "-avoid_negative_ts",
        "make_zero",
        "-max_muxing_queue_size",
        "4096",
        "-flush_packets",
        "1",
        "-max_interleave_delta",
        "1M",
    ]
    recording_args = (
        _recording_output_args(recording_path) if recording_path else []
    )
    return [
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-stats_period",
            "5",
            "-progress",
            "pipe:1",
            *input_args,
            *encode_args,
            *_single_output_args(url),
            *(recording_args if index == 0 and recording_path else []),
        ]
        for index, (_, url) in enumerate(destinations)
    ]


def redact_urls(line: str) -> str:
    return re.sub(r"(?i)(?:https?|rtmps?)://\S+", "[stream-url-hidden]", line)
