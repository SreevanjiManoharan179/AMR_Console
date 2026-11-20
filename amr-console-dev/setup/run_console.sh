#!/usr/bin/env bash
# Start AMR Console and open its page in the browser.
#
#   bash setup/run_console.sh            (or the "AMR Console" app-menu entry, see install_launcher.sh)
#
# The page link carries a one-time access token. This script hands it straight to the browser
# and never prints or saves it. Keep the window open while you use the console; close it, or
# press Ctrl+C, to stop the server. Set AMR_CONSOLE_NO_BROWSER=1 to start without a browser.
repo="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$repo" || exit 1
if [ ! -f install/setup.bash ]; then
  echo "AMR Console is not built yet. Run: bash setup/build.sh"
  read -r -p "Press Enter to close." _; exit 1
fi
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
# shellcheck disable=SC1091
source install/setup.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
echo "Starting AMR Console in $repo ..."
python3 -u tools/amr_console/server.py 2>&1 | {
  found=""
  while IFS= read -r line; do
    case "$line" in
      "AMR Console: http"*)
        found=1
        url="${line#AMR Console: }"
        if [ -z "${AMR_CONSOLE_NO_BROWSER:-}" ]; then
          xdg-open "$url" >/dev/null 2>&1 &
          echo "AMR Console is running; its page has opened in your browser."
        else
          echo "AMR Console is running (browser not opened: AMR_CONSOLE_NO_BROWSER is set)."
          echo "Open this link in a browser on this computer, and share it with no one: $url"
        fi
        echo "Keep this window open while you use the console. Close it, or press Ctrl+C, to stop."
        break ;;
      *) echo "$line" ;;
    esac
  done
  [ -n "$found" ] || echo "The console did not start; see the messages above."
  cat
}
echo "AMR Console stopped."
read -r -p "Press Enter to close this window." _
