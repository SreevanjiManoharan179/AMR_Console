#!/usr/bin/env bash
# Check that this computer can build and run AMR Console. Reads only; installs nothing.
#
#   bash setup/check_system.sh
#
# Prints one line per requirement and, if something is missing, the apt command that would
# install it (you run that yourself). Writes the detected Gazebo version to data/environment.txt,
# which the console shows on its Environment tab.
set -u
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
missing_apt=()
problems=0

ok()   { printf '  ok       %s\n' "$1"; }
bad()  { printf '  MISSING  %s\n' "$1"; problems=$((problems + 1)); }

echo "AMR Console system check ($repo)"

. /etc/os-release 2>/dev/null || true
if [ "${VERSION_ID:-}" = "24.04" ]; then ok "Ubuntu 24.04"; else bad "Ubuntu 24.04 (found: ${PRETTY_NAME:-unknown})"; fi

if [ -f /opt/ros/jazzy/setup.bash ]; then
  ok "ROS 2 Jazzy (/opt/ros/jazzy)"
  # shellcheck disable=SC1091
  set +u; source /opt/ros/jazzy/setup.bash; set -u   # the ROS setup script reads unset variables
else
  bad "ROS 2 Jazzy: follow https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html"
fi

if command -v ros2 >/dev/null 2>&1; then
  for pkg in nav2_bringup nav2_mppi_controller nav2_minimal_tb3_sim slam_toolbox ros_gz_sim ros_gz_bridge \
             rmw_cyclonedds_cpp robot_state_publisher rviz2 xacro tf2_ros nav2_simple_commander; do
    if ros2 pkg prefix "$pkg" >/dev/null 2>&1; then ok "ROS package $pkg"
    else bad "ROS package $pkg"; missing_apt+=("ros-jazzy-${pkg//_/-}"); fi
  done
fi

for tool in colcon gz python3 xdg-open; do
  if command -v "$tool" >/dev/null 2>&1; then ok "command $tool"; else
    bad "command $tool"
    case "$tool" in
      colcon) missing_apt+=(python3-colcon-common-extensions) ;;
      xdg-open) missing_apt+=(xdg-utils) ;;
    esac
  fi
done

for module in yaml numpy scipy PIL; do
  if python3 -c "import $module" >/dev/null 2>&1; then ok "Python module $module"; else
    bad "Python module $module"
    case "$module" in
      yaml) missing_apt+=(python3-yaml) ;; numpy) missing_apt+=(python3-numpy) ;;
      scipy) missing_apt+=(python3-scipy) ;; PIL) missing_apt+=(python3-pil) ;;
    esac
  fi
done
python3 -c "import pytest" >/dev/null 2>&1 && ok "Python module pytest (offline tests)" \
  || echo "  optional pytest: needed only for bash scripts/check.sh (apt: python3-pytest)"

for optional in wmctrl ffmpeg; do
  command -v "$optional" >/dev/null 2>&1 && ok "optional $optional" \
    || echo "  optional $optional: the console works without it (window arranging / screen video)"
done

mkdir -p "$repo/data"
gazebo="$(gz sim --versions 2>/dev/null | head -n 1)"
if [ -n "$gazebo" ]; then
  printf 'Gazebo Sim %s\n' "$gazebo" > "$repo/data/environment.txt"
  ok "Gazebo Sim $gazebo (recorded in data/environment.txt)"
fi

echo
if [ "$problems" -eq 0 ]; then
  echo "All requirements found. Next: bash setup/build.sh"
else
  echo "$problems requirement(s) missing."
  if [ "${#missing_apt[@]}" -gt 0 ]; then
    echo "To install the missing packages, run this yourself:"
    echo "  sudo apt install ${missing_apt[*]}"
  fi
  exit 1
fi
