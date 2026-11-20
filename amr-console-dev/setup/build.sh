#!/usr/bin/env bash
# Build the ROS 2 package of this checkout (colcon, symlink install). Installs nothing system-wide.
#
#   bash setup/build.sh
set -e -o pipefail
exec bash "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/scripts/build.sh" "$@"
