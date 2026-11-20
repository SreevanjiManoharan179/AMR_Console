"""Pure supervision/deadman tests; they create no OS process or ROS node."""
import io
import math
import signal
import threading
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_support
from ros_adapter import Deadman, clamp_velocity, goal_geometry, validate_saved_map, RosSession
from supervision import PromptDetector, ContinueGate, stop_owned, signal_group
from worker import trial_record_event, progress_items, SessionWorker, write_json


def setUpModule():
    test_support.assert_guard_active()


class Clock:
    def __init__(self): self.value = 0.
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


class FakeProc:
    pid = 900001
    def __init__(self): self.code = None
    def poll(self): return self.code


class FakeOwned:
    def __init__(self, responds_to=signal.SIGKILL):
        self.proc = FakeProc()
        self.responds_to = responds_to
        self.live = [{'pid': self.proc.pid, 'pgid': self.proc.pid, 'start_ticks': 42, 'state': 'S'}]
        self.records = {self.proc.pid: self.live[0].copy()}
        self.lock = threading.Lock()
        self.signals = []

    def refresh(self): return list(self.live)
    def _signal(self, pid, sig):
        self.signals.append({'pid': pid, 'signal': sig.name})
        if sig == self.responds_to:
            self.live = []
            self.proc.code = 0


def finish_fake(owned):
    return {'signals': owned.signals, 'live_survivors': owned.refresh(),
            'launch_returncode': owned.proc.poll()}


class SupervisorTests(unittest.TestCase):
    def test_mapping_velocity_bounds_and_finite_validation(self):
        self.assertEqual(clamp_velocity(9, -9), (.26, -1.82))
        self.assertEqual(clamp_velocity(-9, 9), (-.26, 1.82))
        for value in (math.inf, math.nan, True, '0.1'):
            with self.assertRaises(ValueError): clamp_velocity(value, 0)

    def test_deadman_300_ms_wall_time_and_release(self):
        clock, writes = Clock(), []
        deadman = Deadman(lambda x, z: writes.append((x, z)), clock=clock)
        deadman.request(.15, 0)
        clock.sleep(.299)
        self.assertFalse(deadman.tick())
        clock.sleep(.001)
        self.assertTrue(deadman.tick())
        self.assertEqual(writes[-1], (0, 0))
        self.assertFalse(deadman.tick())
        deadman.request(.1, 0)
        deadman.request(0, 0)
        clock.sleep(1)
        self.assertFalse(deadman.tick())

    def test_request_refreshes_timeout(self):
        clock, writes = Clock(), []
        deadman = Deadman(lambda *args: writes.append(args), clock=clock)
        deadman.request(.1, 0)
        clock.sleep(.2)
        deadman.request(.1, 0)
        clock.sleep(.2)
        self.assertFalse(deadman.tick())
        clock.sleep(.1)
        self.assertTrue(deadman.tick())

    def test_prompt_without_newline_split_over_pipe_reads(self):
        detector = PromptDetector()
        self.assertEqual(detector.feed('Inspect Gazebo/RViz; press Enter to close '), [])
        result = detector.feed('this trial and continue: ')
        self.assertEqual(len(result), 1)
        self.assertEqual(detector.feed(''), [])
        self.assertEqual(len(detector.feed('Controller complete. Press Enter to start DWB: ')), 1)

    def test_continue_is_exactly_one_newline_per_observed_prompt(self):
        gate, stream = ContinueGate(), io.BytesIO()
        with self.assertRaises(RuntimeError): gate.send(stream)
        gate.observe()
        gate.send(stream)
        self.assertEqual(stream.getvalue(), b'\n')
        with self.assertRaises(RuntimeError): gate.send(stream)
        self.assertEqual(stream.getvalue(), b'\n')

    def test_job_group_signal_order_and_forced_step(self):
        clock, owned = Clock(), FakeOwned()
        def group(group_owner, sig): group_owner._signal(group_owner.proc.pid, sig)
        with patch('supervision.finish_tracking', finish_fake):
            report, forced, errors = stop_owned(owned, interrupt=.1, terminate=.1,
                clock=clock, sleep=clock.sleep, group_signal=group)
        self.assertEqual([item['signal'] for item in report['signals']], ['SIGINT', 'SIGTERM', 'SIGKILL'])
        self.assertTrue(forced)
        self.assertEqual(errors, [])
        self.assertEqual(report['initial_signal_policy'], 'owned_process_group_sigint_10s')

    def test_manual_coordinator_forwards_child_int_once(self):
        class Coordinator(FakeOwned):
            def __init__(self):
                super().__init__()
                self.forwarded = []
                self.live.append({'pid': 900002, 'pgid': self.proc.pid,
                                  'start_ticks': 43, 'state': 'S'})
                self.records[900002] = self.live[-1].copy()
            def _signal(self, pid, sig):
                self.signals.append({'pid': pid, 'signal': sig.name})
                self.assert_target = pid
                if pid == self.proc.pid and sig == signal.SIGINT:
                    self.forwarded.append({'pid': 900002, 'signal': 'SIGINT'})
                    self.live = []
                    self.proc.code = 0
        clock, owned = Clock(), Coordinator()
        with patch('supervision.finish_tracking', finish_fake):
            report, forced, errors = stop_owned(owned, launch_leader_only=True,
                clock=clock, sleep=clock.sleep,
                group_signal=lambda *_args: self.fail('Manual ROS launch must not get a group INT.'))
        self.assertEqual(report['signals'], [{'pid': owned.proc.pid, 'signal': 'SIGINT'}])
        self.assertEqual(len(owned.forwarded), 1)
        self.assertFalse(forced)
        self.assertEqual(errors, [])
        self.assertEqual(report['initial_signal_policy'], 'manual_ros_launch_leader_only_sigint_10s')

    def test_manual_leader_grace_keeps_owned_fallback_strict(self):
        clock, owned = Clock(), FakeOwned()
        with patch('supervision.finish_tracking', finish_fake):
            report, forced, errors = stop_owned(owned, launch_leader_only=True,
                interrupt=.1, terminate=.1, clock=clock, sleep=clock.sleep,
                group_signal=lambda *_args: self.fail('Leader-only policy cannot signal the group.'))
        self.assertEqual([item['signal'] for item in report['signals']], ['SIGINT', 'SIGTERM', 'SIGKILL'])
        self.assertTrue(forced)
        self.assertEqual(errors, [])
        self.assertEqual(report['initial_signal_policy'], 'manual_ros_launch_leader_only_sigint_10s')

    def test_trial_interrupt_targets_runner_and_allows_90_seconds(self):
        clock, owned = Clock(), FakeOwned(signal.SIGINT)
        with patch('supervision.finish_tracking', finish_fake):
            report, forced, errors = stop_owned(owned, trials=True, clock=clock, sleep=clock.sleep,
                group_signal=lambda *args: self.fail('Graceful trial STOP must not signal the group.'))
        self.assertEqual([item['signal'] for item in report['signals']], ['SIGINT'])
        self.assertFalse(forced)
        self.assertEqual(errors, [])
        self.assertEqual(report['initial_signal_policy'], 'trial_runner_only_sigint_90s')

    def test_runner_timeout_precedes_emergency_int_term_kill(self):
        clock, owned = Clock(), FakeOwned()
        def group(group_owner, sig): group_owner._signal(group_owner.proc.pid, sig)
        with patch('supervision.finish_tracking', finish_fake):
            report, forced, _ = stop_owned(owned, trials=True, graceful=90, interrupt=.1,
                terminate=.1, clock=clock, sleep=clock.sleep, group_signal=group)
        self.assertGreaterEqual(clock.value, 90)
        self.assertEqual([item['signal'] for item in report['signals']],
                         ['SIGINT', 'SIGTERM', 'SIGINT', 'SIGTERM', 'SIGKILL'])
        self.assertTrue(forced)

    def test_foreign_group_member_is_never_signalled(self):
        owned, writes = FakeOwned(), []
        foreign = {'pid': 900002, 'pgid': owned.proc.pid, 'start_ticks': 43, 'state': 'S'}
        table = {owned.proc.pid: owned.live[0], foreign['pid']: foreign}
        with self.assertRaises(RuntimeError):
            signal_group(owned, signal.SIGINT, table_reader=lambda: table,
                         killer=lambda *args: writes.append(args))
        self.assertEqual(writes, [])

    def test_map_goal_bounds_rotation_and_quaternion(self):
        params = dict(origin_x=-1, origin_y=-2, origin_yaw=0, width=100, height=80, resolution=.05)
        z, w = goal_geometry(0, 0, math.pi/2, **params)
        self.assertAlmostEqual(z, math.sqrt(.5))
        self.assertAlmostEqual(w, math.sqrt(.5))
        self.assertAlmostEqual(z*z+w*w, 1)
        for coordinates in ((4, 0, 0), (0, 2, 0), (-1.01, 0, 0), (0, 0, math.inf)):
            with self.assertRaises(ValueError): goal_geometry(*coordinates, **params)
        rotated = dict(origin_x=0, origin_y=0, origin_yaw=math.pi/2,
                       width=40, height=40, resolution=.05)
        goal_geometry(-1, 1, 0, **rotated)
        with self.assertRaises(ValueError): goal_geometry(1, 1, 0, **rotated)

    def test_map_save_requires_zero_result_and_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'test_console_map'
            target.with_suffix('.yaml').write_text('image: test_console_map.pgm\n')
            with self.assertRaises(RuntimeError): validate_saved_map(0, target)
            target.with_suffix('.pgm').write_text('P2\n1 1\n255\n255\n')
            self.assertEqual(validate_saved_map(0, target)['result'], 0)
            with self.assertRaises(RuntimeError): validate_saved_map(1, target)

    def test_trial_harness_overrides_success_and_transport_is_authoritative(self):
        record = {'outcome': 'SUCCEEDED', 'isolation_pass': False,
                  'continue_allowed': False, 'time_s': None, 'wall_time_s': 12.3,
                  'ros_domain_id': 99, 'gz_partition': 'incorrect_record_pair'}
        transport = {'ROS_DOMAIN_ID': '36', 'GZ_PARTITION': 'actual_transport'}
        event = trial_record_event(record, {}, transport, index=0, identity='mppi/medium/trial_01')
        self.assertEqual(event['outcome'], 'harness')
        self.assertEqual(event['navigation_outcome'], 'SUCCEEDED')
        self.assertEqual(event['ros_domain_id'], 36)
        self.assertEqual(event['gz_partition'], 'actual_transport')
        self.assertEqual(event['time_s'], 12.3)
        self.assertFalse(event['bond_abort'])
        event = trial_record_event(record, {}, transport, index=0, identity='trial', stopped=True)
        self.assertEqual(event['outcome'], 'stopped')
        self.assertEqual(record['outcome'], 'SUCCEEDED')

    def test_bond_count_requires_classifier_proof_and_deduplicates_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            trial = output / 'mppi/medium/trial_01'
            transport = {'ROS_DOMAIN_ID': '36', 'GZ_PARTITION': 'actual', 'run_ordinal': 1}
            record = {'outcome': 'SUCCEEDED', 'isolation_pass': True, 'continue_allowed': True}
            write_json(trial / 'transport.json', transport)
            write_json(trial / 'record.json', record)
            # Empty survivors alone cannot be recorded as a Nav2 bond abort.
            write_json(trial / 'shutdown_audit.json', {'survivors': [], 'isolation_pass': True})
            worker = SessionWorker({'kind': 'trials', 'log_dir': directory, 'output_dir': directory})
            events = []
            worker.emit = lambda kind, **fields: events.append({'type': kind, **fields})
            worker.watch_trials()
            self.assertEqual(worker.bond_abort_trials, set())
            self.assertEqual(events[0]['index'], 0)
            worker.seen_records.clear()
            write_json(trial / 'shutdown_audit.json', {'survivors': [], 'isolation_pass': True,
                       'allowed_exception': True, 'bond_abort': True})
            worker.watch_trials()
            worker.watch_trials()
            self.assertEqual(len(worker.bond_abort_trials), 1)
            self.assertEqual(events[-1]['outcome'], 'pass')
            self.assertTrue(events[-1]['bond_abort'])

    def test_progress_matches_runner_single_select_order(self):
        items = progress_items({'controller': 'all', 'medium': 2, 'hard': 1})
        self.assertEqual(len(items), 9)
        self.assertEqual([item['controller'] for item in items],
                         ['mppi']*3 + ['dwb']*3 + ['rpp']*3)
        self.assertEqual([item['goal'] for item in items[:3]], ['medium_01', 'medium_02', 'hard_01'])

    def test_navigation_timeout_cancels_and_keeps_handle_until_result(self):
        class Future:
            def __init__(self, value=None, done=False):
                self.value, self.complete, self.callbacks = value, done, []
            def done(self): return self.complete
            def exception(self): return None
            def result(self): return self.value
            def add_done_callback(self, callback):
                self.callbacks.append(callback)
                if self.complete: callback(self)
            def finish(self):
                self.complete = True
                for callback in self.callbacks: callback(self)
        class Handle:
            accepted = True
            def __init__(self, fail_cancel=False):
                self.future = Future()
                self.canceled = 0
                self.fail_cancel = fail_cancel
            def get_result_async(self): return self.future
            def cancel_goal_async(self):
                self.canceled += 1
                if self.fail_cancel: raise RuntimeError('Injected cancellation failure')
                return Future(done=True)
        def goal():
            return SimpleNamespace(pose=SimpleNamespace(header=SimpleNamespace(frame_id=None, stamp=None),
                pose=SimpleNamespace(position=SimpleNamespace(x=0., y=0.),
                                     orientation=SimpleNamespace(z=0., w=1.))))
        for fail_cancel in (False, True):
            session = RosSession.__new__(RosSession)
            session.active_goal = None
            session.stop_requested = threading.Event()
            session.map = SimpleNamespace(info=SimpleNamespace(width=100, height=100, resolution=.05,
                origin=SimpleNamespace(position=SimpleNamespace(x=-1., y=-1.),
                                       orientation=SimpleNamespace(x=0., y=0., z=0., w=1.))))
            session.goal_type = SimpleNamespace(Goal=goal)
            session.node = SimpleNamespace(get_clock=lambda: SimpleNamespace(
                now=lambda: SimpleNamespace(to_msg=lambda: None)))
            handle = Handle(fail_cancel)
            session.action = SimpleNamespace(server_is_ready=lambda: True,
                send_goal_async=lambda _goal: Future(handle, done=True))
            session.wait = lambda condition, _timeout, _label: self.assertTrue(condition())
            def result(future, _timeout, label):
                if label == 'Navigation goal': raise TimeoutError('Injected navigation timeout')
                return future.result()
            session.result = result
            with self.assertRaises(TimeoutError): session.navigate({'x': 0., 'y': 0., 'yaw': 0.})
            self.assertEqual(handle.canceled, 1)
            self.assertIs(session.active_goal, handle)
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                session.navigate({'x': 0., 'y': 0., 'yaw': 0.})
            handle.future.finish()
            self.assertIsNone(session.active_goal)

    def test_http_protocol_pipe_loss_requests_stop_without_interrupting_cleanup(self):
        class BrokenStream:
            def write(self, _text): raise BrokenPipeError('HTTP owner exited')
            def flush(self): pass
        worker = SessionWorker({'kind': 'trials', 'log_dir': '/tmp', 'output_dir': '/tmp'})
        with patch('worker.sys.stdout', BrokenStream()):
            worker.emit('log', text='Session output', level='info')
            # Subsequent cleanup/state events must also be safe to emit.
            worker.emit('finished', cleanup={'clean': True})
        self.assertTrue(worker.stop_requested.is_set())
        self.assertTrue(worker.requested_stop)

    def test_between_controller_waiting_follows_latest_trial_record(self):
        worker = SessionWorker({'kind': 'trials', 'log_dir': '/tmp', 'output_dir': '/tmp'})
        events = []
        worker.emit = lambda kind, **fields: events.append(kind)
        worker.watch_trials = lambda: worker.emit('trial', index=0, outcome='pass')
        worker.handle_control({'type': '_waiting',
            'prompt': 'Controller complete. Press Enter to start DWB: '})
        self.assertEqual(events, ['trial', 'waiting'])
        self.assertTrue(worker.continue_gate.waiting)


if __name__ == '__main__':
    unittest.main()
