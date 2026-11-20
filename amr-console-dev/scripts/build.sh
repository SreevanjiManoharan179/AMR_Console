#!/usr/bin/env bash
# Build all workspace packages using the system ROS Jazzy underlay.
set -e -o pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
if [[ ! -f /opt/ros/jazzy/setup.bash ]]; then
  echo 'ROS Jazzy underlay is missing: /opt/ros/jazzy/setup.bash' >&2
  exit 2
fi
source /opt/ros/jazzy/setup.bash
command -v colcon >/dev/null || { echo 'colcon is not installed.' >&2; exit 2; }
colcon build --symlink-install
source install/setup.bash
echo 'Build passed. In your terminal: source /opt/ros/jazzy/setup.bash && source install/setup.bash'
