#!/usr/bin/env bash
# Live Relay launcher for Linux and macOS. Mirrors start_live_relay.bat.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1

if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    echo "Missing .env.example. Re-extract the complete project folder." >&2
    exit 1
  fi
  cp .env.example .env
  chmod 600 .env
  echo "Created the local settings file. Configure it in the dashboard after it opens."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install Python and try again." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

if ! ".venv/bin/python" -c "import live_relay, yt_dlp, curl_cffi, imageio_ffmpeg, dotenv, requests, googleapiclient, google_auth_oauthlib; raise SystemExit(tuple(map(int, yt_dlp.version.__version__.split('.')[:3])) < (2026, 10, 21))" >/dev/null 2>&1; then
  echo "Installing project dependencies for the first time or repairing the environment..."
  ".venv/bin/python" -m pip install --disable-pip-version-check -e .
fi

# Exit code 42 means the dashboard staged an update: apply it once, then
# start the new version. Any other exit code ends the launcher as before.
for _attempt in 1 2; do
  set +e
  ".venv/bin/python" -m live_relay
  code=$?
  set -e
  if [ "$code" -ne 42 ]; then
    if [ "$code" -ne 0 ]; then
      echo "The dashboard stopped with an error (exit $code). Check live-relay.log in your configuration folder."
    fi
    exit "$code"
  fi
  echo "Installing the staged update, then restarting..."
  ".venv/bin/python" -m live_relay.updater --apply --project-root "$PWD" || echo "The update could not be applied; starting the current version instead."
done
