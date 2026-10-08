@echo off
REM ---------------------------------------------------------------
REM  Ainara Polaris - full interactive desktop app, run from source
REM  Double-click this file, or run "polaris" from a terminal.
REM ---------------------------------------------------------------

REM Always run from the repo root: Polaris derives the venv path from
REM the working directory, so launching from elsewhere breaks services.
cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" (
    echo.
    echo ERROR: Python venv not found at "%~dp0venv"
    echo Expected: venv\Scripts\python.exe
    echo Create it with:  python -m venv venv
    echo.
    pause
    exit /b 1
)

if not exist "%~dp0node_modules\electron\dist\electron.exe" (
    echo.
    echo ERROR: Electron not found. Run "npm install" in this folder.
    echo.
    pause
    exit /b 1
)

set "AINARA_USE_SOURCE=1"
REM Explicitly cleared so a leftover value from the parent shell
REM cannot silently start Sentinel instead of Polaris.
set "AINARA_SENTINEL_MODE="

echo Starting Ainara Polaris from source...
echo (first start takes ~60s while Whisper and Kokoro load)
echo Press Ctrl+C or quit from the tray to stop.
echo.

"%~dp0node_modules\electron\dist\electron.exe" .

if errorlevel 1 (
    echo.
    echo Polaris exited with an error ^(code %errorlevel%^).
    pause
)
