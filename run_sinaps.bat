@echo off
rem ============================================================
rem  Sinaps launcher for Windows
rem
rem  IMPORTANT: this file is intentionally ASCII-only.
rem  cmd.exe reads .bat files in the OEM codepage (866/1251),
rem  not UTF-8. Cyrillic text in a batch file corrupts parsing:
rem  variables like %ERRORLEVEL% get swallowed, ">" gets split,
rem  and the script breaks in a loop of "not recognized" errors.
rem  All Russian text lives inside Python, which handles UTF-8.
rem
rem  Usage:
rem    run_sinaps.bat                 normal start
rem    run_sinaps.bat --text-only     no microphone, text input only
rem    run_sinaps.bat selftest        check the environment
rem    run_sinaps.bat check-mic       test the microphone
rem    run_sinaps.bat hotkeys         test keyboard and mouse buttons
rem    run_sinaps.bat devices         list audio devices
rem    run_sinaps.bat benchmark       measure latency
rem ============================================================

setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment .venv not found.
    echo.
    echo Create it with:
    echo     py -3.12 -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    echo.
    exit /b 1
)

rem UTF-8 console so that Python output renders correctly.
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

"%VENV_PY%" -m sinaps %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [EXITED WITH CODE %RC%]
    echo Log:       data\logs\sinaps.log
    echo Diagnose:  run_sinaps.bat selftest
)

endlocal & exit /b %RC%
