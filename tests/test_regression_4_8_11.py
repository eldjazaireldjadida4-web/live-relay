"""Regression tests for v4.8.11: the persistent YouTube quota counter.

- Q1: YouTubeQuotaTracker was purely in-memory: a restart mid-day reset
  the count to zero, so the relay reported a full budget while the real
  daily quota was already partly spent, and could push YouTube over its
  10,000-unit day. The count is now persisted in the state dir
  (youtube-quota.json, next to youtube-stream.json) and restored on
  startup; usage from a previous day is ignored and the count resets
  at midnight UTC as before.
- Q2: a missing, corrupt or unreadable state file must never brick
  startup or a stream: the tracker starts at zero and a failed write
  is logged while the in-memory count still stands.
- Q3: the state file is written atomically (temp file + os.replace,
  0600), the same pattern as youtube-stream.json, so a crash mid-write
  can never leave a half-written count behind.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from live_relay.destinations import (
    YOUTUBE_QUOTA_STATE_FILENAME,
    YouTubeDestination,
    YouTubeQuotaTracker,
)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _yesterday() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


class QuotaPersistenceTests(unittest.TestCase):
    def test_usage_survives_a_restart_on_the_same_day(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            tracker = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            tracker.record(250)
            self.assertEqual(tracker.used, 250)

            restarted = YouTubeQuotaTracker(
                budget=10000, state_path=state_path
            )
            self.assertEqual(restarted.used, 250)
            self.assertEqual(restarted.remaining, 9750)

            restarted.record(50)
            reloaded = YouTubeQuotaTracker(
                budget=10000, state_path=state_path
            )
            self.assertEqual(reloaded.used, 300)

    def test_yesterdays_usage_does_not_carry_over(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            state_path.write_text(
                json.dumps({"day": _yesterday(), "used": 9999}),
                encoding="utf-8",
            )
            tracker = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            self.assertEqual(tracker.used, 0)
            tracker.record(1)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"day": _today(), "used": 1})

    def test_corrupt_state_file_starts_at_zero_and_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            state_path.write_text("not json at all", encoding="utf-8")
            tracker = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            self.assertEqual(tracker.used, 0)
            tracker.record(50)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"day": _today(), "used": 50})

    def test_wrongly_shaped_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            for bad in (
                json.dumps({"day": _today(), "used": "250"}),
                json.dumps({"day": _today()}),
                json.dumps([250]),
            ):
                state_path.write_text(bad, encoding="utf-8")
                tracker = YouTubeQuotaTracker(
                    budget=10000, state_path=state_path
                )
                self.assertEqual(tracker.used, 0)

    def test_missing_state_dir_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = (
                Path(directory) / "nested" / YOUTUBE_QUOTA_STATE_FILENAME
            )
            tracker = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            tracker.record(10)
            self.assertTrue(state_path.is_file())
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                {"day": _today(), "used": 10},
            )

    def test_state_file_is_private_and_has_no_temp_leftovers(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            tracker = YouTubeQuotaTracker(budget=10000, state_path=state_path)
            tracker.record(10)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(
                any(path.suffix == ".tmp" for path in Path(directory).iterdir())
            )

    def test_no_state_path_stays_in_memory_only(self):
        tracker = YouTubeQuotaTracker(budget=10000)
        tracker.record(250)
        self.assertEqual(tracker.used, 250)
        self.assertIsNone(tracker._state_path)

    def test_concurrent_records_persist_consistently(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / YOUTUBE_QUOTA_STATE_FILENAME
            tracker = YouTubeQuotaTracker(budget=100000, state_path=state_path)
            # Each thread records 100 units; the file must agree with
            # the in-memory count afterwards.
            def _record_many() -> None:
                for _ in range(100):
                    tracker.record(1)

            threads = [threading.Thread(target=_record_many) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(tracker.used, 400)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"day": _today(), "used": 400})


class QuotaWiringTests(unittest.TestCase):
    def test_destination_persists_quota_in_its_state_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            settings = SimpleNamespace(
                youtube_quota_budget=10000, state_dir=state_dir
            )
            destination = YouTubeDestination(settings)
            self.assertEqual(
                destination.quota._state_path,
                state_dir / YOUTUBE_QUOTA_STATE_FILENAME,
            )
            destination.quota.record(250)
            restarted = YouTubeDestination(settings)
            self.assertEqual(restarted.quota.used, 250)

    def test_destination_without_a_state_dir_does_not_crash(self):
        for settings in (SimpleNamespace(), SimpleNamespace(state_dir=None)):
            destination = YouTubeDestination(settings)
            self.assertIsNone(destination.quota._state_path)
            destination.quota.record(10)
            self.assertEqual(destination.quota.used, 10)


if __name__ == "__main__":
    unittest.main()
