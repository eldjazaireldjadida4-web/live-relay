"""Regression tests for v4.8.13: the fifth audit round on the 4.8.12 code.

- T1: ``run()`` called ``resolve_source(settings)`` synchronously inside
  ``while not stopped.is_set()``. A stop issued during the (up to 90 s)
  yt-dlp lookup was not honoured until the lookup returned, and the
  dashboard only joins the relay thread for 30 s, so the stop was
  reported as timed out. The lookup now runs on a daemon thread and
  the loop polls the stop event while waiting, so ``run()`` exits
  promptly; a lookup error is still re-raised in the caller.
- T2: the 4.8.12 quota merge read the on-disk count under only the
  instance's own RLock, so two tracker instances recording
  concurrently both read the same value and one unit was lost per
  collision (2 threads x 400 record(1) gave 479 instead of 800).
  The read-merge-save cycle in ``record()`` is now serialised by a
  process-wide lock keyed by the state path.
- T3: pipe mode never validated the cookie file, unlike
  ``resolve_source()`` which returns None with a clear log when
  YTDLP_COOKIE_FILE is missing. If the file vanished mid-run, every
  pipe restart spawned a yt-dlp that exited immediately, ``restart()``
  still returned True, and the worker looped with a generic "FFmpeg
  exited" reason. ``SharedSourcePipe.start()`` now fails with a clear
  reason naming the missing cookie file, and the worker start reports
  that reason instead of the generic one.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_relay.destinations import YouTubeQuotaTracker
from live_relay.quality import AdaptiveQuality
from live_relay.service import WorkerState, _start_worker, run
from live_relay.source import SharedSourcePipe


def _run_settings(**overrides):
    values = {
        "ffmpeg_path": "",
        "source_url": "https://www.tiktok.com/@x/live",
        "source_platform": "auto",
        "initial_profile": "720p",
        "source_retry": 1,
        "reconnect_delay": 1,
        "source_end_grace": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pipe_settings(cookie_file: str, **overrides):
    values = {
        "format_selector": "best",
        "source_url": "https://www.tiktok.com/@x/live",
        "cookie_file": cookie_file,
        "cookies_from_browser": "",
        "pipe_mode": True,
        "ffmpeg_preset": "veryfast",
        "logo_enabled": False,
        "logo_file": "",
        "logo_position": "bottom-right",
        "logo_size_percent": 12,
        "logo_opacity": 0.9,
        "frame_enabled": False,
        "frame_file": "",
        "record_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RunStopTests(unittest.TestCase):
    def test_run_returns_promptly_when_stop_is_set_during_a_slow_resolve(self):
        stopped = threading.Event()
        settings = _run_settings()
        outcome: dict[str, object] = {}

        def slow_resolve(_settings):
            time.sleep(8.0)
            return None

        def _target() -> None:
            try:
                run(settings, stopped)
            except Exception as exc:  # noqa: BLE001 - captured for the test
                outcome["error"] = exc
            finally:
                outcome["finished"] = True

        with (
            patch(
                "live_relay.service.resolve_source",
                side_effect=slow_resolve,
            ),
            patch("live_relay.service.resolve_ffmpeg", return_value="ffmpeg"),
        ):
            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            # The lookup is still in flight here: the old code blocked
            # in it for the full 8 s, far past a 30 s dashboard join
            # only when the lookup itself ran long — here the stop must
            # cut the wait short well before the lookup finishes.
            stop_at = time.monotonic() + 1.0
            threading.Timer(1.0, stopped.set).start()
            thread.join(timeout=6.0)
            joined_in = time.monotonic() - stop_at
        self.assertFalse(thread.is_alive())
        self.assertLess(joined_in, 4.0)
        self.assertNotIn("error", outcome)

    def test_run_reraises_a_lookup_error(self):
        stopped = threading.Event()
        settings = _run_settings()
        outcome: dict[str, object] = {}

        def _target() -> None:
            try:
                run(settings, stopped)
            except Exception as exc:  # noqa: BLE001 - captured for the test
                outcome["error"] = exc

        with (
            patch(
                "live_relay.service.resolve_source",
                side_effect=RuntimeError("boom"),
            ),
            patch("live_relay.service.resolve_ffmpeg", return_value="ffmpeg"),
        ):
            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome.get("error"), RuntimeError)
        self.assertEqual(str(outcome["error"]), "boom")


class QuotaConcurrentTests(unittest.TestCase):
    def test_two_trackers_recording_concurrently_lose_no_units(self):
        state_path = (
            Path(tempfile.mkdtemp(prefix="quota-concurrent-"))
            / "youtube-quota.json"
        )
        first = YouTubeQuotaTracker(state_path=state_path)
        second = YouTubeQuotaTracker(state_path=state_path)

        def _record(tracker, count):
            for _ in range(count):
                tracker.record(1)

        threads = [
            threading.Thread(target=_record, args=(first, 400)),
            threading.Thread(target=_record, args=(second, 400)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        # A fresh tracker reads the authoritative on-disk total: every
        # unit from both instances must be there, none lost to a
        # read-merge-save collision.
        reloaded = YouTubeQuotaTracker(state_path=state_path)
        self.assertEqual(reloaded.used, 800)


class SharedPipeCookieTests(unittest.TestCase):
    def test_missing_cookie_file_fails_pipe_start_with_a_clear_reason(self):
        missing = "/nonexistent/cookies-for-live-relay.txt"
        settings = _pipe_settings(cookie_file=missing)
        pipe = SharedSourcePipe(settings)
        with patch(
            "live_relay.source.subprocess.Popen",
            side_effect=AssertionError("no process must be spawned"),
        ):
            started = pipe.start()
        self.assertFalse(started)
        self.assertIn(missing, pipe.last_error)
        self.assertIn("YTDLP_COOKIE_FILE", pipe.last_error)

    def test_worker_start_reports_the_missing_cookie_file(self):
        missing = "/nonexistent/cookies-for-live-relay.txt"
        settings = _pipe_settings(cookie_file=missing)
        pipe = SharedSourcePipe(settings)
        worker = WorkerState(name="youtube", index=0)
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        session = SimpleNamespace(targets=[("youtube", "rtmp://a.fake/y")])
        quality = AdaptiveQuality("720p")
        with patch(
            "live_relay.service.subprocess.Popen",
            side_effect=AssertionError("no process must be spawned"),
        ):
            started = _start_worker(
                worker, "ffmpeg", source, session, quality, settings, pipe
            )
        self.assertFalse(started)
        self.assertIn(missing, worker.failure_reason)
        self.assertIn("YTDLP_COOKIE_FILE", worker.failure_reason)

    def test_per_worker_pipe_start_reports_the_missing_cookie_file(self):
        missing = "/nonexistent/cookies-for-live-relay.txt"
        settings = _pipe_settings(cookie_file=missing)
        worker = WorkerState(name="youtube", index=0)
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        session = SimpleNamespace(targets=[("youtube", "rtmp://a.fake/y")])
        quality = AdaptiveQuality("720p")
        with patch(
            "live_relay.service.subprocess.Popen",
            side_effect=AssertionError("no process must be spawned"),
        ):
            started = _start_worker(
                worker, "ffmpeg", source, session, quality, settings
            )
        self.assertFalse(started)
        self.assertIn(missing, worker.failure_reason)
        self.assertIn("YTDLP_COOKIE_FILE", worker.failure_reason)


if __name__ == "__main__":
    unittest.main()
