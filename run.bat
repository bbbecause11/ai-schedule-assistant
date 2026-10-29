@echo off
REM ===========================================================================
REM  AI Schedule Assistant - one click launcher (Windows)
REM  First run creates a virtual environment and installs dependencies.
REM ===========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [2/3] Installing dependencies, this may take a few minutes...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your network.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment found, skipping install.
)

echo.
echo [3/3] Starting app... browser will open at http://localhost:8501
echo       Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m streamlit run app.py

pause
