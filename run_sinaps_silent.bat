@echo off
rem ============================================================
rem  Sinaps silent launcher (no console window) - for autostart
rem  ASCII-only on purpose, see run_sinaps.bat for the reason.
rem
rem  IMPORTANT: do not launch this AND run_sinaps.bat at the same time.
rem  Two copies share one microphone stream, and voice breaks into
rem  fragments. Sinaps refuses to start a second copy and says so.
rem
rem  This launcher starts the TRAY version: no console window, control
rem  from the icon in the notification area (status, history, settings,
rem  pause, exit).
rem
rem  Install autostart: run  .\run_sinaps.bat  then use
rem  "Settings -> Recognise -> Start with Windows" (writes HKCU\...\Run),
rem  or create a shortcut to this file in Win+R -> shell:startup
rem
rem  To stop the assistant: right-click the tray icon -> Exit.
rem ============================================================

cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\pythonw.exe"
if not exist "%VENV_PY%" set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" exit /b 1

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

start "" "%VENV_PY%" -m sinaps --tray %*
