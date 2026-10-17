@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  python -m venv .venv || py -m venv .venv
  echo [setup] installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo [start] launching dashboard at http://localhost:5000
start "" "http://localhost:5000"
".venv\Scripts\python.exe" app.py
