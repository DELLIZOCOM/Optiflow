@echo off
REM OptiFlow AI — one-command startup script for Windows
REM Usage: start.bat  (double-click or run in terminal)

cd /d "%~dp0"

set PORT=8000
if not "%1"=="" set PORT=%1

REM ── 1. Check Python ──────────────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Install it from https://python.org ^(tick "Add to PATH"^)
    pause
    exit /b 1
)

REM ── 2. Create virtual environment if missing ─────────────────────────────────
if not exist ".venv\" (
    echo Creating virtual environment...
    python -m venv .venv
)

REM ── 3. Activate ──────────────────────────────────────────────────────────────
call .venv\Scripts\activate.bat

REM ── 4. Install / sync dependencies ───────────────────────────────────────────
if not exist ".venv\.deps_installed" (
    echo Installing dependencies...
    pip install -r requirements.txt --quiet
    echo. > .venv\.deps_installed
    echo Done.
)

REM ── 5. Launch ─────────────────────────────────────────────────────────────────
echo.
echo   OptiFlow AI is starting on http://localhost:%PORT%
echo   Open your browser and navigate there.
echo   Press Ctrl+C to stop.
echo.

uvicorn backend.app:app --host 0.0.0.0 --port %PORT%
pause
