"""Offline interlocks, prepared specs, paths and operations; no ROS/processes."""
import json
from pathlib import Path
import shlex
import threading
import time
import unittest
from unittest import mock

from engine import ConsoleBackend
from errors import ConsoleError
from fixtures import ENVIRONMENT, FakeClock, FakeSupervisors, FixtureRepo
import registry
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.repo = FixtureRepo()
        self.clock = FakeClock()
        self.supervisors = FakeSupervisors()
        self.simulation_processes = []
        self.external_lock = False
        self.domain = 80
        self.backend = ConsoleBackend(
            root=self.repo.root, inventory=lambda: self.simulation_processes,
            lock_probe=lambda: self.external_lock, clock=self.clock,
            wall_clock=self.clock.wall, supervisor_factory=self.supervisors,
            transport_selector=lambda kind: {
                'ROS_DOMAIN_ID': str(self.domain),
                'GZ_PARTITION': f'amr_console_{kind}_{self.domain}'},
            environment=dict(ENVIRONMENT))

    def tearDown(self):
        if self.backend.status()['state'] != 'idle':
            self.simulation_processes = []
            self.backend.stop()
        self.repo.close()

    def defaults(self, action_id):
        action = next(a for a in self.backend.actions()['actions'] if a['id'] == action_id)
        return {param['name']: param['default'] for param in action['params']}

    def install_proposed_system(self):
        """Add the default office world, its exact map and the explorer files to the fixture."""
        self.repo.copy_from_repo(*registry.map_files('proposed_system'), *registry.EXPLORE_FILES)

    def approve_o2(self, value=True):
        self.repo.write('tools/amr_console/approvals.yaml',
                        f"proposed_system_places: {'true' if value else 'false'}\n")

    def prepare(self, action_id, **params):
        values = self.defaults(action_id)
        values.update(params)
        preview = self.backend.preview(action_id, values)
        return values, preview

    def start(self, action_id, **params):
        values, preview = self.prepare(action_id, **params)
        result = self.backend.run(action_id, values, spec_id=preview['spec_id'])
        return result, preview

    def assert_error(self, status, callback, code=None):
        with self.assertRaises(ConsoleError) as caught:
            callback()
        error = caught.exception
        self.assertEqual(status, error.status)
        self.assertTrue(error.reason)
        if code:
            self.assertEqual(code, error.code)
        return error

    def test_registry_has_all_unique_contract_actions(self):
        actions = self.backend.actions()['actions']
        ids = [action['id'] for action in actions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({'env.build', 'env.check', 'map.start', 'map.explore', 'map.save', 'nav.start',
                          'nav.goal', 'commands.typed'}, set(ids))
        for action in actions:
            for key in ('tab', 'label', 'description', 'params', 'command', 'session',
                        'requires_session', 'teleop', 'locked', 'lock_reason', 'confirm_phrase'):
                self.assertIn(key, action)

    def test_false_approvals_keep_o2_and_unbuilt_maps_locked(self):
        actions = {a['id']: a for a in self.backend.actions()['actions']}
        self.assertTrue(actions['commands.typed']['locked'])
        self.assertIn('proposed_system_places: true', actions['commands.typed']['lock_reason'])
        # The fixture holds only the sandbox map, so the office world is offered but locked.
        for action_id, name in (('nav.start', 'map'), ('map.start', 'world')):
            param = next(p for p in actions[action_id]['params'] if p['name'] == name)
            self.assertEqual(['tb3_sandbox'], param['options'])
            self.assertIn('proposed_system', param['locked_options'])
            self.assert_error(409, lambda: self.prepare(action_id, **{name: 'proposed_system'}), 'locked_option')
        self.assertTrue(actions['map.explore']['locked'])

    def test_o2_approval_unlocks_the_command_action_and_old_keys_do_not(self):
        self.approve_o2()
        actions = {a['id']: a for a in self.backend.actions()['actions']}
        self.assertFalse(actions['commands.typed']['locked'])
        self.assertIn('no LLM', actions['commands.typed']['description'])
        # The retired approval keys are unknown now: the file fails closed.
        self.repo.write('tools/amr_console/approvals.yaml', 'legacy_locations: true\n')
        action = next(a for a in self.backend.actions()['actions'] if a['id'] == 'commands.typed')
        self.assertTrue(action['locked'])
        self.assertIn('could not be read safely', action['lock_reason'])

    def test_o2_uses_session_transport_and_retains_operation_record(self):
        self.install_proposed_system()
        self.approve_o2()
        self.start('nav.start', map='proposed_system')
        self.assertIn('ground_truth:=true', self.supervisors.last.spec['argv'])
        values, preview = self.prepare('commands.typed', text='go to the kitchen')
        self.assertIsNone(preview['blocked_reason'])
        self.assertEqual('80', preview['env']['ROS_DOMAIN_ID'])
        self.assertEqual('amr_console_navigation_80', preview['env']['GZ_PARTITION'])
        self.backend.run('commands.typed', values, spec_id=preview['spec_id'])
        record = json.loads((Path(self.backend.record['log_dir']) / 'run.json').read_text())
        self.assertEqual('commands.typed', record['operations'][-1]['action_id'])
        self.assertEqual(values, record['operations'][-1]['params'])
        self.assertTrue(record['operations'][-1]['ok'])

    def test_o2_cannot_use_an_old_session_without_truth(self):
        self.install_proposed_system()
        self.start('nav.start', map='proposed_system')  # Approval is false at launch time.
        self.assertNotIn('ground_truth:=true', self.supervisors.last.spec['argv'])
        self.approve_o2()
        values, preview = self.prepare('commands.typed')
        self.assertIn('ground truth', preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('commands.typed', values, spec_id=preview['spec_id']))
        self.assertFalse([c for c in self.supervisors.last.controls if c[0] == 'operation'])

    def test_o2_is_refused_on_a_map_without_approved_places(self):
        self.approve_o2()
        self.start('nav.start', map='tb3_sandbox')
        self.assertFalse(self.backend.status()['session']['record_truth'])
        values, preview = self.prepare('commands.typed')
        self.assertIn('ground truth', preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('commands.typed', values, spec_id=preview['spec_id']))

    def test_unknown_action_and_unknown_params(self):
        self.assert_error(404, lambda: self.backend.preview('trials.foo', {}))
        for removed in ('nav.stage1', 'trials.visual', 'trials.one', 'trials.study'):
            with self.subTest(removed=removed):
                self.assert_error(404, lambda: self.backend.preview(removed, {}), 'unknown_action')
                self.assert_error(404, lambda: self.backend.run(removed, {}, spec_id='s_x'), 'unknown_action')
        self.assert_error(422, lambda: self.backend.preview('env.check', {'injected': 'anything'}))

    def test_removed_maps_invalid_selects_and_nonfinite_goals(self):
        for action_id, params in (('map.start', {'world': 'office_v1'}),
                                  ('map.start', {'world': 'traversal_20260930_121704'}),
                                  ('nav.start', {'map': 'objective1_map'}),
                                  ('nav.start', {'map': 'office_v2'}),
                                  ('nav.start', {'controller': 'all'}),
                                  ('nav.start', {'controller': True})):
            with self.subTest(action_id=action_id, params=params):
                self.assert_error(422, lambda: self.prepare(action_id, **params))
        self.assertFalse(self.supervisors.instances)
        self.start('nav.start')
        for value in (float('inf'), float('-inf'), float('nan')):
            with self.subTest(value=value):
                self.assert_error(422, lambda: self.prepare('nav.goal', x=value))

    def test_nav_controller_selects_params_file_without_fake_argument(self):
        for controller in ('mppi', 'dwb', 'rpp'):
            with self.subTest(controller=controller):
                _, preview = self.prepare('nav.start', controller=controller)
                argv = shlex.split(preview['command'])
                self.assertIn('--noninteractive', argv)
                self.assertTrue(any(arg.startswith('map:=') for arg in argv))
                self.assertTrue(any(arg.startswith('params_file:=') and
                                    arg.endswith(f'nav2_{controller}.yaml') for arg in argv))
                self.assertFalse(any(arg.startswith('controller:=') for arg in argv))
                self.assertEqual('80', preview['env']['ROS_DOMAIN_ID'])
                self.assertEqual('amr_console_navigation_80', preview['env']['GZ_PARTITION'])

    def test_run_executes_prepared_exact_argv_and_output(self):
        result, preview = self.start('nav.start', controller='dwb')
        spec = self.supervisors.last.spec
        self.assertEqual(shlex.split(preview['command']), spec['argv'])
        self.assertEqual(result['run_id'], spec['run_id'])
        self.assertTrue(self.supervisors.last.started)
        self.assertEqual('running', self.backend.status()['state'])
        self.assertTrue(spec['output_dir'].endswith('data/runs/' + result['run_id']))
        self.assertTrue(spec['log_dir'].endswith('data/logs/console/' + result['run_id']))
        self.assertTrue((self.repo.root / 'data/logs/console' / result['run_id'] / 'run.json').is_file())

    def test_missing_changed_expired_specs_are_stale(self):
        self.assert_error(409, lambda: self.backend.run('env.check', {}), 'preview_stale')
        values, preview = self.prepare('nav.start', controller='mppi')
        changed = dict(values, controller='dwb')
        self.assert_error(409, lambda: self.backend.run('nav.start', changed,
                          spec_id=preview['spec_id']), 'preview_stale')
        self.assert_error(409, lambda: self.backend.run('map.start', self.defaults('map.start'),
                          spec_id=preview['spec_id']), 'preview_stale')
        self.clock.advance(601)
        self.assert_error(409, lambda: self.backend.run('nav.start', values,
                          spec_id=preview['spec_id']), 'preview_stale')
        self.assertFalse(self.supervisors.instances)

    def test_transport_change_between_preview_and_run_is_stale(self):
        values, preview = self.prepare('map.start')
        self.domain = 81
        self.assert_error(409, lambda: self.backend.run('map.start', values,
                          spec_id=preview['spec_id']), 'preview_stale')
        self.assertFalse(self.supervisors.instances)

    def test_no_manual_transport_available_refuses_as_interlock(self):
        self.backend.transport_selector = mock.Mock(
            side_effect=RuntimeError('No unoccupied console ROS domain remains in 80–99.'))
        values, preview = self.prepare('map.start')
        self.assertIn('80–99', preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('map.start', values,
                          spec_id=preview['spec_id']))
        self.assertFalse(self.supervisors.instances)

    def test_approval_edit_invalidates_prepared_spec(self):
        values, preview = self.prepare('env.check')
        self.approve_o2()
        self.assert_error(409, lambda: self.backend.run('env.check', values,
                          spec_id=preview['spec_id']), 'preview_stale')

    def test_new_session_and_jobs_refused_while_running(self):
        self.install_proposed_system()
        self.start('map.start')
        for action_id in ('map.start', 'map.explore', 'nav.start', 'env.check', 'env.build'):
            with self.subTest(action_id=action_id):
                values, preview = self.prepare(action_id)
                self.assertIn('Another session is running', preview['blocked_reason'])
                self.assert_error(409, lambda: self.backend.run(action_id, values,
                                  spec_id=preview['spec_id']))

    def test_mapping_is_not_controllable_before_readiness(self):
        with mock.patch('fixtures.FakeSupervisor.start', return_value=None):
            self.start('map.start')
        self.assertEqual('starting', self.backend.status()['state'])
        self.assert_error(409, lambda: self.backend.teleop(.1, 0))
        values, preview = self.prepare('map.save')
        self.assertTrue(preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('map.save', values,
                          spec_id=preview['spec_id']))

    def test_confirmation_must_match_exactly(self):
        actions = registry.actions(self.repo.root, ENVIRONMENT)
        action = next(item for item in actions if item['id'] == 'env.check')
        action['confirm_phrase'] = 'RUN CHECKS'
        with mock.patch('engine.registry.actions', return_value=actions):
            values, preview = self.prepare('env.check')
            for confirmation in (None, 'run checks', 'RUN CHECKS '):
                self.assert_error(422, lambda: self.backend.run('env.check', values,
                                  confirm_text=confirmation, spec_id=preview['spec_id']))
            result = self.backend.run('env.check', values,
                                      confirm_text='RUN CHECKS', spec_id=preview['spec_id'])
            self.assertTrue(result['run_id'])

    def test_external_lock_blocks_checks_and_build(self):
        self.install_proposed_system()
        self.external_lock = True
        for action_id in ('env.check', 'env.build', 'map.start', 'map.explore', 'nav.start'):
            with self.subTest(action_id=action_id):
                values, preview = self.prepare(action_id)
                self.assertIn('terminal simulation', preview['blocked_reason'])
                self.assert_error(409, lambda: self.backend.run(action_id, values,
                                  spec_id=preview['spec_id']))
        self.assertFalse(self.supervisors.instances)

    def test_foreign_inventory_blocks_all_session_preflight(self):
        self.install_proposed_system()
        self.simulation_processes = [{'pid': 43210, 'cmd': 'rviz2', 'owned': False}]
        for action_id in ('env.check', 'env.build', 'map.start', 'map.explore', 'nav.start'):
            with self.subTest(action_id=action_id):
                values, preview = self.prepare(action_id)
                self.assertIn('43210', preview['blocked_reason'])
                self.assert_error(409, lambda: self.backend.run(action_id, values,
                                  spec_id=preview['spec_id']))
        self.assertFalse(self.supervisors.instances)

    def test_unreadable_inventory_fails_closed(self):
        self.backend.inventory = mock.Mock(side_effect=PermissionError('hidden process'))
        values, preview = self.prepare('map.start')
        self.assertIn('could not be read', preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('map.start', values,
                          spec_id=preview['spec_id']))
        self.assertFalse(self.supervisors.instances)

    def test_preflight_is_rechecked_at_execution(self):
        for action_id in ('map.start', 'nav.start'):
            with self.subTest(action_id=action_id):
                values, preview = self.prepare(action_id)
                self.assertIsNone(preview['blocked_reason'])
                self.simulation_processes = [{'pid': 43210, 'cmd': 'map_server', 'owned': False}]
                error = self.assert_error(409, lambda: self.backend.run(action_id, values,
                                          spec_id=preview['spec_id']), 'interlock')
                self.assertIn('43210', error.reason)
                self.simulation_processes = []
        self.assertFalse(self.supervisors.instances)

    def test_operation_needs_matching_session_and_preserves_run_id(self):
        values, preview = self.prepare('map.save')
        self.assertTrue(preview['blocked_reason'])
        self.assert_error(409, lambda: self.backend.run('map.save', values,
                          spec_id=preview['spec_id']))
        result, _ = self.start('map.start')
        saved, preview = self.start('map.save', name='test_console_map')
        self.assertEqual(result['run_id'], saved['run_id'])
        self.assertEqual('operation', preview['kind'])
        self.assertNotIn('command', preview)
        self.assertEqual(1, len(self.supervisors.instances))

    def test_nav_goal_inside_map_and_goal_bounds(self):
        self.start('nav.start', map='tb3_sandbox')
        _, preview = self.prepare('nav.goal', x=0.0, y=0.0, yaw=1.2)
        self.assertEqual('operation', preview['kind'])
        for x, y in ((999, 0), (0, -999)):
            self.assert_error(422, lambda: self.prepare('nav.goal', x=x, y=y))

    def test_completed_save_result_is_durable_and_stop_does_not_replace_it(self):
        started, _ = self.start('map.start')
        saved, _ = self.start('map.save', name='test_console_map')
        log = self.repo.root / 'data/logs/console' / started['run_id'] / 'run.json'
        operation = json.loads(log.read_text())['operations'][0]
        self.assertEqual('map.save', operation['action_id'])
        self.assertTrue(operation['request_id'])
        self.assertTrue(operation['ok'])
        self.assertEqual({'ok': True}, operation['result'])
        self.assertIsNotNone(operation['end_at'])
        self.backend.stop()
        self.assertEqual(operation, json.loads(log.read_text())['operations'][0])

    def test_operation_control_error_is_saved_before_it_is_raised(self):
        started, _ = self.start('map.start')
        values, preview = self.prepare('map.save', name='failed_map')
        with mock.patch.object(self.supervisors.last, 'control',
                               side_effect=BrokenPipeError('worker pipe closed')):
            with self.assertRaises(BrokenPipeError):
                self.backend.run('map.save', values, spec_id=preview['spec_id'])
        log = self.repo.root / 'data/logs/console' / started['run_id'] / 'run.json'
        operation = json.loads(log.read_text())['operations'][0]
        self.assertFalse(operation['ok'])
        self.assertEqual('operation_send_failed', operation['error']['error'])
        self.assertIn('worker pipe closed', operation['error']['reason'])
        self.assertIsNotNone(operation['end_at'])
        self.assertFalse(self.backend.operation_busy)

    def test_late_operation_success_after_http_timeout_releases_busy_state(self):
        started, _ = self.start('map.start')
        self.supervisors.last.respond_to_operations = False
        values, preview = self.prepare('map.save', name='slow_map')
        with mock.patch('engine.threading.Event') as event_factory:
            event_factory.return_value.wait.return_value = False
            self.assert_error(500, lambda: self.backend.run('map.save', values,
                spec_id=preview['spec_id']), 'operation_timeout')
        self.assertTrue(self.backend.operation_busy)
        request_id = self.supervisors.last.controls[-1][1]['request_id']
        self.supervisors.last.complete_operation(request_id, result={'map_yaml': 'slow_map.yaml'})
        self.assertFalse(self.backend.operation_busy)
        self.assertNotIn(request_id, self.backend.pending)
        log = self.repo.root / 'data/logs/console' / started['run_id'] / 'run.json'
        operation = json.loads(log.read_text())['operations'][0]
        self.assertTrue(operation['ok'])
        self.assertIn('wait_timeout_at', operation)
        self.assertEqual({'map_yaml': 'slow_map.yaml'}, operation['result'])
        _, next_preview = self.prepare('map.save', name='next_map')
        self.assertIsNone(next_preview['blocked_reason'])

    def test_stop_records_pending_save_failure_and_unblocks_http_waiter(self):
        started, _ = self.start('map.start')
        self.supervisors.last.respond_to_operations = False
        values, preview = self.prepare('map.save', name='interrupted_map')
        errors = []

        def save():
            try:
                self.backend.run('map.save', values, spec_id=preview['spec_id'])
            except ConsoleError as exc:
                errors.append(exc)

        thread = threading.Thread(target=save, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not self.backend.operation_busy and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.backend.operation_busy)
            self.backend.stop()
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual('session_ended', errors[0].code)
        log = self.repo.root / 'data/logs/console' / started['run_id'] / 'run.json'
        operation = json.loads(log.read_text())['operations'][0]
        self.assertFalse(operation['ok'])
        self.assertEqual('session_ended', operation['error']['error'])
        self.assertIsNotNone(operation['end_at'])

    def test_only_one_session_operation_can_be_in_progress(self):
        self.start('map.start')
        supervisor = self.supervisors.last
        supervisor.respond_to_operations = False
        params, preview = self.prepare('map.save', name='first_map')
        failures = []

        def save():
            try:
                self.backend.run('map.save', params, spec_id=preview['spec_id'])
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=save, daemon=True)
        thread.start()
        request_id = None
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                operations = [data for kind, data in supervisor.controls if kind == 'operation']
                if operations:
                    request_id = operations[-1]['request_id']
                    break
                time.sleep(.01)
            self.assertIsNotNone(request_id, 'First operation did not reach its session worker.')
            values, blocked = self.prepare('map.save', name='second_map')
            self.assertTrue(blocked['blocked_reason'])
            self.assert_error(409, lambda: self.backend.run('map.save', values,
                              spec_id=blocked['spec_id']))
        finally:
            if request_id is not None:
                supervisor.complete_operation(request_id)
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)

    def test_map_name_and_no_overwrite(self):
        self.start('map.start')
        for name in ('../outside', 'xy', 'UPPERCASE', 'name;touch_x'):
            self.assert_error(422, lambda: self.prepare('map.save', name=name))
        run_id = self.backend.status()['session']['run_id']
        self.repo.write(f'data/runs/{run_id}/maps/test_console_map/test_console_map.pgm', 'old')
        self.assert_error(409, lambda: self.prepare('map.save', name='test_console_map'))

    def test_continue_is_refused_without_a_waiting_trial_batch(self):
        # No remaining action starts a trial batch, so Continue never reaches a session worker.
        self.assert_error(409, self.backend.continue_run, 'not_waiting')
        for action_id in ('map.start', 'nav.start'):
            with self.subTest(action_id=action_id):
                self.start(action_id)
                supervisor = self.supervisors.last
                self.assert_error(409, self.backend.continue_run, 'not_waiting')
                supervisor.on_event({'type': 'waiting', 'trial_index': 0, 'prompt': 'Press Enter to continue: '})
                self.assert_error(409, self.backend.continue_run, 'not_waiting')
                self.assertNotIn('continue', [kind for kind, _ in supervisor.controls])
                self.backend.stop()

    def test_teleop_only_running_mapping_and_clamped(self):
        self.assert_error(409, lambda: self.backend.teleop(0.1, 0))
        self.start('nav.start')
        self.assert_error(409, lambda: self.backend.teleop(0.1, 0))
        self.backend.stop()
        self.start('map.start')
        self.assertEqual({'ok': True}, self.backend.teleop(99, -99))
        kind, values = self.supervisors.last.controls[-1]
        self.assertEqual('teleop', kind)
        self.assertEqual(0.26, values['linear_x'])
        self.assertEqual(-1.82, values['angular_z'])
        self.assert_error(422, lambda: self.backend.teleop(float('nan'), 0))

    def test_clean_stop_returns_audit_and_idle(self):
        self.start('map.start')
        result = self.backend.stop()
        self.assertEqual(0, result['survivors'])
        self.assertFalse(result['forced'])
        self.assertFalse(result['bond_abort'])
        self.assertTrue(result['report'].startswith('data/logs/console/'))
        self.assertEqual('idle', self.backend.status()['state'])

    def test_forced_stop_sets_error_and_alarm(self):
        for action_id in ('map.start', 'nav.start'):
            with self.subTest(action_id=action_id):
                result, _ = self.start(action_id)
                self.supervisors.last.stop_report.update(forced=True, clean=False)
                self.backend.stop()
                self.assertEqual('error', self.backend.status()['state'])
                events = [event for _, event in self.backend.events.read(0, timeout=0)]
                self.assertTrue(any(event['type'] == 'alarm' for event in events))
                log = self.repo.root / 'data/logs/console' / result['run_id']
                self.assertTrue(json.loads((log / 'cleanup.json').read_text())['forced'])
                self.assertEqual('error', json.loads((log / 'run.json').read_text())['status'])
                # An error state needs a clean re-audit before the next session.
                _, blocked = self.prepare('map.start')
                self.assertIn('Another session is running', blocked['blocked_reason'])
                self.assertTrue(self.backend.stop()['report'].endswith('/cleanup_recheck.json'))
                self.assertEqual('idle', self.backend.status()['state'])

    def test_natural_cleanup_fault_sets_error_alarm_and_record(self):
        result, _ = self.start('nav.start')
        supervisor = self.supervisors.last
        cleanup = dict(supervisor.stop_report, forced=True, clean=False)
        supervisor.on_event({'type': 'finished', 'cleanup': cleanup, 'exit_code': 0})
        self.assertEqual('error', self.backend.status()['state'])
        self.assertIsNotNone(self.backend.status()['session'])
        alarms = [event for _, event in self.backend.events.read(0, timeout=0) if event['type'] == 'alarm']
        self.assertTrue(alarms)
        self.assertIn('cleanup.json', alarms[-1]['text'])
        log = self.repo.root / 'data/logs/console' / result['run_id']
        self.assertFalse(json.loads((log / 'cleanup.json').read_text())['clean'])
        record = json.loads((log / 'run.json').read_text())
        self.assertEqual('error', record['status'])
        self.assertTrue(record['cleanup']['forced'])

    def test_manual_shutdown_bond_abort_is_counted_once_per_session(self):
        self.start('nav.start')
        supervisor = self.supervisors.last
        cleanup = dict(supervisor.stop_report, bond_abort=True, bond_abort_count=1)
        supervisor.on_event({'type': 'finished', 'cleanup': cleanup, 'exit_code': 0})
        supervisor.on_event({'type': 'finished', 'cleanup': cleanup, 'exit_code': 0})
        status = self.backend.status()
        self.assertEqual('idle', status['state'])
        self.assertEqual(1, status['counters']['bond_aborts'])

    def test_survivors_need_clean_stop_before_starting_again(self):
        self.start('map.start')
        self.supervisors.last.stop_report.update(survivors=1, clean=False)
        self.simulation_processes = [{'pid': 43210, 'cmd': 'rviz2', 'owned': False}]
        self.backend.stop()
        self.assertEqual('error', self.backend.status()['state'])
        self.simulation_processes = []
        self.assertEqual(0, self.backend.stop()['survivors'])
        self.assertEqual('idle', self.backend.status()['state'])

    def test_file_allowlist_and_maximum_size(self):
        self.repo.write('data/runs/example/CHECKSHEET.md', 'hello')
        self.repo.write('data/benchmarks/batch/manifest.json', '{"runs": []}')
        self.repo.write('data/logs/console/example/cleanup.json', '{"survivors": 0}')
        self.repo.write('docs/archive/example.md', 'old docs record')
        self.assertEqual('hello', self.backend.file('data/runs/example/CHECKSHEET.md')['text'])
        self.assertIn('runs', self.backend.file('data/benchmarks/batch/manifest.json')['text'])
        self.assertIn('survivors', self.backend.file('data/logs/console/example/cleanup.json')['text'])
        self.repo.write('data/runs/example/image.png', 'not served')
        self.repo.write('data/runs/example/large.log', 'x' * (1024 * 1024 + 1))
        for path in ('data/runs/example/image.png', 'data/runs/example/large.log',
                     'docs/archive/example.md', 'data/environment.txt', 'data',
                     'src/amr_physical_ai/config/nav2_mppi.yaml', '../outside',
                     'data/runs/../../tools/amr_console/approvals.yaml',
                     'docs/../tools/amr_console/approvals.yaml'):
            with self.subTest(path=path), self.assertRaises(ConsoleError):
                self.backend.file(path)

    def test_symlink_escape_refused(self):
        outside = self.repo.write('outside.txt', 'outside secret')
        link = self.repo.root / 'data/runs/example/escape.txt'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        folder = self.repo.root / 'data/logs/console/escape_folder'
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.symlink_to(self.repo.root / 'src', target_is_directory=True)
        with mock.patch('engine.shutil.which', return_value='/usr/bin/xdg-open'), \
                mock.patch('engine.subprocess.Popen') as popen:
            for path in ('data/runs/example/escape.txt', 'data/logs/console/escape_folder'):
                with self.subTest(path=path):
                    self.assert_error(404, lambda: self.backend.file(path), 'bad_path')
                    self.assert_error(404, lambda: self.backend.open(path), 'bad_path')
            popen.assert_not_called()

    def test_open_images_and_directories_uses_http_child_not_session_worker(self):
        self.repo.write('data/runs/example/image.png', 'image fixture')
        with mock.patch('engine.shutil.which', return_value='/usr/bin/xdg-open'):
            with mock.patch('engine.subprocess.Popen') as popen:
                for path in ('data/runs/example/image.png', 'data/runs/example'):
                    result = self.backend.open(path)
                    self.assertTrue(result['ok'])
                self.assertEqual(2, popen.call_count)
                for call in popen.call_args_list:
                    self.assertIsInstance(call.args[0], list)
                    self.assertEqual('xdg-open', call.args[0][0])
                    self.assertTrue(call.kwargs['start_new_session'])
                    self.assertNotIn('shell', call.kwargs)
        self.assertFalse(self.supervisors.instances)

    def test_results_paths_are_repo_relative_and_kinds_stay_separate(self):
        self.repo.write('data/runs/20261027_100000_map_start_fixture/maps/office/office.yaml', 'image: office.pgm')
        self.repo.write('data/benchmarks/batch_20261027/manifest.json', '{"runs": []}')
        self.repo.write('docs/archive/visual/objective1/run_20261005_fixture/CHECKSHEET.md', 'Visual checks')
        self.repo.write('docs/archive/controllers/run_20261001_paired/summary.csv', 'historical')
        runs = self.backend.results()['runs']
        self.assertEqual({('data/runs/20261027_100000_map_start_fixture', 'console'),
                          ('data/benchmarks/batch_20261027', 'benchmark')},
                         {(run['path'], run['dataset_kind']) for run in runs})
        for run in runs:
            for file in run['files']:
                self.assertTrue(file['path'].startswith(run['path'] + '/'))
                self.assertEqual(file['path'], self.backend.file(file['path'])['path'])
        self.assertNotIn('docs/', str(runs))
