"""The mandatory guard refuses before tests can create fake processes."""
import fcntl
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_support
import run_tests


def setUpModule():
    test_support.assert_guard_active()


class GuardTests(unittest.TestCase):
    def test_running_suite_holds_actual_simulation_lock(self):
        handle = (test_support.ROOT / 'log/simulation.lock').open('a+')
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            handle.close()

    def test_external_lock_refuses_before_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'log').mkdir()
            handle = (root / 'log/simulation.lock').open('a+')
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            inventory = mock.Mock(return_value=[])
            try:
                with mock.patch.object(test_support, '_active', False):
                    with self.assertRaisesRegex(test_support.TestEnvironmentBusy, 'lock is held'):
                        with test_support.guarded_test_environment(root, inventory):
                            self.fail('Guard must not yield while the lock is held.')
                inventory.assert_not_called()
            finally:
                handle.close()

    def test_foreign_simulation_refuses_after_lock_before_yield(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = mock.Mock(return_value=[{'pid': 123, 'cmd': 'rviz2', 'owned': False}])
            with mock.patch.object(test_support, '_active', False):
                with self.assertRaisesRegex(test_support.TestEnvironmentBusy, 'PID 123: rviz2'):
                    with test_support.guarded_test_environment(root, inventory):
                        self.fail('Guard must not yield while a simulator remains.')
            inventory.assert_called_once_with()

    def test_failed_inventory_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(test_support, '_active', False):
                with self.assertRaisesRegex(test_support.TestEnvironmentBusy, 'could not be read'):
                    with test_support.guarded_test_environment(
                            Path(directory), mock.Mock(side_effect=PermissionError('hidden process'))):
                        self.fail('An unreadable inventory cannot permit tests.')

    def test_direct_test_invocation_requires_guard(self):
        with mock.patch.object(test_support, '_active', False):
            with self.assertRaisesRegex(test_support.TestEnvironmentBusy, 'run_tests.py'):
                test_support.assert_guard_active()

    def test_guard_releases_lock_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(test_support, '_active', False):
                with self.assertRaisesRegex(ValueError, 'fixture failure'):
                    with test_support.guarded_test_environment(root, lambda: []):
                        raise ValueError('fixture failure')
                with test_support.guarded_test_environment(root, lambda: []):
                    test_support.assert_guard_active()

    def test_refused_runner_never_discovers_or_runs_tests(self):
        with mock.patch.object(run_tests, 'guarded_test_environment',
                               side_effect=test_support.TestEnvironmentBusy('terminal run active')):
            with mock.patch.object(run_tests.unittest.defaultTestLoader, 'discover') as discover:
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(2, run_tests.main([]))
                self.assertIn('TESTS REFUSED', output.getvalue())
                discover.assert_not_called()
