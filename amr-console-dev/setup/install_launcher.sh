#!/usr/bin/env bash
# Add "AMR Console" and "AMR Speech Lab" to the app menu and app search for this user (no sudo).
#
#   bash setup/install_launcher.sh            add or update both apps and their icons
#   bash setup/install_launcher.sh --remove   remove them again
#
# For each app it writes ~/.local/bin/<name> (a link to the start script in this checkout),
# ~/.local/share/applications/<name>.desktop, and its icon in ~/.local/share/icons/hicolor
# (scalable SVG and a 256 px PNG; the originals are in setup/icons/). Run it again after moving
# this folder. An older lab launcher (amr-console.desktop) is left alone.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor"
for pair in "amr_console:run_console.sh:AMR Console" "amr_speech_lab:run_speech_lab.sh:AMR Speech Lab"; do
  IFS=: read -r name script title <<< "$pair"
  bin="$HOME/.local/bin/$name"
  entry="$apps/$name.desktop"
  svg="$icons/scalable/apps/$name.svg"
  png="$icons/256x256/apps/$name.png"
  if [ "${1:-}" = "--remove" ]; then
    rm -f -- "$bin" "$entry" "$svg" "$png"
    echo "Removed the $title launcher and icon."
    continue
  fi
  mkdir -p "$(dirname "$bin")" "$apps" "$(dirname "$svg")" "$(dirname "$png")"
  chmod +x "$repo/setup/$script"
  ln -sfn "$repo/setup/$script" "$bin"
  install -m 644 "$repo/setup/icons/$name.svg" "$svg"
  install -m 644 "$repo/setup/icons/${name}_256.png" "$png"
  sed -e "s#@LAUNCHER@#$bin#" -e "s#@ICON@#$svg#" "$repo/setup/$name.desktop" > "$entry"
  echo "Installed: $title -> $repo/setup/$script (icon: $svg)"
done
if [ "${1:-}" != "--remove" ]; then
  update-desktop-database -q "$apps" 2>/dev/null || true
  echo "Press the Super key and type \"AMR\" to find them. If an old icon still shows, log out and in once."
fi
