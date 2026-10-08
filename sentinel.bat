@echo off
REM ---------------------------------------------------------------
REM  Ainara Sentinel - headless scheduler (Bureau + Orakle + plans)
REM  run from source. Double-click, or run "sentinel" from a terminal.
REM  Extra arguments pass through, e.g.:
REM    sentinel --status
REM    sentinel --stop
REM    sentinel --run-plan sentinel_trading_template
REM ---------------------------------------------------------------

cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" (
    echo.
    echo ERROR: Python venv not found at "%~dp0venv"
    echo Create it with:  py -3.12 -m venv venv
    echo.
    pause
    exit /b 1
)

REM Run from source, not the packaged sentinel.exe.
set "AINARA_USE_SOURCE=1"
set "AINARA_SENTINEL_MODE="

echo Starting Ainara Sentinel from source...
echo Press Ctrl+C to stop.
echo.

"%~dp0venv\Scripts\python.exe" "%~dp0scripts\scheduler.py" %*

if errorlevel 1 (
    echo.
    echo Sentinel exited with an error ^(code %errorlevel%^).
    pause
)
