from __future__ import annotations

import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from live_relay.thumbnails import (
    get_thumbnail_path,
    inspect_thumbnail,
    save_thumbnail,
)


def make_png(width: int = 1280, height: int = 720) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        crc = zlib.crc32(kind + content) & 0xFFFFFFFF
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanline = b"\x00" + b"\x00\x00\x00\xff" * width
    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", header)
    image += chunk(b"IDAT", zlib.compress(scanline * height))
    image += chunk(b"IEND", b"")
    return image


class ThumbnailTests(unittest.TestCase):
    def test_png_dimensions_and_recommended_shape(self):
        info = inspect_thumbnail(make_png())
        self.assertEqual(info["mime_type"], "image/png")
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        self.assertTrue(info["recommended"])

    def test_small_png_is_valid_but_not_recommended(self):
        info = inspect_thumbnail(make_png(640, 360))
        self.assertFalse(info["recommended"])

    def test_invalid_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "PNG and JPEG"):
            inspect_thumbnail(b"not an image")

    def test_saved_thumbnail_is_atomic_private_and_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            path, info = save_thumbnail(state_dir, make_png(640, 360))
            self.assertTrue(path.is_file())
            self.assertEqual(path, get_thumbnail_path(state_dir))
            self.assertEqual(path.read_bytes(), make_png(640, 360))
            self.assertEqual(info["width"], 640)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
