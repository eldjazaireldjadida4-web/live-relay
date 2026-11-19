from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityProfile:
    name: str
    width: int
    height: int
    video_bitrate: str
    audio_bitrate: str = "128k"
    fps: int = 30


PROFILES = (
    QualityProfile("360p", 640, 360, "1000k"),
    QualityProfile("480p", 854, 480, "2000k"),
    QualityProfile("720p", 1280, 720, "4000k"),
    QualityProfile("1080p", 1920, 1080, "6000k"),
)


class AdaptiveQuality:
    def __init__(self, initial_profile: str) -> None:
        self.index = next(
            i for i, item in enumerate(PROFILES) if item.name == initial_profile
        )
        self.slow_heartbeats = 0
        self.stable_since: float | None = None

    @property
    def profile(self) -> QualityProfile:
        return PROFILES[self.index]

    def observe(
        self, speed: float | None, now: float, health: str | None = None
    ) -> str | None:
        if health in {"bad", "no_data"}:
            self.slow_heartbeats += 1
            self.stable_since = None
            if self.slow_heartbeats >= 2:
                self.slow_heartbeats = 0
                if self.index > 0:
                    self.index -= 1
                    return "lower"
            return None
        if speed is None:
            self.slow_heartbeats = 0
            self.stable_since = None
            return None
        if speed < 0.92:
            self.slow_heartbeats += 1
            self.stable_since = None
            if self.slow_heartbeats >= 3:
                self.slow_heartbeats = 0
                if self.index > 0:
                    self.index -= 1
                    return "lower"
            return None
        self.slow_heartbeats = 0
        if speed >= 1.15:
            if self.stable_since is None:
                self.stable_since = now
            elif now - self.stable_since >= 600 and self.index < len(PROFILES) - 1:
                self.index += 1
                self.stable_since = now
                return "raise"
        else:
            self.stable_since = None
        return None
