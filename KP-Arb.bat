@echo off
rem KP-Arb.bat - double-click to run the dashboard on Windows. The first run sets everything up:
rem   finds Python 3.10+  ->  creates .venv and installs requirements.txt  ->  asks for your API keys (.env)  ->  checks them
rem Every run after that just starts the app (packages are reinstalled only when requirements.txt changes), with a
rem 3-second chance to press K and change your keys. Keep this window open while trading; Ctrl+C stops the app.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title K/P Cross-Market Arb
set "VPY=.venv\Scripts\python.exe"
echo K/P Cross-Market Arb

rem -- 1. Python + virtual environment (first run, or if .venv was deleted) -------------
if exist "%VPY%" goto :packages
echo.
echo ==^> First-time setup: finding Python 3.10 or newer
set "PY="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3.10" "py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY (
  echo Python 3.10 or newer was not found.
  echo Opening python.org - install Python, tick "Add python.exe to PATH", then double-click KP-Arb.bat again.
  start "" "https://www.python.org/downloads/windows/"
  goto :fail
)
for /f "delims=" %%V in ('%PY% --version 2^>^&1') do echo Found %%V  ^(%PY%^)
echo.
echo ==^> Creating the virtual environment (.venv)
%PY% -m venv .venv
if errorlevel 1 ( echo Could not create .venv. & goto :fail )

rem -- 2. packages: install when requirements.txt changed since the last install --------
:packages
set "REQ="
for /f "delims=" %%H in ('certutil -hashfile requirements.txt SHA1 ^| findstr /v ":"') do if not defined REQ set "REQ=%%H"
set "OLD="
if exist ".venv\.requirements.sha" set /p OLD=<".venv\.requirements.sha"
if /i "!REQ!"=="!OLD!" goto :keycheck
echo.
echo ==^> Installing packages (first run takes a few minutes)
"%VPY%" -m pip install --quiet --upgrade pip
if errorlevel 1 ( echo Could not upgrade pip - check your internet connection. & goto :fail )
"%VPY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 ( echo Package install failed - see the messages above. & goto :fail )
(echo !REQ!)>".venv\.requirements.sha"
echo Packages installed.

rem -- 3. API keys: ask on first run (or when K is pressed), check after any change -----
:keycheck
set "ARG="
"%VPY%" -c "import sys; sys.path.insert(0,'tools'); import setup_keys as s; sys.exit(0 if all(s.read_env().get(k) for k in s.KEYS) else 1)"
if errorlevel 1 goto :keys
choice /c KS /t 3 /d S /n /m "Starting... press K within 3 seconds to change your API keys: "
if errorlevel 2 goto :start
set "ARG=--change"
:keys
echo.
echo ==^> API keys
:keysloop
"%VPY%" tools\setup_keys.py %ARG%
if not errorlevel 1 goto :start
echo.
choice /c YN /n /m "Re-enter the keys? [Y/N] "
if errorlevel 2 ( echo Not starting until the keys check out. Double-click KP-Arb.bat to try again. & goto :fail )
set "ARG=--change"
goto :keysloop

rem -- 4. start ----------------------------------------------------------------------
:start
echo.
echo ==^> Starting the dashboard - it opens in your browser. Keep this window open; Ctrl+C stops the app.
"%VPY%" -m streamlit run trade\web_app.py
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
