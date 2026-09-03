#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Prefer distro Python so the desktop window can use system GTK/WebKit
# (Anaconda's python3 cannot import Ubuntu's `gi` module).
if [[ -x /usr/bin/python3 ]]; then
  PY=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "python3 not found. On Ubuntu: sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
"$PY" setup.py "$@"
