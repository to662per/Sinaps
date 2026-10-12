@echo off
rem ============================================================
rem  Sinaps one-time setup - run this ONCE after cloning.
rem
rem  IMPORTANT: this file is intentionally ASCII-only.
rem  cmd.exe reads .bat files in the OEM codepage (866/1251), not
rem  UTF-8. Cyrillic in a batch file corrupts parsing: variables
rem  like %ERRORLEVEL% get swallowed, ">" gets split, and the
rem  script dies in a loop of "not recognized" errors.
rem  All Russian text lives inside Python.
rem
rem  What it does:
rem    1. finds a suitable Python (3.12 only - see note below)
rem    2. creates .venv if missing
rem    3. installs requirements.txt  (~3.5 GB: torch, CUDA, Qt)
rem    4. downloads speech models     (~2 GB)
rem    5. checks the microphone
rem
rem  Total first run: ~5.5 GB on disk, 15-40 minutes.
rem  Everything else fits in git; this is the only step needing
rem  the network.
rem
rem  Safe to run twice: nothing already present is downloaded again.
rem
rem  Python 3.12 ONLY. ctranslate2 (used by faster-whisper) has no
rem  wheels for 3.13/3.14, so a newer Python fails at install.
rem ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_DIR=.venv"
set "REQ=requirements.txt"
set "RC=0"
set "RC2=0"

echo.
echo ============================================================
echo   SINAPS - FIRST-TIME SETUP
echo ============================================================
echo.
echo This runs once. It downloads about 5.5 GB and may take a while.
echo.
echo Do not close this window while it works.
echo.

rem --- 1. which Python? ------------------------------------------------
rem The venv interpreter is the only authoritative answer. Looking at
rem PATH instead is a trap: on many machines PATH holds 3.14 while the
rem venv was built from 3.12, and the script would then "fix" a
rem working environment to a broken one.

if exist "%VENV_PY%" goto :have_venv

echo [1/4] Looking for Python 3.12 ...

rem py launcher, version specific
for /f "delims=" %%v in ('py -3.12 -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2^>nul') do (
    if "%%v"=="312" set "PY=py -3.12"
)

if defined PY goto :have_python

rem bare python on PATH, only if it really is 3.12
for /f "delims=" %%v in ('python -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2^>nul') do (
    if "%%v"=="312" set "PY=python"
)

if defined PY goto :have_python

rem usual install locations, checked in order
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "C:\Python312\python.exe"
) do (
    if exist %%p (
        for /f "delims=" %%v in ('%%~p -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2^>nul') do (
            if "%%v"=="312" set "PY=%%~p"
        )
    )
)

if defined PY goto :have_python

rem nothing suitable. Tell them what was found, if anything.
set "WRONGVER="
for /f "delims=" %%v in ('python -c "import sys;print(sys.version)" 2^>nul') do (
    if not defined WRONGVER set "WRONGVER=%%v"
)
goto :wrongpython

:have_python
echo        found: %PY%

echo [1/4] Creating virtual environment .venv ...
%PY% -m venv "%VENV_DIR%"
if errorlevel 1 goto :venvfail
if not exist "%VENV_PY%" goto :venvfail

:have_venv
rem verify the interpreter we are about to trust
for /f "delims=" %%v in ('"%VENV_PY%" -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2^>nul') do (
    if not "%%v"=="312" goto :wrongpython
)
echo [1/4] Python 3.12 ready.

rem --- 2. dependencies --------------------------------------------------
if not exist "%REQ%" goto :noreq

rem requirements.txt is compared byte for byte against a stamp left by
rem the previous run. Reinstalling 3.5 GB takes ten minutes; checking
rem takes two seconds.
set "STAMP=.venv\.sinaps-installed"
set "NEED_PIP=1"
if exist "%STAMP%" fc /b "%STAMP%" "%REQ%" >nul 2>&1 && set "NEED_PIP=0"

if "%NEED_PIP%"=="0" goto :pip_done

echo.
echo [2/4] Installing dependencies, about 3.5 GB ...
echo        torch, CUDA libraries, PySide6, sounddevice.
echo        This is the longest step. Please wait.
echo.
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :pipfail
"%VENV_PY%" -m pip install -r "%REQ%"
if errorlevel 1 goto :pipfail
copy /y "%REQ%" "%STAMP%" >nul
:pip_done
echo [2/4] Dependencies ready.

rem --- 3. models and sound --------------------------------------------
echo.
echo [3/4] Checking models and sound. About 2 GB may be downloaded.
echo.
rem `setup --yes` ends with a verified summary of what is on disk, so a
rem separate check run right after it would print the same table twice.
rem It would also be the weaker check: it looks at files rather than
rem actually loading the models.
"%VENV_PY%" -m sinaps setup --yes
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :setupwarn

echo.
echo ============================================================
echo   READY
echo ============================================================
echo.
echo Start with the tray (no console window):
echo     run_sinaps_silent.bat
echo.
echo Or with a console, for troubleshooting:
echo     run_sinaps.bat
echo.
echo Start together with Windows:
echo     run_sinaps_silent.bat, then tray menu - Settings - Start with Windows
echo.
echo Logs:     data\logs\sinaps.log
echo Commands: see the docs folder, file KOMANDY.md
echo.
goto :end

:setupwarn
echo.
echo [4/4] Setup finished with problems. See the messages above.
echo       Fix the cause and run this file again - anything already
echo       downloaded will NOT be downloaded a second time.
echo.

:end
echo Press any key to close.
pause >nul
endlocal & exit /b %RC2%

rem --- failure branches -------------------------------------------------

:venvfail
echo.
echo [ERROR] Could not create the virtual environment .venv
echo         Try manually to see the real error:
echo             py -3.12 -m venv .venv
goto :fail

:pipfail
echo.
echo [ERROR] Installing dependencies failed.
echo.
echo         Most common causes:
echo           - no internet connection
echo           - not enough free disk space (need about 4 GB)
echo           - antivirus blocked pip
echo.
echo         Try manually to see the full error:
echo             .venv\Scripts\python -m pip install -r requirements.txt
goto :fail

:wrongpython
echo.
echo [ERROR] Sinaps needs Python 3.12 exactly.
echo.
if defined WRONGVER echo         Found on PATH: %WRONGVER%
echo         Versions 3.13 and newer will NOT work:
echo         faster-whisper depends on ctranslate2, which has no
echo         build for Python 3.13 / 3.14 yet.
echo.
echo         Install Python 3.12 from https://www.python.org/downloads/
echo         Tick "Add Python to PATH" during setup.
echo.
if exist "%VENV_PY%" echo         NOTE: .venv already exists but was not
echo         built with 3.12. Delete the .venv folder and run again.
goto :fail

:noreq
echo.
echo [ERROR] requirements.txt not found next to this file.
echo         Keep setup_sinaps.bat in the project root.
goto :fail

:fail
echo Press any key to close.
pause >nul
endlocal & exit /b 1
