#!/usr/bin/env bash
# Start the AMR Speech Lab (speech and language test bench, no robot) and open its page.
#
#   bash setup/run_speech_lab.sh        (or the "AMR Speech Lab" app-menu entry, see install_launcher.sh)
#
# The page link carries a one-time access token. This script hands it straight to the browser and
# never prints or saves it. Keep the window open while you use the lab; close it, or press Ctrl+C,
# to stop. Close the AMR Console first: both start the same language model.
repo="$(cd -- "$(dirname -- "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
cd "$repo" || exit 1
if pgrep -f "tools/amr_console/server.py" >/dev/null; then
  echo "The AMR Console is running. Close it first: the Speech Lab starts the same language model."
  read -r -p "Press Enter to close this window." _; exit 1
fi
echo "Starting the AMR Speech Lab in $repo ..."
python3 -u tools/speech_lab/server.py --no-browser 2>&1 | {
  found=""
  while IFS= read -r line; do
    case "$line" in
      "AMR Speech Lab: http"*)
        found=1
        xdg-open "${line#AMR Speech Lab: }" >/dev/null 2>&1 &
        echo "The Speech Lab is running; its page has opened in your browser."
        echo "The models take about 20 s to load. Keep this window open; close it, or press Ctrl+C, to stop."
        break ;;
      *) echo "$line" ;;
    esac
  done
  [ -n "$found" ] || echo "The Speech Lab did not start; see the messages above."
  cat
}
echo "AMR Speech Lab stopped."
read -r -p "Press Enter to close this window." _
