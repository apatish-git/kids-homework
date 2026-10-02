#!/usr/bin/env bash
# Start "בית ספר חכם" on Linux. First run creates the virtualenv and downloads Chromium.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Installing for the first time..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  echo
  echo "If the browser fails to start, install its system libraries once with:"
  echo "  sudo .venv/bin/python -m playwright install-deps chromium"
  echo "and Hebrew fonts for the manual-login view:  sudo apt install fonts-noto-core"
  echo
fi

# Listens on this machine only by default. To use it from another computer, prefer an SSH tunnel:
#   ssh -L 8765:127.0.0.1:8765 <user>@<server>   then open http://127.0.0.1:8765 locally
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
