from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RelayStatus:
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _destination_session: Any = field(default=None, repr=False)
    _metadata_overrides: dict[str, str] | None = field(default=None, repr=False)
    _data: dict[str, Any] = field(
        default_factory=lambda: {
            "mode": "idle",
            "message": "لوحة التحكم جاهزة",
            "source_platform": "",
            "title": "",
            "description": "",
            "tags": [],
            "metadata_provider": "",
            "metadata_manual": False,
            "thumbnail_provider": "",
            "quality": "",
            "bitrate": "",
            "started_at": None,
            "retry_count": 0,
            "last_error": "",
            "active_platforms": [],
            "destination_errors": {},
            "updated_at": time.time(),
        }
    )

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)
            self._data["updated_at"] = time.time()

    def set_destination_session(self, session: Any) -> None:
        with self._lock:
            self._destination_session = session

    @property
    def destination_session(self) -> Any:
        with self._lock:
            return self._destination_session

    def set_metadata_overrides(self, overrides: dict[str, str] | None) -> None:
        with self._lock:
            self._metadata_overrides = (
                {str(key): str(value) for key, value in overrides.items()}
                if overrides is not None
                else None
            )

    @property
    def metadata_overrides(self) -> dict[str, str] | None:
        with self._lock:
            return (
                dict(self._metadata_overrides)
                if self._metadata_overrides is not None
                else None
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._data)
        started_at = result.get("started_at")
        result["uptime_seconds"] = (
            max(0, int(time.time() - started_at)) if started_at else 0
        )
        return result
