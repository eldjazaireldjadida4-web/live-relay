from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_TIMEOUT_SECONDS = 10

_EVENT_LABELS = {
    "ar": {
        "session_started": "🔴 بدأ البث المباشر",
        "destination_failed": "⚠️ مقاطعة في وجهة بث",
        "relay_ended": "⏹️ انتهى البث",
        "relay_stopped": "⏹️ تم إيقاف المُرحِّل",
    },
    "en": {
        "session_started": "🔴 Live relay started",
        "destination_failed": "⚠️ Destination interrupted",
        "relay_ended": "⏹️ Live relay ended",
        "relay_stopped": "⏹️ Relay stopped",
    },
}

_FIELD_LABELS = {
    "ar": {
        "title": "العنوان",
        "platforms": "الوجهات",
        "platform": "الوجهة",
        "reason": "السبب",
        "quality": "الجودة",
        "duration": "المدة",
        "reconnect_in": "إعادة المحاولة بعد",
    },
    "en": {
        "title": "Title",
        "platforms": "Destinations",
        "platform": "Destination",
        "reason": "Reason",
        "quality": "Quality",
        "duration": "Duration",
        "reconnect_in": "Reconnect in",
    },
}


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@dataclass(frozen=True)
class Notifier:
    telegram_token: str = ""
    telegram_chat: str = ""
    webhook_url: str = ""
    language: str = "ar"

    @property
    def enabled(self) -> bool:
        return bool(self.telegram_token or self.webhook_url)

    def notify(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        payload = {
            key: value
            for key, value in fields.items()
            if value not in (None, "", [], {})
        }
        sender = threading.Thread(
            target=self._deliver,
            args=(event, payload),
            name=f"relay-notify-{event}",
            daemon=True,
        )
        sender.start()

    def _deliver(self, event: str, fields: dict[str, Any]) -> None:
        try:
            if self.webhook_url:
                self._post_json(self.webhook_url, self._webhook_payload(event, fields))
        except Exception as exc:
            logging.debug("Webhook notification failed: %s", exc)
        try:
            if self.telegram_token:
                self._post_json(
                    f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                    self._telegram_payload(event, fields),
                )
        except Exception as exc:
            logging.debug("Telegram notification failed: %s", exc)

    def _webhook_payload(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in fields.items()
            if value not in (None, "", [], {})
        }
        return {
            "event": event,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **clean,
        }

    def _telegram_payload(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        labels = _EVENT_LABELS.get(self.language, _EVENT_LABELS["ar"])
        field_labels = _FIELD_LABELS.get(self.language, _FIELD_LABELS["ar"])
        lines = [labels.get(event, event.replace("_", " ").title())]
        duration = fields.pop("duration", None)
        if duration is not None and isinstance(duration, (int, float)):
            fields["duration"] = _format_duration(duration)
        for key, value in fields.items():
            label = field_labels.get(key, key)
            lines.append(f"{label}: {value}")
        return {
            "chat_id": self.telegram_chat,
            "text": "\n".join(lines),
            "disable_web_page_preview": True,
        }

    def _post_json(self, url: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            logging.debug(
                "Notification endpoint returned HTTP %s", exc.code
            )


def notifier_from_settings(settings: Any) -> Notifier:
    return Notifier(
        telegram_token=getattr(settings, "notify_telegram_token", ""),
        telegram_chat=getattr(settings, "notify_telegram_chat", ""),
        webhook_url=getattr(settings, "notify_webhook_url", ""),
        language=getattr(
            settings,
            "notify_language",
            getattr(settings, "metadata_language", "ar"),
        )
        or "ar",
    )


def recording_file_path(state_dir: os.PathLike | str, platform: str) -> str:
    slug = "".join(
        char if char.isalnum() else "-"
        for char in platform.strip().lower()
    ).strip("-") or "relay"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = os.path.join(str(state_dir), "recordings")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{stamp}-{slug}.mp4")
