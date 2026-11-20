#!/usr/bin/env bash
# Offline checks: no ROS nodes, simulator, network access or package installation.
#
#   bash scripts/check.sh            all five suites
#   bash scripts/check.sh -k grid    extra arguments go to the ROS package suite (pytest)
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$project_root/log/matplotlib}"
export MPLBACKEND=Agg
# Keep ROS launch-testing entry-point plugins out of these pure Python suites.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

echo '== ROS package: worlds, maps, exploration, delivery commands'
python3 -m pytest -q -p no:cacheprovider "$@"
echo '== Task layer: tags, grounding, runner, spatial memory'
python3 -m pytest -q -p no:cacheprovider tools/amr_task/tests
echo '== Grounding calibration (conformal pilot)'
(cd tools/grounding_calibration && python3 -m pytest -q -p no:cacheprovider)
echo '== Speech Lab scoring'
python3 -m pytest -q -p no:cacheprovider tools/speech_lab/tests
echo '== Console backend'
(cd tools/amr_console && python3 run_tests.py 2>&1 | tail -n 3)
for script in scripts/*.sh setup/*.sh; do
  bash -n "$script"
done
echo 'Offline checks passed; a simulation run is a separate test.'
