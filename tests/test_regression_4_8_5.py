"""Regression tests for v4.8.5: per-destination recovery (H6) and the
session-level circuit breaker (H7).

H7: encoder supervision rebuilds its worker state on every re-entry, so a
destination that fails permanently used to retry forever at full speed
(and flood notifications on every ffmpeg exit). Failure counting now
lives on the session: after five consecutive failures a destination
cools down with an escalating delay before the next probe, notifies once
when the breaker opens and once when it recovers after a stable run.

H6: a dead destination used to stay dead until every destination gave
up. Now the breaker-open moment recreates just that destination's
platform event (a new Facebook live video, or a new YouTube broadcast
when the old one is provably dead), while healthy destinations keep
streaming. A saved YouTube stream in "error" state is no longer reused.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_relay.destinations import (
    DestinationSession,
    YouTubeDestination,
    recreate_destination,
)
from live_relay.metadata import StreamMetadata
from live_relay.quality import AdaptiveQuality
from live_relay.service import (
    _breaker_cooldown,
    _destination_health,
    _note_worker_failure,
    _record_destination_stable,
    _session_is_dead,
    _start_encoder,
    WorkerState,
)

METADATA = StreamMetadata("عنوان البث", "الوصف", ("وسم",))


def _session_with_targets(*targets: tuple[str, str]) -> DestinationSession:
    session = DestinationSession(metadata=METADATA, base_metadata=METADATA)
    session.targets = list(targets)
    return session


class BreakerCooldownTests(unittest.TestCase):
    def test_cooldown_doubles_each_trip_up_to_the_cap(self):
        self.assertEqual(_breaker_cooldown(1), 60.0)
        self.assertEqual(_breaker_cooldown(2), 120.0)
        self.assertEqual(_breaker_cooldown(3), 240.0)
        self.assertEqual(_breaker_cooldown(4), 480.0)
        self.assertEqual(_breaker_cooldown(5), 600.0)
        self.assertEqual(_breaker_cooldown(9), 600.0)


class NoteWorkerFailureTests(unittest.TestCase):
    def test_five_failures_open_the_breaker_and_notify_once(self):
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        worker = WorkerState(name="youtube", index=0)
        notifier = Mock()
        now = time.monotonic()
        for _ in range(5):
            _note_worker_failure(session, worker, notifier, now, "boom")
        health = _destination_health(session, "youtube")
        self.assertTrue(health.breaker_open)
        self.assertTrue(worker.exhausted)
        self.assertEqual(health.breaker_trips, 1)
        self.assertAlmostEqual(health.next_retry_at, now + 60.0, delta=1.0)
        self.assertEqual(health.total_failures, 5)
        notifier.notify.assert_called_once()
        event = notifier.notify.call_args.args[0]
        self.assertEqual(event, "destination_failed")
        # A sixth failure while the breaker is already open does not
        # re-notify and does not re-trip.
        _note_worker_failure(session, worker, notifier, now, "boom")
        self.assertEqual(health.breaker_trips, 1)
        notifier.notify.assert_called_once()

    def test_failures_survive_worker_recreation(self):
        """The counter lives on the session, so a fresh WorkerState on
        the next supervision re-entry does not reset the budget."""
        session = _session_with_targets(("facebook", "rtmp://a.fake/f"))
        notifier = Mock()
        now = time.monotonic()
        for _ in range(3):
            _note_worker_failure(
                session, WorkerState(name="facebook", index=0), notifier, now, "x"
            )
        for _ in range(2):
            _note_worker_failure(
                session, WorkerState(name="facebook", index=0), notifier, now, "x"
            )
        health = _destination_health(session, "facebook")
        self.assertTrue(health.breaker_open)
        self.assertEqual(health.consecutive_failures, 5)
        notifier.notify.assert_called_once()

    def test_stable_run_resets_the_breaker_and_notifies_recovery(self):
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        worker = WorkerState(name="youtube", index=0)
        notifier = Mock()
        now = time.monotonic()
        for _ in range(5):
            _note_worker_failure(session, worker, notifier, now, "boom")
        _record_destination_stable(session, "youtube", notifier)
        health = _destination_health(session, "youtube")
        self.assertFalse(health.breaker_open)
        self.assertEqual(health.consecutive_failures, 0)
        self.assertEqual(health.breaker_trips, 0)
        self.assertEqual(notifier.notify.call_count, 2)
        self.assertEqual(
            notifier.notify.call_args_list[1].args[0], "destination_recovered"
        )
        # A second stable period does not notify again.
        _record_destination_stable(session, "youtube", notifier)
        self.assertEqual(notifier.notify.call_count, 2)

    def test_stable_run_without_a_trip_notifies_nothing(self):
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        notifier = Mock()
        _record_destination_stable(session, "youtube", notifier)
        notifier.notify.assert_not_called()


class RecreateDestinationTests(unittest.TestCase):
    def test_facebook_creates_a_new_video_and_ends_the_old_one(self):
        session = _session_with_targets(("facebook", "rtmp://old/f"))
        facebook = Mock()
        facebook.live_video_id = "old-id"
        facebook.start.return_value = [("facebook", "rtmp://new/f")]
        session.facebook = facebook
        url = recreate_destination(session, "facebook", METADATA)
        self.assertEqual(url, "rtmp://new/f")
        self.assertEqual(session.targets, [("facebook", "rtmp://new/f")])
        facebook.start.assert_called_once_with(METADATA)
        facebook.end_live_video.assert_called_once_with("old-id")

    def test_facebook_start_failure_keeps_the_old_video(self):
        session = _session_with_targets(("facebook", "rtmp://old/f"))
        facebook = Mock()
        facebook.live_video_id = "old-id"
        facebook.start.side_effect = RuntimeError("Graph API down")
        session.facebook = facebook
        with self.assertRaises(RuntimeError):
            recreate_destination(session, "facebook", METADATA)
        facebook.end_live_video.assert_not_called()
        self.assertEqual(session.targets, [("facebook", "rtmp://old/f")])

    def test_static_destinations_are_never_recreated(self):
        session = _session_with_targets(("facebook", "rtmp://static/f"))
        session.facebook = None
        self.assertIsNone(recreate_destination(session, "facebook", METADATA))
        session = _session_with_targets(("youtube", "rtmp://static/y"))
        session.youtube = None
        self.assertIsNone(recreate_destination(session, "youtube", METADATA))

    def test_youtube_live_broadcast_is_kept(self):
        session = _session_with_targets(("youtube", "rtmp://old/y"))
        youtube = Mock()
        youtube.check_status.return_value = {
            "broadcast_status": "live",
            "stream_status": "active",
            "stream_health": "good",
        }
        session.youtube = youtube
        self.assertIsNone(recreate_destination(session, "youtube", METADATA))
        youtube.start.assert_not_called()

    def test_youtube_dead_broadcast_is_replaced(self):
        session = _session_with_targets(("youtube", "rtmp://old/y"))
        youtube = Mock()
        youtube.check_status.return_value = {
            "broadcast_status": "complete",
            "stream_status": "inactive",
            "stream_health": "unknown",
        }
        youtube.broadcast = {"id": "old-broadcast"}
        youtube.start.return_value = [("youtube", "rtmp://new/y")]
        session.youtube = youtube
        url = recreate_destination(session, "youtube", METADATA)
        self.assertEqual(url, "rtmp://new/y")
        self.assertEqual(session.targets, [("youtube", "rtmp://new/y")])
        youtube.end_broadcast.assert_called_once_with({"id": "old-broadcast"})

    def test_youtube_error_stream_is_replaced(self):
        session = _session_with_targets(("youtube", "rtmp://old/y"))
        youtube = Mock()
        youtube.check_status.return_value = {
            "broadcast_status": "live",
            "stream_status": "error",
            "stream_health": "bad",
        }
        youtube.broadcast = {"id": "old-broadcast"}
        youtube.start.return_value = [("youtube", "rtmp://new/y")]
        session.youtube = youtube
        self.assertEqual(
            recreate_destination(session, "youtube", METADATA), "rtmp://new/y"
        )

    def test_youtube_uncheckable_broadcast_is_kept(self):
        session = _session_with_targets(("youtube", "rtmp://old/y"))
        youtube = Mock()
        youtube.check_status.side_effect = RuntimeError("API down")
        session.youtube = youtube
        self.assertIsNone(recreate_destination(session, "youtube", METADATA))
        youtube.start.assert_not_called()


class GetStreamReuseTests(unittest.TestCase):
    def _destination_with_saved_stream(self, stream_status: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_dir = Path(tmp.name)
        (state_dir / "youtube-stream.json").write_text(
            json.dumps({"stream_id": "saved-stream"}), encoding="utf-8"
        )
        settings = SimpleNamespace(
            state_dir=state_dir,
            youtube_client_secrets="secrets.json",
            youtube_privacy="public",
            youtube_backup=False,
        )
        destination = YouTubeDestination(settings)
        service = Mock()
        list_response = Mock()
        list_response.execute.return_value = {
            "items": [
                {
                    "id": "saved-stream",
                    "status": {"streamStatus": stream_status},
                    "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a/y"}},
                }
            ]
        }
        insert_response = Mock()
        insert_response.execute.return_value = {
            "id": "fresh-stream",
            "status": {"streamStatus": "ready"},
            "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a/y"}},
        }
        service.liveStreams.return_value.list.return_value = list_response
        service.liveStreams.return_value.insert.return_value = insert_response
        destination.service = service
        return destination, service

    def test_error_stream_is_not_reused(self):
        destination, service = self._destination_with_saved_stream("error")
        stream = destination._get_stream()
        self.assertEqual(stream["id"], "fresh-stream")
        service.liveStreams.return_value.insert.assert_called_once()

    def test_created_stream_is_not_reused(self):
        destination, service = self._destination_with_saved_stream("created")
        stream = destination._get_stream()
        self.assertEqual(stream["id"], "fresh-stream")

    def test_ready_stream_is_reused(self):
        destination, service = self._destination_with_saved_stream("ready")
        stream = destination._get_stream()
        self.assertEqual(stream["id"], "saved-stream")
        service.liveStreams.return_value.insert.assert_not_called()


class SessionIsDeadTests(unittest.TestCase):
    def _session(self, status: dict[str, str]) -> DestinationSession:
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        youtube = Mock()
        youtube.check_status.return_value = status
        session.youtube = youtube
        return session

    def test_error_stream_counts_as_dead(self):
        session = self._session(
            {
                "broadcast_status": "live",
                "stream_status": "error",
                "stream_health": "bad",
            }
        )
        self.assertTrue(_session_is_dead(session))

    def test_live_broadcast_with_active_stream_is_not_dead(self):
        session = self._session(
            {
                "broadcast_status": "live",
                "stream_status": "active",
                "stream_health": "good",
            }
        )
        self.assertFalse(_session_is_dead(session))


class BreakerIntegrationTests(unittest.TestCase):
    def test_permanently_failing_destination_stops_spawning_after_five(self):
        """A worker that exits immediately is retried five times, then
        its breaker opens and no further ffmpeg process is spawned while
        the loop keeps waiting for the probe time instead of giving up."""
        stopped = threading.Event()
        settings = SimpleNamespace(
            stall_timeout=45,
            heartbeat=3600,
            adaptive_quality=False,
            pipe_mode=False,
            ffmpeg_preset="veryfast",
            logo_enabled=False,
            logo_file="",
            logo_position="bottom-right",
            logo_size_percent=12,
            logo_opacity=0.9,
        )
        source = SimpleNamespace(urls=("u",), headers=(), width=0, height=0)
        session = _session_with_targets(("youtube", "rtmp://a.fake/y"))
        quality = AdaptiveQuality("720p")
        notifier = Mock()

        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = 1
                self.stdout = None
                self.stderr = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

        starts: list[str] = []

        def fake_start(worker, *_args, **_kwargs):
            starts.append(worker.name)
            worker.process = FakeProcess()
            worker.started_at = time.monotonic()
            return True

        def stop_when_cooling():
            while not stopped.is_set():
                health = session.health.get("youtube")
                if health is not None and health.breaker_open:
                    time.sleep(1.0)
                    # One extra second of cooldown must not spawn a 6th process.
                    self.assertEqual(len(starts), 5)
                    stopped.set()
                    return
                time.sleep(0.1)

        watcher = threading.Thread(target=stop_when_cooling, daemon=True)
        watcher.start()
        try:
            with (
                patch("live_relay.service._start_worker", side_effect=fake_start),
                patch("live_relay.service._worker_backoff", return_value=0.0),
                patch(
                    "live_relay.service.resolve_source", return_value=source
                ),
            ):
                reason, _ = _start_encoder(
                    "ffmpeg",
                    source,
                    session,
                    quality,
                    settings,
                    stopped,
                    None,
                    notifier,
                )
        finally:
            stopped.set()
            watcher.join(timeout=5)
        self.assertEqual(reason, "stopped")
        self.assertEqual(starts, ["youtube"] * 5)
        health = _destination_health(session, "youtube")
        self.assertTrue(health.breaker_open)
        notifier.notify.assert_called_once()
        self.assertEqual(
            notifier.notify.call_args.args[0], "destination_failed"
        )


if __name__ == "__main__":
    unittest.main()
