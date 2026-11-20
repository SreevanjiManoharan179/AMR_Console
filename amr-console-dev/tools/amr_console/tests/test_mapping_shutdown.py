"""Offline lifecycle shutdown tests; no ROS node or OS process is started."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_support
from ros_adapter import RosSession, quiesce_mapping_lifecycle


def setUpModule():
    test_support.assert_guard_active()


class Clock:
    def __init__(self):
        self.value = 0.
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class MappingShutdownTests(unittest.TestCase):
    def test_active_deactivates_publication_threads_before_cleanup(self):
        calls, states = [], iter(((3, 'active'), (2, 'inactive'), (1, 'unconfigured')))
        def query():
            value = next(states)
            calls.append(('state', value[0]))
            return value
        def change(transition):
            calls.append(('transition', transition))
            return True
        report = quiesce_mapping_lifecycle(query, change)
        self.assertTrue(report['success'])
        self.assertEqual(calls, [('state', 3), ('transition', 4), ('state', 2),
                                 ('transition', 2), ('state', 1)])
        self.assertEqual(report['final_state_id'], 1)
        self.assertEqual([item['transition_label'] for item in report['records']
                          if item['operation'] == 'change_state'], ['deactivate', 'cleanup'])

    def test_inactive_only_needs_cleanup_and_unconfigured_needs_no_transition(self):
        states = iter(((2, 'inactive'), (1, 'unconfigured')))
        transitions = []
        report = quiesce_mapping_lifecycle(lambda: next(states),
            lambda transition: transitions.append(transition) or True)
        self.assertTrue(report['success'])
        self.assertEqual(transitions, [2])
        report = quiesce_mapping_lifecycle(lambda: (1, 'unconfigured'),
            lambda _transition: self.fail('Unconfigured SLAM needs no further transition.'))
        self.assertTrue(report['success'])

    def test_rejected_deactivation_cannot_cleanup_and_records_response(self):
        transitions = []
        report = quiesce_mapping_lifecycle(lambda: (3, 'active'),
            lambda transition: transitions.append(transition) or False)
        self.assertFalse(report['success'])
        self.assertEqual(transitions, [4])
        self.assertFalse(report['records'][-1]['success'])
        self.assertIn('rejected', report['error'])

    def test_accepted_transition_requires_expected_state_before_cleanup(self):
        transitions = []
        report = quiesce_mapping_lifecycle(lambda: (3, 'active'),
            lambda transition: transitions.append(transition) or True)
        self.assertFalse(report['success'])
        self.assertEqual(transitions, [4])
        self.assertIn('expected state 2, received 3', report['error'])

    def test_query_exception_and_finalized_state_are_not_waived(self):
        def broken_query():
            raise RuntimeError('Injected state service failure')
        report = quiesce_mapping_lifecycle(broken_query, lambda _transition: True)
        self.assertFalse(report['success'])
        self.assertEqual(report['records'][0]['error'], 'Injected state service failure')
        report = quiesce_mapping_lifecycle(lambda: (4, 'finalized'), lambda _transition: True)
        self.assertFalse(report['success'])
        self.assertIn('state 4', report['error'])

    def make_session(self, clock, *, available=True, transition_delay=0., context_ok=True):
        state = [3]
        destroyed, calls = [], []
        class Future:
            def __init__(self, response, delay=0.):
                self.response, self.when = response, clock() + delay
            def done(self):
                return clock() >= self.when
            def exception(self):
                return None
            def result(self):
                return self.response
        class Client:
            def __init__(self, name):
                self.name = name
            def service_is_ready(self):
                return available
            def call_async(self, request):
                if self.name.endswith('get_state'):
                    calls.append(('state', state[0]))
                    return Future(SimpleNamespace(current_state=SimpleNamespace(
                        id=state[0], label={3: 'active', 2: 'inactive', 1: 'unconfigured'}[state[0]])), .02)
                transition = request.transition.id
                calls.append(('transition', transition))
                state[0] = {4: 2, 2: 1}[transition]
                return Future(SimpleNamespace(success=True), transition_delay)
        session = RosSession.__new__(RosSession)
        session.kind = 'mapping'
        session.stop_requested = threading.Event()
        session.stop_requested.set()
        session.context = SimpleNamespace(ok=lambda: context_ok)
        session.node = SimpleNamespace(create_client=lambda _kind, name: Client(name),
            destroy_client=lambda client: destroyed.append(client.name))
        types = (SimpleNamespace(Request=lambda: object()),
                 SimpleNamespace(Request=lambda: SimpleNamespace(transition=SimpleNamespace(id=None))))
        return session, types, destroyed, calls

    def test_stop_flag_does_not_interrupt_bounded_shutdown_service_waits(self):
        clock = Clock()
        session, types, destroyed, calls = self.make_session(clock)
        with patch('ros_adapter.time.monotonic', clock), patch('ros_adapter.time.sleep', clock.sleep):
            report = session.quiesce_mapping(service_types=types)
        self.assertTrue(report['success'])
        self.assertTrue(session.stop_requested.is_set())
        self.assertEqual(calls, [('state', 3), ('transition', 4), ('state', 2),
                                 ('transition', 2), ('state', 1)])
        self.assertEqual(destroyed, ['/slam_toolbox/get_state', '/slam_toolbox/change_state'])
        self.assertTrue(all(record['success'] for record in report['records']))

    def test_unavailable_service_has_one_deadline_and_destroys_clients(self):
        clock = Clock()
        session, types, destroyed, calls = self.make_session(clock, available=False)
        with patch('ros_adapter.time.monotonic', clock), patch('ros_adapter.time.sleep', clock.sleep):
            report = session.quiesce_mapping(timeout=1, service_types=types)
        self.assertFalse(report['success'])
        self.assertGreaterEqual(clock(), 1)
        self.assertLess(clock(), 1.03)
        self.assertEqual(calls, [])
        self.assertIn('timed out', report['error'])
        self.assertFalse(report['records'][0]['success'])
        self.assertEqual(len(destroyed), 2)

    def test_transition_future_timeout_retains_failed_evidence_and_no_cleanup(self):
        clock = Clock()
        session, types, destroyed, calls = self.make_session(clock, transition_delay=2)
        with patch('ros_adapter.time.monotonic', clock), patch('ros_adapter.time.sleep', clock.sleep):
            report = session.quiesce_mapping(timeout=1, service_types=types)
        self.assertFalse(report['success'])
        self.assertEqual(calls, [('state', 3), ('transition', 4)])
        transition = [record for record in report['records'] if record['operation'] == 'change_state'][0]
        self.assertFalse(transition['success'])
        self.assertIn('timed out', transition['error'])
        self.assertEqual(len(destroyed), 2)

    def test_invalid_context_fails_before_deadline_without_waiver(self):
        clock = Clock()
        session, types, destroyed, _calls = self.make_session(clock, available=False, context_ok=False)
        with patch('ros_adapter.time.monotonic', clock), patch('ros_adapter.time.sleep', clock.sleep):
            report = session.quiesce_mapping(service_types=types)
        self.assertFalse(report['success'])
        self.assertEqual(clock(), 0.)
        self.assertIn('context became invalid', report['error'])
        self.assertEqual(len(destroyed), 2)


if __name__ == '__main__':
    unittest.main()
