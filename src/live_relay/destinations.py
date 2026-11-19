from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import Settings, ensure_private_dir
from .encoder import redact_urls
from .metadata import StreamMetadata
from .source import clean_title
from .thumbnails import get_thumbnail_path

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


@dataclass
class DestinationSession:
    targets: list[tuple[str, str]] = field(default_factory=list)
    youtube: "YouTubeDestination | None" = None
    facebook: "FacebookDestination | None" = None
    failures: dict[str, str] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock, repr=False)
    metadata: StreamMetadata | None = None
    base_metadata: StreamMetadata | None = None
    manual_metadata_override: bool = False
    ignore_config_overrides: bool = False

    @property
    def active_platforms(self) -> list[str]:
        platforms = (
            "youtube" if name.startswith("youtube") else name
            for name, _ in self.targets
        )
        return list(dict.fromkeys(platforms))


def _coerce_metadata(value: StreamMetadata | str, settings: Any) -> StreamMetadata:
    if isinstance(value, StreamMetadata):
        return value
    return StreamMetadata(
        title=clean_title(str(value)),
        description="",
        tags=(),
        category_id=str(getattr(settings, "youtube_category_id", "22")),
    )


class YouTubeDestination:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service: Any = None
        self.broadcast: dict[str, Any] | None = None
        self.stream: dict[str, Any] | None = None
        self.metadata: StreamMetadata | None = None

    def _load_credentials(self, allow_browser: bool) -> tuple[Path, Any]:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        client_file = Path(self.settings.youtube_client_secrets).expanduser()
        if not client_file.is_file():
            raise RuntimeError("YOUTUBE_CLIENT_SECRETS_FILE does not exist")
        token_file = self.settings.state_dir / "youtube-token.json"
        ensure_private_dir(token_file.parent)
        credentials = None
        if token_file.is_file():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(token_file), [YOUTUBE_SCOPE]
                )
            except (ValueError, OSError):
                logging.warning(
                    "Saved YouTube authorization is unreadable; signing in again"
                )
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception:
                logging.warning(
                    "Saved YouTube authorization expired or was revoked; signing in again"
                )
                credentials = None
        if (not credentials or not credentials.valid) and self.settings.youtube_manual_auth:
            code = self.settings.youtube_manual_auth_code
            if not code:
                raise RuntimeError(
                    "YOUTUBE_MANUAL_AUTH is enabled but YOUTUBE_MANUAL_AUTH_CODE is empty"
                )
            self.manual_auth_exchange(code)
            credentials = self._load_credentials(allow_browser=False)[1]
        if (not credentials or not credentials.valid) and allow_browser:
            flow = self._oauth_flow(client_file)
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                access_type="offline",
                prompt="consent",
            )
        return token_file, credentials

    @staticmethod
    def _oauth_flow(client_file: Path) -> Any:
        from google_auth_oauthlib.flow import InstalledAppFlow

        return InstalledAppFlow.from_client_secrets_file(
            str(client_file), [YOUTUBE_SCOPE]
        )

    def _save_credentials(self, token_file: Path, credentials: Any) -> None:
        ensure_private_dir(token_file.parent)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{token_file.name}.", suffix=".tmp", dir=token_file.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as token_handle:
                token_handle.write(credentials.to_json())
                token_handle.flush()
                os.fsync(token_handle.fileno())
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            os.replace(temporary_path, token_file)
            try:
                os.chmod(token_file, 0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    def manual_auth_url(self) -> str:
        from google_auth_oauthlib.flow import InstalledAppFlow

        client_file = Path(self.settings.youtube_client_secrets).expanduser()
        if not client_file.is_file():
            raise RuntimeError("YOUTUBE_CLIENT_SECRETS_FILE does not exist")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_file), [YOUTUBE_SCOPE]
        )
        flow.redirect_uri = "http://localhost"
        self._manual_flow = flow
        authorization_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        return authorization_url

    def manual_auth_exchange(self, code: str) -> None:
        flow = getattr(self, "_manual_flow", None)
        if flow is None:
            client_file = Path(self.settings.youtube_client_secrets).expanduser()
            if not client_file.is_file():
                raise RuntimeError("YOUTUBE_CLIENT_SECRETS_FILE does not exist")
            flow = self._oauth_flow(client_file)
            flow.redirect_uri = "http://localhost"
        flow.fetch_token(code=code.strip())
        credentials = flow.credentials
        if not credentials.refresh_token:
            raise RuntimeError(
                "Google did not return a refresh token; revoke the app access at"
                " https://myaccount.google.com/permissions and authorize again"
            )
        token_file = self.settings.state_dir / "youtube-token.json"
        self._save_credentials(token_file, credentials)
        self._manual_flow = None

    def authenticate(self, allow_browser: bool = True) -> None:
        token_file, credentials = self._load_credentials(allow_browser=allow_browser)
        if not credentials or not credentials.valid:
            raise RuntimeError(
                "YouTube authorization is missing or expired. Reconnect the account from the dashboard before starting."
            )
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        channel_ids = {
            item.get("id")
            for item in service.channels()
            .list(part="id", mine=True)
            .execute()
            .get("items", [])
        }
        if self.settings.youtube_channel_id not in channel_ids:
            raise RuntimeError(
                f"Authorized YouTube account does not have access to channel {self.settings.youtube_channel_id}"
            )
        self._save_credentials(token_file, credentials)
        self.service = service

    def verify_access(self) -> dict[str, str]:
        token_file, credentials = self._load_credentials(allow_browser=False)
        if not credentials or not credentials.valid:
            raise RuntimeError(
                "No valid YouTube authorization is saved on this device; press 'ربط YouTube' once before checking."
            )
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        items = (
            service.channels()
            .list(part="id,snippet", mine=True)
            .execute()
            .get("items", [])
        )
        if not items:
            raise RuntimeError(
                "YouTube did not return a channel for the saved authorization."
            )
        channel = items[0]
        channel_id = str(channel.get("id", ""))
        if not channel_id:
            raise RuntimeError(
                "YouTube did not return a channel ID for the saved authorization."
            )
        if channel_id != self.settings.youtube_channel_id:
            raise RuntimeError(
                f"The saved YouTube authorization belongs to channel {channel_id}, not {self.settings.youtube_channel_id}"
            )
        title = str((channel.get("snippet") or {}).get("title") or "YouTube channel")
        self._save_credentials(token_file, credentials)
        return {"id": channel_id, "name": title}

    def start(self, metadata: StreamMetadata | str) -> list[tuple[str, str]]:
        self.authenticate(allow_browser=False)
        metadata = _coerce_metadata(metadata, self.settings)
        scheduled = (
            (datetime.now(timezone.utc) + timedelta(seconds=30))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        self.broadcast = (
            self.service.liveBroadcasts()
            .insert(
                part="snippet,status,contentDetails",
                body={
                    "snippet": {
                        "title": metadata.title,
                        "description": metadata.description,
                        "categoryId": metadata.category_id,
                        "scheduledStartTime": scheduled,
                    },
                    "status": {"privacyStatus": self.settings.youtube_privacy},
                    "contentDetails": {
                        "enableAutoStart": True,
                        "enableAutoStop": False,
                        "monitorStream": {"enableMonitorStream": False},
                        "recordFromStart": True,
                        "enableDvr": True,
                        "latencyPreference": "low",
                    },
                },
            )
            .execute()
        )
        self.stream = self._get_stream()
        self.service.liveBroadcasts().bind(
            part="id,contentDetails",
            id=self.broadcast["id"],
            streamId=self.stream["id"],
        ).execute()
        self.metadata = metadata
        try:
            self._update_video_metadata(metadata)
        except Exception as exc:
            logging.warning(
                "Could not synchronize YouTube tags; continuing with live broadcast: %s",
                _safe_error(exc),
            )
        state_dir = getattr(self.settings, "state_dir", None)
        thumbnail_path = get_thumbnail_path(state_dir) if state_dir else None
        if thumbnail_path:
            try:
                self.update_thumbnail(thumbnail_path)
            except Exception as exc:
                logging.warning(
                    "Saved YouTube thumbnail could not be applied: %s",
                    _safe_error(exc),
                )
        ingestion = self.stream["cdn"]["ingestionInfo"]
        stream_name = ingestion["streamName"]
        primary_address = ingestion.get("rtmpsIngestionAddress") or ingestion.get(
            "ingestionAddress"
        )
        if not primary_address:
            raise RuntimeError("YouTube did not return an RTMP(S) ingest address")
        primary = f"{primary_address.rstrip('/')}/{stream_name}"
        targets = [("youtube", primary)]
        backup_address = ingestion.get("rtmpsBackupIngestionAddress") or ingestion.get(
            "backupIngestionAddress"
        )
        if self.settings.youtube_backup and backup_address:
            targets.append(
                ("youtube-backup", f"{backup_address.rstrip('/')}/{stream_name}")
            )
        logging.info("YouTube broadcast created with automatic start enabled")
        return targets

    def check_status(self) -> dict[str, str]:
        """Read-only probe of what YouTube itself says about the broadcast and stream."""
        if not self.service or not self.broadcast or not self.stream:
            raise RuntimeError("No YouTube broadcast is open in this session yet.")
        broadcast = (
            self.service.liveBroadcasts()
            .list(part="status", id=self.broadcast["id"])
            .execute()
            .get("items", [])
        )
        stream = (
            self.service.liveStreams()
            .list(part="status", id=self.stream["id"])
            .execute()
            .get("items", [])
        )
        if not broadcast or not stream:
            raise RuntimeError(
                "YouTube no longer knows the broadcast or stream of this session."
            )
        result = {
            "broadcast_status": str(
                (broadcast[0].get("status") or {}).get("lifeCycleStatus") or "unknown"
            ),
            "stream_status": str(
                (stream[0].get("status") or {}).get("streamStatus") or "unknown"
            ),
            "stream_health": str(
                (stream[0].get("status") or {}).get("healthStatus", {}).get("status")
                or "unknown"
            ),
        }
        return result

    def ensure_live(self) -> bool:
        """Push a broadcast that is stuck in 'ready' into the live state.

        YouTube normally auto-starts the broadcast once ingest is detected,
        but frequent ingest flaps can leave it waiting forever. When the
        stream is already receiving data, transitioning directly to live
        is safe and unblocks the dashboard.
        """
        if not self.service or not self.broadcast:
            return False
        try:
            self.service.liveBroadcasts().transition(
                part="status",
                id=self.broadcast["id"],
                broadcastStatus="live",
            ).execute()
        except Exception as exc:
            logging.warning(
                "Could not force the YouTube broadcast live: %s",
                _safe_error(exc),
            )
            return False
        logging.info("YouTube broadcast transitioned to live")
        return True

    def _get_stream(self) -> dict[str, Any]:
        ensure_private_dir(self.settings.state_dir)
        state_file = self.settings.state_dir / "youtube-stream.json"
        stream = None
        try:
            stream_id = json.loads(state_file.read_text(encoding="utf-8")).get(
                "stream_id"
            )
            if stream_id:
                items = (
                    self.service.liveStreams()
                    .list(part="cdn,contentDetails,status", id=stream_id)
                    .execute()
                    .get("items", [])
                )
                # Reuse only an idle saved stream. An "active" stream may still
                # be fed by another encoder (or a previous process that has not
                # timed out yet); reusing it would hijack that ingest, so an
                # active stream gets a fresh reusable one instead. The old
                # stream is then orphaned on YouTube, which is the safe side
                # of that trade-off.
                if items and items[0].get("status", {}).get("streamStatus") != "active":
                    stream = items[0]
        except (OSError, ValueError, AttributeError, TypeError):
            stream = None
        if stream is None:
            stream = (
                self.service.liveStreams()
                .insert(
                    part="snippet,cdn,contentDetails",
                    body={
                        "snippet": {"title": "Live Relay reusable ingest"},
                        "cdn": {
                            "frameRate": "variable",
                            "ingestionType": "rtmp",
                            "resolution": "variable",
                        },
                        "contentDetails": {"isReusable": True},
                    },
                )
                .execute()
            )
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".youtube-stream.", suffix=".tmp", dir=state_file.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"stream_id": stream["id"]}))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary_path, 0o600)
                except OSError:
                    pass
                os.replace(temporary_path, state_file)
            finally:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            try:
                os.chmod(state_file, 0o600)
            except OSError:
                pass
        return stream

    def _update_video_metadata(self, metadata: StreamMetadata) -> None:
        self.service.videos().update(
            part="snippet",
            body={
                "id": self.broadcast["id"],
                "snippet": {
                    "title": metadata.title,
                    "description": metadata.description,
                    "categoryId": metadata.category_id,
                    "tags": list(metadata.tags),
                },
            },
        ).execute()


    def update_thumbnail(self, image_path: Path) -> None:
        if not self.broadcast or not self.service:
            raise RuntimeError("A YouTube API broadcast is not active")
        image_path = Path(image_path)
        if not image_path.is_file():
            raise ValueError("The saved YouTube thumbnail file is missing")
        mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        self.service.thumbnails().set(
            videoId=self.broadcast["id"],
            media_body=MediaFileUpload(
                str(image_path), mimetype=mime_type, resumable=True
            ),
        ).execute()
        logging.info("Custom YouTube thumbnail applied to the live video")

    def update_metadata(self, value: StreamMetadata | str) -> None:
        if not self.broadcast or not self.service:
            return
        metadata = _coerce_metadata(value, self.settings)
        existing = self.broadcast.get("snippet", {})
        snippet = {
            key: existing[key]
            for key in ("scheduledStartTime", "scheduledEndTime")
            if key in existing
        }
        snippet.update(
            {
                "title": metadata.title,
                "description": metadata.description,
                "categoryId": metadata.category_id,
            }
        )
        updated = (
            self.service.liveBroadcasts()
            .update(
                part="snippet",
                body={"id": self.broadcast["id"], "snippet": snippet},
            )
            .execute()
        )
        self.broadcast = updated
        self._update_video_metadata(metadata)
        self.metadata = metadata

    def update_title(self, title: str) -> None:
        self.update_metadata(title)

    def stop(self) -> None:
        if self.broadcast and self.service:
            try:
                self.service.liveBroadcasts().transition(
                    part="status", id=self.broadcast["id"], broadcastStatus="complete"
                ).execute()
                logging.info("YouTube broadcast marked complete")
            except Exception as exc:
                logging.warning(
                    "YouTube broadcast could not be marked complete: %s",
                    _safe_error(exc),
                )
        self.broadcast = None
        self.stream = None


class FacebookDestination:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live_video_id: str | None = None
        self.title = ""
        self.metadata: StreamMetadata | None = None
        self.graph_root = (
            f"https://graph.facebook.com/{settings.facebook_graph_version}/"
        )

    def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        response = requests.post(urljoin(self.graph_root, path), data=data, timeout=30)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok or not isinstance(body, dict) or "error" in body:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = (
                error.get("message", "request failed")
                if isinstance(error, dict)
                else "request failed"
            )
            message = str(message)
            if self.settings.facebook_access_token:
                message = message.replace(
                    self.settings.facebook_access_token, "[token hidden]"
                )
            code = error.get("code", "") if isinstance(error, dict) else ""
            raise RuntimeError(f"Facebook Live API error {code}: {message}")
        return body

    def start(self, value: StreamMetadata | str) -> list[tuple[str, str]]:
        metadata = _coerce_metadata(value, self.settings)
        body = self._post(
            f"{self.settings.facebook_target_id}/live_videos",
            {
                "title": metadata.title,
                "description": metadata.description,
                "status": "LIVE_NOW",
                "access_token": self.settings.facebook_access_token,
            },
        )
        self.live_video_id = str(body["id"]) if body.get("id") else None
        stream_url = body.get("secure_stream_url") or body.get("stream_url")
        if not stream_url or not self.live_video_id:
            raise RuntimeError(
                "Facebook Live API did not return a live video ID and RTMPS ingest URL"
            )
        self.title = metadata.title
        self.metadata = metadata
        logging.info("Facebook live video created with LIVE_NOW status")
        return [("facebook", str(stream_url))]

    def verify_access(self) -> dict[str, str]:
        target = self.settings.facebook_target_id
        try:
            response = requests.get(
                urljoin(self.graph_root, target),
                params={"fields": "id,name"},
                headers={
                    "Authorization": f"Bearer {self.settings.facebook_access_token}"
                },
                timeout=15,
            )
        except requests.RequestException:
            raise RuntimeError(
                "Could not reach the Facebook Graph API. Check the network connection."
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok or not isinstance(body, dict) or "error" in body:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            message = str(
                error.get("message", "request failed")
                if isinstance(error, dict)
                else "request failed"
            )
            if self.settings.facebook_access_token:
                message = message.replace(
                    self.settings.facebook_access_token, "[token hidden]"
                )
            message = message[:240]
            raise RuntimeError(f"Facebook access check failed: {message}")
        object_id = str(body.get("id", ""))
        if not object_id:
            raise RuntimeError(
                "Facebook did not return an account or Page ID for the configured token."
            )
        if target != "me" and object_id != target:
            raise RuntimeError(
                "Facebook returned a different object than the selected Page."
            )
        return {"id": object_id, "name": str(body.get("name") or "Facebook target")}

    def update_metadata(self, value: StreamMetadata | str) -> None:
        if not self.live_video_id:
            return
        metadata = _coerce_metadata(value, self.settings)
        self._post(
            self.live_video_id,
            {
                "title": metadata.title,
                "description": metadata.description,
                "access_token": self.settings.facebook_access_token,
            },
        )
        self.title = metadata.title
        self.metadata = metadata

    def update_title(self, title: str) -> None:
        self.update_metadata(title)

    def stop(self) -> None:
        if self.live_video_id:
            try:
                self._post(
                    self.live_video_id,
                    {
                        "end_live_video": "true",
                        "access_token": self.settings.facebook_access_token,
                    },
                )
                logging.info("Facebook live video ended")
            except Exception as exc:
                logging.warning(
                    "Facebook live video could not be ended: %s", _safe_error(exc)
                )
        self.live_video_id = None


def _safe_error(exc: Exception, secrets_to_hide: tuple[str, ...] = ()) -> str:
    message = redact_urls(str(exc).replace("\n", " "))
    for secret in secrets_to_hide:
        if secret:
            message = message.replace(secret, "[secret hidden]")
    return message[:300]


def open_destinations(
    settings: Settings,
    value: StreamMetadata | str,
    base_metadata: StreamMetadata | None = None,
) -> DestinationSession:
    metadata = _coerce_metadata(value, settings)
    base_metadata = base_metadata or metadata
    session = DestinationSession(metadata=metadata, base_metadata=base_metadata)
    secrets_to_hide = (
        getattr(settings, "youtube_static_key", ""),
        getattr(settings, "facebook_access_token", ""),
        getattr(settings, "facebook_static_key", ""),
    )
    if "youtube" in settings.platforms:
        want_api = settings.youtube_mode == "api" or (
            settings.youtube_mode == "auto" and bool(settings.youtube_client_secrets)
        )
        if want_api:
            if not settings.youtube_client_secrets:
                session.failures["youtube"] = (
                    "YouTube API mode requires a Google OAuth client JSON file"
                )
            else:
                session.youtube = YouTubeDestination(settings)
                try:
                    session.targets.extend(session.youtube.start(metadata))
                except Exception as exc:
                    error = _safe_error(exc, secrets_to_hide)
                    logging.error("YouTube destination unavailable: %s", error)
                    session.failures["youtube"] = error
                    session.youtube.stop()
                    session.youtube = None
        elif settings.youtube_static_key:
            base = "rtmp://a.rtmp.youtube.com/live2"
            session.targets.append(("youtube", f"{base}/{settings.youtube_static_key}"))
            if settings.youtube_backup:
                session.targets.append(
                    (
                        "youtube-backup",
                        f"rtmp://b.rtmp.youtube.com/live2/{settings.youtube_static_key}?backup=1",
                    )
                )
            logging.warning(
                "YouTube static-key mode cannot create broadcasts or synchronize titles"
            )
        else:
            session.failures["youtube"] = "YouTube has no static stream key configured"
    if "facebook" in settings.platforms:
        if settings.facebook_access_token:
            session.facebook = FacebookDestination(settings)
            try:
                session.targets.extend(session.facebook.start(metadata))
            except Exception as exc:
                error = _safe_error(exc, secrets_to_hide)
                logging.error("Facebook destination unavailable: %s", error)
                session.failures["facebook"] = error
                session.facebook.stop()
                session.facebook = None
        elif settings.facebook_static_key:
            session.targets.append(
                (
                    "facebook",
                    f"rtmps://live-api-s.facebook.com:443/rtmp/{settings.facebook_static_key}",
                )
            )
            logging.warning(
                "Facebook static-key mode cannot create live videos or synchronize titles"
            )
        else:
            session.failures["facebook"] = (
                "Facebook has no access token or static stream key configured"
            )
    if not session.targets:
        details = "; ".join(
            f"{name}: {error}" for name, error in session.failures.items()
        )
        message = "No streaming destinations could be created"
        if details:
            message = f"{message}: {details}"
        raise RuntimeError(message)
    return session


def update_destination_metadata(
    session: DestinationSession,
    metadata: StreamMetadata,
    manual: bool = False,
    base_metadata: StreamMetadata | None = None,
    disable_config_overrides: bool = False,
) -> dict[str, str]:
    results: dict[str, str] = {}
    with session.lock:
        for platform, destination in (
            ("youtube", session.youtube),
            ("facebook", session.facebook),
        ):
            if not destination:
                continue
            try:
                destination.update_metadata(metadata)
                results[platform] = "updated"
            except Exception as exc:
                error = _safe_error(exc)
                results[platform] = error
                logging.warning("Could not update %s metadata: %s", platform, error)
        if "updated" in results.values():
            session.metadata = metadata
            session.manual_metadata_override = manual
            if manual:
                session.ignore_config_overrides = False
            if base_metadata is not None:
                session.base_metadata = base_metadata
            if disable_config_overrides:
                session.ignore_config_overrides = True
    return results


def update_destination_thumbnail(
    session: DestinationSession, image_path: Path
) -> dict[str, str]:
    with session.lock:
        if not session.youtube:
            return {"youtube": "YouTube API destination is not active"}
        try:
            session.youtube.update_thumbnail(image_path)
            return {"youtube": "updated"}
        except Exception as exc:
            error = _safe_error(exc)
            logging.warning("Could not update the YouTube thumbnail: %s", error)
            return {"youtube": error}


def update_destination_titles(session: DestinationSession, title: str) -> None:
    with session.lock:
        for platform, destination in (
            ("YouTube", session.youtube),
            ("Facebook", session.facebook),
        ):
            if destination:
                try:
                    destination.update_title(title)
                except Exception as exc:
                    logging.warning(
                        "Could not update the %s title: %s", platform, _safe_error(exc)
                    )


def close_destinations(session: DestinationSession) -> None:
    with session.lock:
        if session.facebook:
            session.facebook.stop()
        if session.youtube:
            session.youtube.stop()
        session.targets.clear()
        session.failures.clear()
