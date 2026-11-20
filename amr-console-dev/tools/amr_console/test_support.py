"""Guard offline tests against overlap with the user's simulation runs.

The guard is acquired before discovery, so no test module can start a process
while a terminal trial owns the shared lock. This module imports no ROS nodes.
"""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
_active = False


class TestEnvironmentBusy(RuntimeError):
    """The test suite must wait for the user's simulation to finish."""


def _inventory(root):
    scripts = Path(root) / 'src/amr_physical_ai/scripts'
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from transport_isolation import process_inventory
    return process_inventory()


def assert_guard_active():
    if not _active:
        raise TestEnvironmentBusy(
            'Run python3 tools/amr_console/run_tests.py: the simulation lock '
            'and process inventory must be checked before offline tests.')


@contextmanager
def guarded_test_environment(root=ROOT, inventory=None):
    """Hold the shared exclusive lock while all offline tests execute."""
    global _active
    if _active:
        raise RuntimeError('An offline test guard is already active.')
    lock_path = Path(root) / 'log/simulation.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TestEnvironmentBusy(
                'The simulation lock is held; finish the terminal trial run '
                'before running console tests.') from exc
        try:
            survivors = (inventory or (lambda: _inventory(root)))()
        except Exception as exc:
            raise TestEnvironmentBusy(
                f'The full simulation process inventory could not be read: {exc}') from exc
        if survivors:
            identities = ', '.join(
                f"PID {item.get('pid', '?')}: {item.get('cmd', item.get('comm', '?'))}"
                for item in survivors)
            raise TestEnvironmentBusy(
                'Simulation processes are present; no tests were started: ' + identities)
        _active = True
        yield {'lock': str(lock_path), 'process_inventory': survivors}
    finally:
        _active = False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
