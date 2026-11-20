"""Forced cleanup evidence tests use saved records, never processes or ROS."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_support
from worker import SessionWorker, forced_shutdown_evidence, trial_record_event, write_json


def setUpModule():
    test_support.assert_guard_active()


class ForcedCleanupEvidenceTests(unittest.TestCase):
    def test_nested_launcher_and_owned_signal_escalations_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            session = base / 'session.log'
            session.write_text('Runner completed its audited cleanup.\n')
            trial = base / 'output/mppi/medium/trial_01'
            trial.mkdir(parents=True)
            (trial / 'launch.log').write_text(
                "[ERROR] [gz-1]: process failed to terminate, escalating to 'SIGTERM'\n"
                '[gz-1] Escalating to SIGKILL on [Gazebo Sim Server]\n')
            write_json(trial / 'process_cleanup.json', {'signals': [
                {'pid': 123, 'signal': 'SIGINT'}, {'pid': 456, 'signal': 'SIGTERM'}]})
            evidence = forced_shutdown_evidence(session, base / 'output')
            self.assertEqual([item['signal'] for item in evidence['events']],
                             ['SIGTERM', 'SIGKILL', 'SIGTERM'])
            self.assertEqual(evidence['events'][0]['line_number'], 1)
            self.assertEqual(evidence['events'][2]['signal_index'], 1)
            self.assertEqual(evidence['events'][2]['pid'], 456)
            self.assertEqual(evidence['read_errors'], [])

    def test_ordinary_rclcpp_sigterm_handler_does_not_prove_forced_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            session = base / 'session.log'
            session.write_text('[rclcpp]: signal_handler(SIGINT/SIGTERM)\n')
            trial = base / 'output/mppi/medium/trial_01'
            trial.mkdir(parents=True)
            (trial / 'launch.log').write_text(
                "[INFO] [gz-1]: sending signal 'SIGINT' to process[gz-1]\n"
                '[map_server] [INFO] [rclcpp]: signal_handler(SIGINT/SIGTERM)\n'
                '[INFO] [gz-1]: process has finished cleanly\n')
            write_json(trial / 'process_cleanup.json', {'signals': [{'pid': 123, 'signal': 'SIGINT'}]})
            self.assertEqual(forced_shutdown_evidence(session, base / 'output'),
                             {'events': [], 'read_errors': []})

    def test_unreadable_evidence_is_reported_and_outside_run_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            session = base / 'session.log'
            session.write_text('')
            trial = base / 'output/mppi/medium/trial_01'
            trial.mkdir(parents=True)
            outside = base / 'outside.log'
            outside.write_text('Escalating to SIGKILL\n')
            (trial / 'launch.log').symlink_to(outside)
            (trial / 'process_cleanup.json').write_text('{broken json')
            evidence = forced_shutdown_evidence(session, base / 'output')
            self.assertEqual(evidence['events'], [])
            self.assertEqual(len(evidence['read_errors']), 2)
            self.assertIn('escapes', evidence['read_errors'][0]['error'])

    def test_stopped_success_with_nested_gazebo_escalation_is_forced_and_unclean(self):
        for escalated in (False, True):
            with self.subTest(escalated=escalated), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log_dir = root / 'data/logs/console/test_run'
                output = root / 'data/runs/test_run'
                log_dir.mkdir(parents=True)
                trial = output / 'mppi/medium/trial_01'
                trial.mkdir(parents=True)
                (log_dir / 'session.log').write_text('Navigation SUCCEEDED; runner canceled at Enter.\n')
                (trial / 'launch.log').write_text(
                    "[ERROR] [gz-1]: escalating to 'SIGTERM'\n" if escalated else
                    '[rclcpp]: signal_handler(SIGINT/SIGTERM)\n')
                write_json(trial / 'process_cleanup.json', {
                    'signals': [{'pid': 123, 'signal': 'SIGINT'}], 'live_survivors': []})
                audit = {'continue_allowed': not escalated, 'survivors': [], 'bond_abort': False}
                write_json(trial / 'shutdown_audit.json', audit)
                record = {'outcome': 'SUCCEEDED', 'time_s': 12.261,
                          'isolation_pass': not escalated, 'continue_allowed': not escalated}
                write_json(trial / 'record.json', record)
                raw_audit = (trial / 'shutdown_audit.json').read_bytes()
                raw_record = (trial / 'record.json').read_bytes()
                worker = SessionWorker({'kind': 'trials', 'log_dir': str(log_dir),
                                        'output_dir': str(output)})
                worker.stop_requested.set()
                worker.requested_stop = True
                worker.owned = SimpleNamespace(proc=SimpleNamespace(returncode=-2))
                worker.watch_trials = lambda: None
                events = []
                worker.emit = lambda kind, **fields: events.append({'type': kind, **fields})
                tracked = {'launch_returncode': -2, 'live_survivors': [],
                           'signals': [{'pid': 123, 'signal': 'SIGINT'}]}
                with (log_dir / 'session.log').open('a') as stream, \
                     patch('worker.ROOT', root), patch('worker.full_inventory', return_value=[]), \
                     patch('worker.stop_owned', return_value=(tracked, False, [])) as stop:
                    worker.combined_log = stream
                    worker.cleanup()
                result = json.loads((log_dir / 'cleanup.json').read_text())
                self.assertEqual(result['forced'], escalated)
                self.assertEqual(result['clean'], not escalated)
                self.assertEqual(result['survivors'], 0)
                self.assertFalse(result['bond_abort'])
                self.assertEqual(bool(result['forced_evidence']['events']), escalated)
                self.assertEqual(bool(result['audit']['failed_trial_audits']), escalated)
                self.assertEqual(any(event['type'] == 'alarm' for event in events), escalated)
                stop.assert_called_once_with(worker.owned, trials=True, launch_leader_only=False)
                stopped = trial_record_event(record, audit,
                    {'ROS_DOMAIN_ID': '37', 'GZ_PARTITION': 'test'}, index=0, identity='trial', stopped=True)
                self.assertEqual(stopped['outcome'], 'stopped')
                self.assertEqual(stopped['navigation_outcome'], 'SUCCEEDED')
                self.assertEqual((trial / 'shutdown_audit.json').read_bytes(), raw_audit)
                self.assertEqual((trial / 'record.json').read_bytes(), raw_record)


if __name__ == '__main__':
    unittest.main()
