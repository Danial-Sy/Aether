#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/aether"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/launch.log"

fail() {
  local msg="$1"
  printf '%s\n' "$msg" | tee -a "$LOG" >&2
  if [[ ! -t 1 ]] && command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Aether" --width=420 --text="$msg" || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send -u critical "Aether" "$msg" || true
  fi
  exit 1
}

if [[ ! -x .venv/bin/python ]]; then
  fail "Aether is not set up yet.\n\nOpen a terminal and run:\n  cd ~/aether && ./setup.sh --skip-models"
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export GGML_CUDA_ENABLE_UNIFIED_MEMORY="${GGML_CUDA_ENABLE_UNIFIED_MEMORY:-1}"
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PYTHON_EXE CONDA_SHLVL 2>/dev/null || true

if [[ -t 1 ]]; then
  exec .venv/bin/python desktop.py
fi

# A process started from a .desktop file can remain attached to the short-lived
# launcher process (gio/desktop shell).  Put the actual window in its own user
# service so closing the launcher cannot also tear down Aether.  The marker avoids
# recursion in the transient service; systems without a user systemd instance keep
# the direct-launch fallback below.
if [[ "${AETHER_DESKTOP_DETACHED:-0}" != "1" ]] \
   && command -v systemd-run >/dev/null 2>&1 \
   && systemctl --user is-system-running >/dev/null 2>&1; then
  UNIT="aether-desktop-$$-$RANDOM"
  if systemd-run --user --quiet --collect --unit="$UNIT" \
      --property="WorkingDirectory=$PWD" \
      --setenv="DISPLAY=${DISPLAY:-}" \
      --setenv="WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}" \
      --setenv="XAUTHORITY=${XAUTHORITY:-}" \
      --setenv="XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}" \
      --setenv="DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-}" \
      /usr/bin/env AETHER_DESKTOP_DETACHED=1 "$PWD/aether.sh"; then
    exit 0
  fi
fi

if ! .venv/bin/python desktop.py >>"$LOG" 2>&1; then
  fail "Aether failed to start.\n\nDetails: $LOG"
fi
