#!/usr/bin/env bash
# DeskBot installer (Fedora first; other distros: install the listed packages yourself).
# Usage: ./install.sh        (asks before optional steps)
#        ./install.sh -y     (accepts every default)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/.local/share/deskbot"
BIN="$HOME/.local/bin"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
EC_CONF="$CONF/pipewire/pipewire.conf.d/60-deskbot-echo-cancel.conf"
YES=0
[[ "${1:-}" == "-y" ]] && YES=1

say() { printf '\n\033[1;31m▶ %s\033[0m\n' "$*"; }
ask() {  # ask "question" Y|N  -> returns 0 for yes
  local q="$1" d="${2:-Y}" a hint="y/N"
  [[ $d == Y ]] && hint="Y/n"
  if [[ $YES == 1 ]]; then [[ $d == Y ]]; return; fi
  read -r -p "$q [$hint] " a || true
  a="${a:-$d}"
  [[ "$a" =~ ^[Yy] ]]
}

if [[ $EUID -eq 0 && -z "${DESKBOT_ALLOW_ROOT:-}" ]]; then
  echo "Run this as your normal user (not root). It will ask for sudo only for system packages."
  exit 1
fi

say "1/6 System packages"
PKGS=(python3 xcb-util-cursor portaudio gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good
      pipewire-gstreamer pipewire-utils pulseaudio-utils espeak-ng google-noto-sans-telugu-fonts)
if command -v dnf >/dev/null 2>&1; then
  missing=()
  for p in "${PKGS[@]}"; do rpm -q "$p" >/dev/null 2>&1 || missing+=("$p"); done
  if ((${#missing[@]})); then
    echo "Installing: ${missing[*]}"
    if ! sudo dnf install -y "${missing[@]}"; then
      echo "Bulk install failed; trying one by one…"
      for p in "${missing[@]}"; do sudo dnf install -y "$p" || echo "  (skipped $p)"; done
    fi
  else
    echo "All present."
  fi
else
  echo "Not Fedora. Please install the equivalents of: ${PKGS[*]}"
  echo "(Debian/Ubuntu: python3-venv libxcb-cursor0 libportaudio2 gstreamer1.0-tools"
  echo " gstreamer1.0-pipewire gstreamer1.0-plugins-good pipewire-bin pulseaudio-utils espeak-ng)"
fi

PY=python3
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10+ is required."; exit 1; }

say "2/6 Copying the app to $APP"
mkdir -p "$APP"
rm -rf "$APP/app"
mkdir -p "$APP/app"
cp -r "$SRC/deskbot" "$APP/app/"
cp "$SRC/requirements.txt" "$SRC/README.md" "$SRC/NOTES.md" "$APP/app/"
cp "$SRC/uninstall.sh" "$APP/uninstall.sh"
chmod +x "$APP/uninstall.sh"
find "$APP/app" -name "__pycache__" -prune -exec rm -rf {} +

say "3/6 Python environment (first time downloads ~150 MB)"
[[ -x "$APP/venv/bin/python" ]] || "$PY" -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install --upgrade pip --quiet
"$APP/venv/bin/python" -m pip install --upgrade -r "$SRC/requirements.txt"

say "4/6 Launcher and app-menu entry"
mkdir -p "$BIN" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/256x256/apps"
cat > "$BIN/deskbot" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="$APP/app\${PYTHONPATH:+:\$PYTHONPATH}"
cd "$APP/app"  # python -m puts the current folder first: never run a stale source checkout by accident
exec "$APP/venv/bin/python" -m deskbot "\$@"
EOF
chmod +x "$BIN/deskbot"
cp "$SRC/deskbot/assets/icon.png" "$HOME/.local/share/icons/hicolor/256x256/apps/deskbot.png"
cat > "$HOME/.local/share/applications/deskbot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DeskBot
Comment=Floating robot companion that sees, listens and talks
Exec=$BIN/deskbot
Icon=deskbot
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$HOME/.local/share/applications" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

say "5/6 Microphone"
# DeskBot uses your normal system mic. Older versions added a PipeWire echo-cancel mic that gave
# silence on some laptops: remove it if it is still there.
if [[ -f "$EC_CONF" ]]; then
  rm -f "$EC_CONF"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
  sleep 2
  echo "Removed DeskBot's old echo-cancel mic."
fi
if command -v pactl >/dev/null 2>&1 && [[ "$(pactl get-default-source 2>/dev/null)" == deskbot_* ]]; then
  real=$(pactl list short sources 2>/dev/null | awk '{print $2}' | grep -v -e '\.monitor$' -e '^deskbot_' | head -1)
  [[ -n "$real" ]] && pactl set-default-source "$real" && echo "Restored your system mic to: $real"
fi
echo "Using your system mic (change it in GNOME Settings → Sound, or in DeskBot → Settings → Talking)."

say "6/6 Start automatically when you log in?"
AUTO="$CONF/autostart/deskbot.desktop"
if ask "Start DeskBot at login?" Y; then
  mkdir -p "$CONF/autostart"
  cat > "$AUTO" <<EOF
[Desktop Entry]
Type=Application
Name=DeskBot
Exec=$BIN/deskbot
Icon=deskbot
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
EOF
fi

cat <<EOF

✔ DeskBot installed.
  Start it:      deskbot        (or "DeskBot" in your app menu)
  Check setup:   deskbot --selftest
  Test screen:   deskbot --selftest --screen
  Uninstall:     $APP/uninstall.sh

First run: Settings opens. Paste your free keys on the "Brain (AI)" tab:
  Groq   → https://console.groq.com/keys
  Gemini → https://aistudio.google.com/apikey
EOF
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH; open a new terminal or use the app menu.";; esac

if ask "Launch DeskBot now?" Y; then
  nohup "$BIN/deskbot" >/dev/null 2>&1 &
  disown || true
fi
