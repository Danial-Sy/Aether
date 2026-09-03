#!/usr/bin/env bash
# Install the Aether launcher into the app menu AND onto the Desktop, so it can be
# started by clicking an icon instead of running a script by hand.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$APPS" "$DESKTOP_DIR"

# Rewrite the absolute paths for wherever this checkout actually lives.
gen() {
  sed -e "s|^Exec=.*|Exec=${APP_DIR}/aether.sh|" \
      -e "s|^Path=.*|Path=${APP_DIR}|" \
      -e "s|^Icon=.*|Icon=${APP_DIR}/static/assets/aether.png|" \
      Aether.desktop > "$1"
}

chmod +x aether.sh

gen "$APPS/aether.desktop"
chmod 644 "$APPS/aether.desktop"

gen "$DESKTOP_DIR/Aether.desktop"
chmod 755 "$DESKTOP_DIR/Aether.desktop"

# GNOME/Nautilus refuse to launch a .desktop file that is not marked trusted.
# Prefer the system gio: a conda/homebrew gio earlier on PATH is often built
# without GVFS metadata support and silently fails to set this.
for GIO in /usr/bin/gio "$(command -v gio 2>/dev/null || true)"; do
  [ -x "$GIO" ] || continue
  if "$GIO" set "$DESKTOP_DIR/Aether.desktop" metadata::trusted true 2>/dev/null; then
    break
  fi
done
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" 2>/dev/null || true
fi

echo "Installed:"
echo "  app menu : $APPS/aether.desktop"
echo "  desktop  : $DESKTOP_DIR/Aether.desktop"
