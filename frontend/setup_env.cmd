@echo off
rem ------------------------------------------------------------
rem OpenStacks environment bootstrap
rem  * creates a clean .venv next to this script (Python 3.14 aware;
rem    PySide6 6.9+/6.10 ships 3.14 wheels)
rem  * installs requirements.txt (PySide6 only)
rem  * starts the GUI (pythonw, detached)
rem ------------------------------------------------------------
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [setup_env] Python 3.9+ was not found on PATH. Install it first.
  exit /b 1
)

if not exist .venv (
  echo [setup_env] creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [setup_env] failed to create .venv
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

echo [setup_env] installing requirements (PySide6) ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [setup_env] pip install failed
  exit /b 1
)

echo [setup_env] launching OpenStacks ...
start "" .venv\Scripts\pythonw.exe run_player.py

endlocal
