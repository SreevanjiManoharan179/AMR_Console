"""Orderly stop acknowledges acceptance separately from process completion."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from gazebo_shutdown import stop_gazebo
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class GazeboStopTests(unittest.TestCase):
    def setUp(self):
        self.target = {'pid': 123, 'start_ticks': 45, 'cmd': 'gz sim server', 'owned': True}
        self.env = {'GZ_PARTITION': 'amr_console_mapping_80', 'ROS_DOMAIN_ID': '80'}

    def test_ack_waits_for_owned_exit_and_never_signals_foreign_process(self):
        owned = Mock()
        owned.refresh.side_effect = [[self.target], [{'pid': 123, 'start_ticks': 99}]]
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout='data: true', stderr=''))
        inventory = lambda _: [self.target]
        result = stop_gazebo(owned, self.env, inventory=inventory, run=run,
                            sleep=Mock(), clock=lambda: 1.)
        self.assertTrue(result['accepted'])
        self.assertTrue(result['complete'])
        self.assertEqual([self.target], result['targets'])
        self.assertEqual('/server_control', run.call_args.args[0][3])
        self.assertEqual(self.env, run.call_args.kwargs['env'])
        owned._signal.assert_not_called()

    def test_accepted_request_with_survivor_is_not_completion(self):
        owned = Mock()
        owned.refresh.return_value = [self.target]
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout='data: true', stderr=''))
        result = stop_gazebo(owned, self.env, inventory=lambda _: [self.target],
                            run=run, timeout=0, clock=lambda: 1.)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['complete'])
        self.assertIn('did not exit', result['error'])

    def test_unacknowledged_stop_is_failure_not_an_exit(self):
        run = Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='timeout'))
        result = stop_gazebo(Mock(), self.env, inventory=lambda _: [self.target], run=run)
        self.assertFalse(result['accepted'])
        self.assertFalse(result['complete'])

    def test_foreign_only_session_sends_no_service_request(self):
        run = Mock()
        result = stop_gazebo(Mock(), self.env,
            inventory=lambda _: [dict(self.target, owned=False)], run=run)
        self.assertTrue(result['complete'])
        run.assert_not_called()

    def test_foreign_process_appearing_during_session_blocks_transport_stop(self):
        run = Mock()
        result = stop_gazebo(Mock(), self.env, inventory=lambda _: [self.target,
            dict(self.target, pid=999, owned=False)], run=run)
        self.assertFalse(result['complete'])
        self.assertIn('refused', result['error'])
        run.assert_not_called()
