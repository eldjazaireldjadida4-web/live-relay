@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".env" (
  if not exist ".env.example" (
    echo Missing .env.example. Re-extract the complete project folder.
    pause
    exit /b 1
  )
  copy /y ".env.example" ".env" >nul
  echo Created the local settings file. Configure it in the dashboard after it opens.
)

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
      echo Python 3.10 or newer is required. Install Python and try again.
      pause
      exit /b 1
    )
    python -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
)

call :ensure_dependencies
if errorlevel 1 exit /b 1

REM Exit code 42 means the dashboard staged an update: apply it once, then
REM start the new version. Loop twice like the shell launcher, so a second
REM staged update does not silently leave the dashboard down.
for /l %%A in (1,1,2) do (
  ".venv\Scripts\python.exe" -m live_relay
  if not errorlevel 42 (
    if errorlevel 1 echo The dashboard stopped with an error. Check live-relay.log in your Windows user configuration folder.
    goto :end
  )
  echo Installing the staged update, then restarting...
  ".venv\Scripts\python.exe" -m live_relay.updater --apply --project-root "%CD%"
  if errorlevel 1 (
    echo The update could not be applied; starting the current version instead.
  ) else (
    REM A new version may bring new dependencies; re-check before relaunch.
    call :ensure_dependencies
    if errorlevel 1 exit /b 1
  )
)
:end
pause
exit /b 0

:ensure_dependencies
".venv\Scripts\python.exe" -c "import live_relay, yt_dlp, curl_cffi, imageio_ffmpeg, dotenv, requests, googleapiclient, google_auth_oauthlib; raise SystemExit(tuple(map(int, yt_dlp.version.__version__.split('.')[:3])) < (2026, 10, 21))" >nul 2>nul
if errorlevel 1 (
  echo Installing project dependencies for the first time or repairing the environment...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -e .
  if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)
exit /b 0
