#!/usr/bin/env bash
# Removes DeskBot. Your settings (API keys) are kept unless you agree to delete them.
set -uo pipefail
APP="$HOME/.local/share/deskbot"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
EC_CONF="$CONF/pipewire/pipewire.conf.d/60-deskbot-echo-cancel.conf"

pkill -f "python -m deskbot" 2>/dev/null || true
rm -f "$HOME/.local/bin/deskbot" "$HOME/.local/share/applications/deskbot.desktop" \
      "$HOME/.local/share/icons/hicolor/256x256/apps/deskbot.png" "$CONF/autostart/deskbot.desktop"
if [[ -f "$EC_CONF" ]]; then
  rm -f "$EC_CONF"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
  echo "Echo cancellation removed (your normal mic is used again)."
fi
read -r -p "Also delete settings and API keys in $CONF/deskbot? [y/N] " a || true
if [[ "${a:-N}" =~ ^[Yy] ]]; then
  rm -rf "$CONF/deskbot" "${XDG_STATE_HOME:-$HOME/.local/state}/deskbot"
fi
rm -rf "$APP"
echo "DeskBot uninstalled."
