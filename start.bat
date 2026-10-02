@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Installing for the first time...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
  .venv\Scripts\python -m playwright install chromium
)
start "" http://127.0.0.1:8765
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
