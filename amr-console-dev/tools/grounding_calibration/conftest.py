# Lets pytest import grounding_calibration however it is invoked (from this folder, or from the
# repository root by a wider test run such as scripts/check.sh).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
