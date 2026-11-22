"""Automatic updates for live-relay.

The app runs from source (there is no installer yet), so an update is a
GitHub release zipball: the dashboard checks ``/releases/latest`` for the
configured repository, downloads the zip, verifies it, and stages it. The
launcher scripts apply the staged zip on the next start and relaunch the
app, so no file is ever overwritten while the running process holds it.

Safety rules, all deliberate:

- Network failures never raise out of the check: they return ``None`` and
  the dashboard simply reports that no check could be made.
- Nothing is downloaded or applied without an explicit user click in the
  dashboard. The automatic check only *reports* that a release exists.
- A staged zip is verified before it is trusted: ``PK`` magic, announced
  size when GitHub gives one, and :func:`zipfile.ZipFile.testzip` at
  download; its size and sha256 are recorded at staging and re-checked at
  apply time, and the extracted tree must prove it is live-relay at the
  staged version before any file is touched. When the release carries a
  ``MANIFEST.json`` (file list + sha256 hashes, generated at release
  time), every extracted file is verified against it — a missing,
  modified or unlisted file refuses the update; a release without a
  manifest keeps the previous checks.
- Applying an update never touches local secrets or local state: ``.env``,
  ``.venv``, OAuth/client-secret files, TLS keys, logs and the state
  directory are preserved, and zip members escaping the project root
  (absolute paths or ``..``) are rejected.
- ``UPDATE_REPO`` is ``eldjazaireldjadida4-web/live-relay`` by default;
  clearing it in the dashboard disables the check entirely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# The launcher scripts treat this exit code as "an update is staged:
# apply it, then start the new version".
UPDATE_RESTART_EXIT_CODE = 42

CHECK_INTERVAL_SECONDS = 86400  # check at most once a day
CHECK_TIMEOUT_SECONDS = 10
DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_MAX_BYTES = 200 * 1024 * 1024
MAX_RELEASE_NOTES_CHARS = 4000

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$"
)

# Paths that an update must never overwrite, relative to the project root.
_PRESERVED_NAMES = {".env", ".git", ".venv", ".test-venv", "__pycache__"}
_PRESERVED_SUFFIXES = (".key", ".crt", ".pem", ".p12", ".log")
_PRESERVED_PREFIXES = ("client_secret", "oauth-client")

# The release manifest: a file list + sha256 hashes for every
# releasable file, generated from the project tree at release time
# (``python -m live_relay.updater --manifest``) and committed at the
# project root, so it travels inside the release zipball itself.
MANIFEST_FILENAME = "MANIFEST.json"
_MANIFEST_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".test-venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
_MANIFEST_EXCLUDED_NAMES = {".env", MANIFEST_FILENAME, ".DS_Store"}
_MANIFEST_EXCLUDED_SUFFIXES = (".key", ".crt", ".pem", ".p12", ".log")
_MANIFEST_EXCLUDED_PREFIXES = ("client_secret", "oauth-client")


def parse_version(value: str) -> tuple[int, ...] | None:
    """Parse ``4.8.2``, ``v4.8.2``, a four-part hotfix ``4.8.3.1`` or a
    pre-release tag such as ``v4.8.3-rc1`` (the numeric core is used, so a
    pre-release compares equal to its release). Anything else is not a
    release version."""
    if not isinstance(value, str):
        return None
    match = _VERSION_PATTERN.match(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def is_newer(latest: str, current: str) -> bool:
    """True only when both versions parse and latest is strictly newer."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    return latest_parsed > current_parsed


def fetch_latest_release(repo: str, timeout: int = CHECK_TIMEOUT_SECONDS) -> dict | None:
    """Return the latest GitHub release for ``owner/repo``, or ``None``.

    Never raises: network errors, HTTP errors, bad JSON and releases
    without a parseable version tag all return ``None`` and are logged
    with only the exception type, never the response body.
    """
    if not repo or not REPO_PATTERN.match(repo.strip()):
        return None
    url = f"https://api.github.com/repos/{repo.strip()}/releases/latest"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "live-relay-updater",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, OSError, TimeoutError) as exc:
        logging.warning("Update check failed: %s", type(exc).__name__)
        return None
    if not isinstance(payload, dict):
        return None
    version = parse_version(str(payload.get("tag_name") or ""))
    if version is None:
        logging.warning(
            "Latest release has no parseable version tag: %r",
            payload.get("tag_name"),
        )
        return None
    assets = []
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        assets.append(
            {
                "name": str(asset.get("name") or ""),
                "browser_download_url": str(asset.get("browser_download_url") or ""),
                "size": asset.get("size"),
            }
        )
    return {
        "version": ".".join(str(part) for part in version),
        "tag": str(payload.get("tag_name")),
        "name": str(payload.get("name") or ""),
        "body": str(payload.get("body") or "")[:MAX_RELEASE_NOTES_CHARS],
        "html_url": str(payload.get("html_url") or ""),
        "zipball_url": str(payload.get("zipball_url") or ""),
        "assets": assets,
    }


def download_release_zip(
    url: str,
    destination: Path,
    expected_size: int | None = None,
    timeout: int = DOWNLOAD_TIMEOUT_SECONDS,
) -> Path:
    """Download ``url`` to ``destination`` and verify it is a usable zip.

    Raises :class:`ValueError` on a size mismatch, an oversized download, a
    non-zip payload or a corrupt zip, and removes the partial file on any
    failure.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "live-relay-updater"})
    try:
        with urlopen(request, timeout=timeout) as response:
            announced = response.headers.get("Content-Length")
            if expected_size is None and announced is not None:
                try:
                    expected_size = int(announced)
                except ValueError:
                    expected_size = None
            total = 0
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > DOWNLOAD_MAX_BYTES:
                        raise ValueError(
                            "Downloaded update exceeds the "
                            f"{DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB limit"
                        )
                    handle.write(chunk)
    except (URLError, HTTPError, OSError, TimeoutError) as exc:
        _remove_quietly(destination)
        raise RuntimeError(f"Could not download the update: {type(exc).__name__}") from exc
    except ValueError:
        _remove_quietly(destination)
        raise
    if expected_size is not None and total != expected_size:
        _remove_quietly(destination)
        raise ValueError(
            f"Downloaded update size {total} does not match the announced "
            f"size {expected_size}; the partial file was removed"
        )
    try:
        with open(destination, "rb") as handle:
            magic = handle.read(2)
    except OSError:
        _remove_quietly(destination)
        raise ValueError("Downloaded update could not be read") from None
    if len(magic) < 2 or magic != b"PK":
        _remove_quietly(destination)
        raise ValueError("Downloaded update is not a zip file")
    try:
        with zipfile.ZipFile(destination) as archive:
            corrupt_member = archive.testzip()
    except zipfile.BadZipFile as exc:
        _remove_quietly(destination)
        raise ValueError("Downloaded update is not a valid zip file") from exc
    if corrupt_member is not None:
        _remove_quietly(destination)
        raise ValueError("Downloaded update zip is corrupt")
    return destination


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _update_state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "update.json"


def read_update_state(state_dir: Path) -> dict:
    try:
        data = json.loads(_update_state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state_file(state_dir: Path, state: dict) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _update_state_path(state_dir)
    fd, temp_name = tempfile.mkstemp(prefix=".update-", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        _remove_quietly(Path(temp_name))
        raise
    try:
        dir_fd = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def write_update_state(state_dir: Path, **fields) -> dict:
    state = read_update_state(state_dir)
    state.update(fields)
    state["updated_at"] = time.time()
    _write_state_file(state_dir, state)
    return state


def due_for_check(state_dir: Path, now: float | None = None) -> bool:
    """True when no check ran in the last :data:`CHECK_INTERVAL_SECONDS`."""
    last = read_update_state(state_dir).get("last_check")
    if not isinstance(last, (int, float)):
        return True
    return (now if now is not None else time.time()) - last >= CHECK_INTERVAL_SECONDS


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_excluded(relative: Path) -> bool:
    """True for files that must never appear in a release manifest:
    local secrets, caches, VCS metadata and build residue."""
    if any(part in _MANIFEST_EXCLUDED_DIRS for part in relative.parts):
        return True
    if any(part.endswith(".egg-info") for part in relative.parts):
        return True
    name = relative.name
    if name in _MANIFEST_EXCLUDED_NAMES:
        return True
    lowered = name.lower()
    if lowered.endswith(_MANIFEST_EXCLUDED_SUFFIXES):
        return True
    if any(lowered.startswith(prefix) for prefix in _MANIFEST_EXCLUDED_PREFIXES):
        return True
    return False


def build_manifest(project_root: Path, version: str) -> dict:
    """File list + sha256 hashes for every releasable file in the tree.

    The manifest itself, local secrets, caches and build residue are
    excluded. The output is deterministic (sorted paths), so a
    regenerated manifest compares equal to the committed one.
    """
    project_root = Path(project_root).resolve()
    files = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project_root)
        if _manifest_excluded(relative):
            continue
        files.append({"path": relative.as_posix(), "sha256": _file_sha256(path)})
    return {"version": version, "files": files}


def write_manifest(project_root: Path, version: str) -> Path:
    """Regenerate MANIFEST.json in the project root for ``version``.

    Run this before tagging a release; the updater verifies the
    downloaded update against the manifest it carries.
    """
    manifest = build_manifest(project_root, version)
    path = Path(project_root) / MANIFEST_FILENAME
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def verify_manifest(content_root: Path, version: str) -> bool:
    """Verify an extracted update tree against its MANIFEST.json.

    Returns False when the tree carries no manifest (older releases):
    the caller then falls back to the existing zip-integrity and
    version checks. Raises :class:`ValueError` when a manifest exists
    but the tree does not match it: a version mismatch, a missing or
    modified file, or an unlisted file.
    """
    manifest_path = Path(content_root) / MANIFEST_FILENAME
    if not manifest_path.is_file():
        logging.info("Update has no manifest; using zip-integrity checks only")
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Update manifest could not be read") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        raise ValueError("Update manifest is for a different version")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("Update manifest has no file list")
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Update manifest has a malformed entry")
        path = str(entry.get("path") or "")
        digest = str(entry.get("sha256") or "")
        parsed = Path(path)
        if (
            not path
            or not digest
            or parsed.is_absolute()
            or ".." in parsed.parts
        ):
            raise ValueError("Update manifest has an unsafe path")
        expected[path] = digest
    for path, digest in expected.items():
        candidate = content_root / path
        if not candidate.is_file():
            raise ValueError(f"Update is missing file {path}")
        if _file_sha256(candidate) != digest:
            raise ValueError(
                f"Update file {path} does not match its manifest hash"
            )
    for found in sorted(content_root.rglob("*")):
        if not found.is_file():
            continue
        relative = found.relative_to(content_root)
        if relative.name == MANIFEST_FILENAME or _is_preserved(relative):
            continue
        if relative.as_posix() not in expected:
            raise ValueError(
                f"Update contains unlisted file {relative.as_posix()}"
            )
    return True


def stage_update(state_dir: Path, release: dict, zip_path: Path) -> dict:
    """Record a verified downloaded release as staged for the next start.

    The zip's size and sha256 are stored with the marker so
    :func:`apply_pending_update` can prove the staged file is the one that
    was downloaded, not a damaged or swapped copy. When the file cannot be
    read (a mocked download in tests), the marker is stored without them
    and apply falls back to zip-integrity checks.
    """
    zip_path = Path(zip_path)
    try:
        size = zip_path.stat().st_size
        sha256 = _file_sha256(zip_path)
    except OSError:
        size = None
        sha256 = None
    return write_update_state(
        state_dir,
        staged={
            "version": release["version"],
            "tag": release.get("tag", ""),
            "html_url": release.get("html_url", ""),
            "zip": str(zip_path),
            "size": size,
            "sha256": sha256,
        },
    )


def _is_preserved(relative: Path) -> bool:
    parts = relative.parts
    if any(part in _PRESERVED_NAMES for part in parts):
        return True
    name = relative.name.lower()
    if name.endswith(_PRESERVED_SUFFIXES):
        return True
    if any(name.startswith(prefix) for prefix in _PRESERVED_PREFIXES):
        return True
    return False


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for member in archive.infolist():
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Update zip contains an unsafe path: {member.filename}")
        members.append(member)
    return members


def _top_level_prefix(members: list[zipfile.ZipInfo]) -> str:
    """GitHub zipballs put everything under one ``repo-tag/`` directory.

    Return that prefix (with trailing slash) when every file member shares
    it; a plain project zip (no single top-level directory) returns "".
    """
    names = [member.filename for member in members if not member.is_dir()]
    if not names:
        return ""
    first_parts = [Path(name).parts[0] for name in names]
    if len(set(first_parts)) == 1 and len(Path(names[0]).parts) > 1:
        return first_parts[0] + "/"
    return ""


def _copy_file_atomic(source: Path, target: Path) -> None:
    """Copy ``source`` over ``target`` atomically, preserving its mode.

    The bytes land in a temporary file in the target directory and are
    moved into place with :func:`os.replace`, so a crash mid-copy can never
    leave a half-written file behind, and executable bits (launcher
    scripts) survive the update.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".update-", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with open(source, "rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        shutil.copymode(source, temp_path)
        os.replace(temp_path, target)
    except BaseException:
        _remove_quietly(temp_path)
        raise
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _validate_update_content(content_root: Path, version: str) -> None:
    """Prove the extracted tree really is live-relay at ``version``.

    Raises :class:`ValueError` for an empty zip, a zip of unrelated files,
    a mixed layout, or a version mismatch. Nothing is copied before this
    passes, so a bad zip can never report a successful update.
    """
    package_init = content_root / "src" / "live_relay" / "__init__.py"
    if not package_init.is_file():
        raise ValueError(
            "Update zip does not contain the live-relay project "
            "(missing src/live_relay/__init__.py)"
        )
    text = package_init.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    if not match:
        raise ValueError("Update zip has no readable __version__")
    extracted_version = parse_version(match.group(1))
    staged_version = parse_version(version)
    if extracted_version is None or staged_version is None:
        raise ValueError("Update zip version could not be parsed")
    if extracted_version != staged_version:
        raise ValueError(
            f"Update zip is version {match.group(1)}, not the staged {version}"
        )


def apply_pending_update(project_root: Path, state_dir: Path) -> str | None:
    """Apply a staged update over ``project_root``; return its version.

    Returns ``None`` when nothing is staged. Preserved files (``.env``,
    ``.venv``, secrets, logs, the state directory) are never overwritten.
    The staged marker and zip are removed only after a successful apply.
    The staged zip is re-verified (size and sha256 when they were recorded,
    otherwise zip integrity) and the extracted tree must prove it is
    live-relay at the staged version before any file is touched.
    """
    project_root = Path(project_root).resolve()
    state_dir = Path(state_dir)
    state = read_update_state(state_dir)
    staged = state.get("staged")
    if not isinstance(staged, dict):
        return None
    zip_path = Path(staged.get("zip") or "")
    version = str(staged.get("version") or "")
    if not zip_path.is_file():
        raise RuntimeError("Staged update file is missing; download it again")
    if not version or parse_version(version) is None:
        raise ValueError("Staged update has no valid version")
    expected_size = staged.get("size")
    expected_sha256 = staged.get("sha256")
    if isinstance(expected_size, int) and zip_path.stat().st_size != expected_size:
        raise ValueError("Staged update file changed size; download it again")
    if isinstance(expected_sha256, str) and _file_sha256(zip_path) != expected_sha256:
        raise ValueError("Staged update file is damaged; download it again")
    with zipfile.ZipFile(zip_path) as archive:
        members = _safe_members(archive)
        prefix = _top_level_prefix(members)
        if archive.testzip() is not None:
            raise ValueError("Staged update zip is corrupt")
        # The zip records Unix modes in external_attr, but extraction does
        # not restore them; remember the executable bits so launcher
        # scripts keep working after the update.
        zip_modes = {
            info.filename: (info.external_attr >> 16) & 0o777
            for info in archive.infolist()
            if not info.is_dir()
        }
        with tempfile.TemporaryDirectory(prefix="live-relay-update-") as directory:
            extracted = Path(directory)
            archive.extractall(extracted)
            # A GitHub zipball has everything under one ``repo-tag/``
            # directory, which is stripped. But a plain project zip whose
            # files happen to share one first directory (for example only
            # ``src/...`` files) would be mis-detected, so fall back to
            # the unstripped tree when the stripped one is not the project.
            candidates = []
            if prefix:
                candidates.append(extracted / prefix.rstrip("/"))
            candidates.append(extracted)
            content_root = None
            first_error: ValueError | None = None
            for candidate in candidates:
                try:
                    _validate_update_content(candidate, version)
                except ValueError as exc:
                    # Keep the first failure: for a real zipball it names
                    # the actual problem (e.g. a version mismatch), while
                    # the fallback candidate is expected to fail.
                    if first_error is None:
                        first_error = exc
                    continue
                content_root = candidate
                break
            if content_root is None:
                raise first_error  # type: ignore[misc]
            # A manifest travels inside the release itself: prove the
            # extracted tree matches it file by file, instead of
            # trusting the zip contents alone. A tree without one (an
            # older release) keeps the previous checks.
            verify_manifest(content_root, version)
            for source in sorted(content_root.rglob("*")):
                if not source.is_file():
                    continue
                relative = source.relative_to(content_root)
                if _is_preserved(relative):
                    continue
                target = project_root / relative
                # Never write into the state directory itself.
                try:
                    target.resolve().relative_to(state_dir.resolve())
                    continue
                except ValueError:
                    pass
                _copy_file_atomic(source, target)
                member = f"{prefix}{relative.as_posix()}" if prefix else relative.as_posix()
                recorded = zip_modes.get(member, 0)
                if recorded & 0o111:
                    try:
                        current = stat.S_IMODE(target.stat().st_mode)
                        os.chmod(target, current | (recorded & 0o111))
                    except OSError:
                        pass
    state.pop("staged", None)
    state["last_applied_version"] = version
    state["last_applied_at"] = time.time()
    state.pop("latest", None)
    state["updated_at"] = time.time()
    _write_state_file(state_dir, state)
    _remove_quietly(zip_path)
    logging.info("Applied live-relay update to %s", version)
    return version


def _resolve_state_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.getenv("LIVE_RELAY_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    from .config import default_state_dir

    return default_state_dir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="live-relay updater")
    parser.add_argument("--apply", action="store_true", help="apply a staged update")
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="regenerate MANIFEST.json for the current version and exit",
    )
    parser.add_argument("--project-root", default=".", help="project root to update")
    parser.add_argument("--state-dir", default=None, help="state directory")
    args = parser.parse_args(argv)
    if args.manifest:
        from . import __version__

        path = write_manifest(Path(args.project_root), __version__)
        print(f"Wrote {path} for version {__version__}.")
        return 0
    if not args.apply:
        parser.print_help()
        return 2
    state_dir = _resolve_state_dir(args.state_dir)
    try:
        version = apply_pending_update(Path(args.project_root), state_dir)
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
        print(f"Could not apply the update: {exc}", file=sys.stderr)
        return 1
    if version is None:
        print("No update is staged.")
        return 2
    print(f"Updated live-relay to {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
