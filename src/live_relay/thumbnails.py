from __future__ import annotations

import logging
import os
import tempfile
import zlib
from pathlib import Path
from typing import Any

from .config import ensure_private_dir

MAX_THUMBNAIL_SIZE = 50_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def inspect_thumbnail(data: bytes) -> dict[str, Any]:
    if not data:
        raise ValueError("Choose a non-empty PNG or JPEG image")
    if len(data) > MAX_THUMBNAIL_SIZE:
        raise ValueError("The thumbnail must be 50 MB or smaller")
    if data.startswith(PNG_SIGNATURE):
        if (
            len(data) < 33
            or int.from_bytes(data[8:12], "big") != 13
            or data[12:16] != b"IHDR"
            or data[-8:-4] != b"IEND"
        ):
            raise ValueError("The PNG file is incomplete or invalid")
        expected_crc = int.from_bytes(data[29:33], "big")
        actual_crc = zlib.crc32(data[12:29]) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("The PNG header checksum is invalid")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        mime_type = "image/png"
        extension = ".png"
    elif data.startswith(b"\xff\xd8"):
        if len(data) < 12 or not data.endswith(b"\xff\xd9"):
            raise ValueError("The JPEG file is incomplete or invalid")
        width = height = 0
        index = 2
        while index < len(data) - 4:
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                raise ValueError("The JPEG file is incomplete or invalid")
            if marker in JPEG_SOF_MARKERS:
                if segment_length < 7:
                    raise ValueError("The JPEG image dimensions are invalid")
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                break
            index += segment_length
        if width < 1 or height < 1:
            raise ValueError("The JPEG image dimensions could not be read")
        mime_type = "image/jpeg"
        extension = ".jpg"
    else:
        raise ValueError("Only PNG and JPEG thumbnail images are supported")
    if width < 1 or height < 1:
        raise ValueError("The thumbnail image dimensions are invalid")
    ratio = width / height
    recommended = width >= 1280 and height >= 720 and abs(ratio - 16 / 9) <= 0.02
    return {
        "mime_type": mime_type,
        "extension": extension,
        "width": width,
        "height": height,
        "recommended": recommended,
        "size_bytes": len(data),
    }


def get_thumbnail_path(state_dir: Path) -> Path | None:
    for extension in (".png", ".jpg"):
        path = Path(state_dir) / f"youtube-thumbnail{extension}"
        if path.is_file():
            return path
    return None


MANUAL_HOLD_NAME = "thumbnail-manual"


def set_manual_hold(state_dir: Path, held: bool) -> None:
    state_dir = Path(state_dir)
    marker = state_dir / MANUAL_HOLD_NAME
    try:
        if held:
            ensure_private_dir(state_dir)
            marker.touch(exist_ok=True)
        else:
            marker.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.warning("Could not update the manual thumbnail hold: %s", exc)


def manual_hold(state_dir: Path) -> bool:
    return (Path(state_dir) / MANUAL_HOLD_NAME).is_file()


def clear_thumbnail(state_dir: Path) -> None:
    state_dir = Path(state_dir)
    for extension in (".png", ".jpg"):
        try:
            (state_dir / f"youtube-thumbnail{extension}").unlink()
        except FileNotFoundError:
            pass
    set_manual_hold(state_dir, False)


def save_thumbnail(state_dir: Path, data: bytes) -> tuple[Path, dict[str, Any]]:
    info = inspect_thumbnail(data)
    state_dir = Path(state_dir)
    ensure_private_dir(state_dir)
    target = state_dir / f"youtube-thumbnail{info['extension']}"
    fd, temporary = tempfile.mkstemp(prefix=".youtube-thumbnail-", dir=state_dir)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    for extension in (".png", ".jpg"):
        other = state_dir / f"youtube-thumbnail{extension}"
        if other != target:
            try:
                other.unlink()
            except FileNotFoundError:
                pass
    return target, info
