"""Compatibility import for the shared source-owned Gazebo shutdown helper."""
import supervision  # noqa: F401  (side effect: puts the source scripts on the import path)

from gazebo_stop import stop_gazebo  # noqa: F401  (re-exported for worker.py)
