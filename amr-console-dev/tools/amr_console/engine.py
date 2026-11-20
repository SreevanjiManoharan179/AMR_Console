"""Console state, prepared specifications and backend-enforced interlocks."""
import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from client import WorkerClient
from errors import ConsoleError
from events import EventStream
from office_tasks import OfficeTasks
import paths
import registry
import runtime


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temporary.replace(path)


def grid_on_map(displays, center_x, center_y, half_extent):
    """Lay the RViz Grid display over the map instead of around the dock.

    The map frame's (0, 0) is the robot's start (the dock), so a grid centred there leaves part
    of an office outside it. The grid is centred on the map on whole metres (so its 1 m lines
    stay on whole map coordinates) and sized to cover the whole map. Display only: no frame,
    topic or navigation setting changes. Returns the displays text unchanged if it has no Grid.
    """
    grid_x, grid_y = math.floor(center_x + .5), math.floor(center_y + .5)
    shift = max(abs(center_x - grid_x), abs(center_y - grid_y))
    count = 2 * max(1, math.ceil(half_extent + shift - 1e-9))
    blocks = re.split(r'(?m)^(?=    - )', displays)
    for index, block in enumerate(blocks):
        if not re.search(r'(?m)^(    - |      )Class: rviz_default_plugins/Grid$', block):
            continue
        offset = f'      Offset:\n        X: {grid_x}\n        Y: {grid_y}\n        Z: 0\n'
        if re.search(r'(?m)^      Plane Cell Count: .+$', block):
            block = re.sub(r'(?m)^      Plane Cell Count: .+$', f'      Plane Cell Count: {count}', block, count=1)
        else:
            block = block.rstrip('\n') + f'\n      Plane Cell Count: {count}\n'
        if re.search(r'(?m)^      Offset:\n        X: .+\n        Y: .+\n        Z: .+\n', block):
            block = re.sub(r'(?m)^      Offset:\n        X: .+\n        Y: .+\n        Z: .+\n', offset, block, count=1)
        else:
            block = block.rstrip('\n') + '\n' + offset
        blocks[index] = block
        break
    return ''.join(blocks)


def private_rviz_copy(spec, root, target):
    """Frame the current arena in the console's disposable RViz view.

    Mapping sessions are centred on the arena's saved map. Sessions in an office
    world are centred on its exact map and zoomed out to fit it; other
    navigation sessions keep their template view unchanged. Whenever the view is
    framed, the Grid display is laid over the same map (grid_on_map).
    """
    text = Path(spec['rviz_source']).read_text()
    view_map = spec.get('view_map')
    if spec['action_id'] == 'map.start' or view_map:
        bounds = registry.map_bounds(root, view_map or 'tb3_sandbox')
        x, y, yaw = bounds['origin']
        dx = bounds['width'] * bounds['resolution'] / 2
        dy = bounds['height'] * bounds['resolution'] / 2
        center_x = x + math.cos(yaw)*dx - math.sin(yaw)*dy
        center_y = y + math.sin(yaw)*dx + math.cos(yaw)*dy
        displays, views = text.split('  Views:\n', 1)
        # Only these known templates are adjusted. The source file, map
        # data, scan topics, fixed frame and navigation configuration stay intact.
        if spec['action_id'] == 'map.start':
            displays = displays.replace('      Alpha: 0.7\n',
                                        '      Alpha: 1\n      Draw Behind: true\n', 1)
        displays = grid_on_map(displays, center_x, center_y, max(dx, dy))
        views = re.sub(r'(?m)^      X: .+$', f'      X: {center_x:.6g}', views, count=1)
        views = re.sub(r'(?m)^      Y: .+$', f'      Y: {center_y:.6g}', views, count=1)
        if spec.get('view_scale'):
            views = re.sub(r'(?m)^      Scale: .+$', f"      Scale: {spec['view_scale']}", views, count=1)
        text = displays + '  Views:\n' + views
    Path(target).write_text(text)


class ConsoleBackend:
    def __init__(self, root=runtime.ROOT, *, inventory=None, lock_probe=None,
                 clock=time.monotonic, wall_clock=time.time,
                 transport_selector=None, supervisor_factory=None, environment=None,
                 office_factory=None):
        self.root = Path(root).resolve()
        self.inventory = inventory or (lambda: runtime.inventory(self.root))
        self.lock_probe = lock_probe or (lambda: runtime.lock_held(self.root))
        self.clock, self.wall_clock = clock, wall_clock
        self.transport_selector = transport_selector or (lambda kind: runtime.choose_transport(kind, self.root))
        self.supervisor_factory = supervisor_factory or WorkerClient
        self.environment = dict(environment or runtime.environment(self.root))
        self.events = EventStream()
        self.lock = threading.RLock()
        self.state = 'idle'
        self.session = None
        self.supervisor = None
        self.last_run = None
        self.record = None
        self.specs = {}
        self.pending = {}
        self.operation_busy = False
        self.waiting = None
        self.stopping = False
        self.bond_aborts = 0
        self.bond_abort_seen = set()
        self.current_trial = None
        self.stopped_trial = None
        self.office_cancel_generation = 0
        self.office = (office_factory or OfficeTasks)(self)
        self._state_event()

    def status(self):
        with self.lock:
            session = dict(self.session, operation_running=self.operation_busy) if self.session else None
            office = self.office.brief() if getattr(self, 'office', None) else None
            return copy.deepcopy({'state': self.state, 'session': session,
                                  'environment': self.environment,
                                  'counters': {'bond_aborts': self.bond_aborts},
                                  'office': office})

    def _state_event(self):
        self.events.publish({'type': 'state', 'status': self.status()})

    def _set_operation_busy(self, busy):
        """Track the one session operation in progress; the page learns of each change at once."""
        changed = self.operation_busy != busy
        self.operation_busy = busy
        if changed and self.session:
            self._state_event()

    def actions(self):
        return {'actions': registry.actions(self.root, self.environment)}

    def _action(self, action_id):
        if not isinstance(action_id, str):
            raise ConsoleError(422, 'invalid_action', 'Action ID must be a string.')
        return registry.find_action(self.actions()['actions'], action_id)

    def _validate(self, action, params):
        bounds = None
        if action['id'] == 'nav.goal' and self.session and self.session.get('map'):
            bounds = registry.map_bounds(self.root, self.session['map'])
        normalized = registry.validate(action, params, map_bounds=bounds)
        if action['id'] == 'map.save' and self.session:
            name = normalized['name']
            stem = self.root / 'data/runs' / self.session['run_id'] / 'maps' / name / name
            if any(stem.with_suffix(suffix).exists() for suffix in ('.yaml', '.pgm')):
                raise ConsoleError(409, 'map_exists', 'That map name already has a YAML or PGM file in this session. Choose a new name.')
        return normalized

    def _preflight_reason(self):
        try:
            if self.lock_probe():
                return 'A terminal simulation holds log/simulation.lock; finish it first.'
            found = self.inventory()
            if found:
                return 'Simulation processes remain; none were signalled: ' + '; '.join(
                    f"PID {p.get('pid', '?')}: {p.get('cmd', p.get('comm', '?'))}" for p in found)
        except Exception as exc:
            return f'The full preflight could not be read; no session started: {exc}'
        return None

    def _blocked(self, action):
        if action.get('locked'):
            return action['lock_reason']
        required = action.get('requires_session')
        if required:
            if self.state != 'running' or not self.session or self.session['kind'] != required:
                return f'Start a running {required} session first.'
            if self.operation_busy:
                return 'Another save or navigation goal is still running; wait for its result or press STOP.'
            if action['id'] in ('nav.goal', 'commands.typed') and self.office.robot_busy():
                return 'An office task is using the robot. Cancel it on the Office tasks tab first.'
            if action['id'] == 'commands.typed' and not self.session.get('record_truth'):
                return ('Start a new navigation session on a world with approved places '
                        '(config/worlds.yaml); it must record Gazebo ground truth.')
            return None
        if self.state != 'idle':
            label = self.session['label'] if self.session else self.state
            return f'Another session is running ({label}). Press STOP first.'
        return self._preflight_reason()

    def _fingerprint(self):
        h = hashlib.sha256()
        patterns = ('tools/amr_console/*.py', 'tools/amr_console/API_CONTRACT.md',
                    'tools/amr_console/approvals.yaml', 'scripts/*.sh',
                    'src/amr_physical_ai/config/*.yaml', 'src/amr_physical_ai/maps/*',
                    'src/amr_physical_ai/worlds/*',
                    'src/amr_physical_ai/launch/*.py', 'src/amr_physical_ai/scripts/*.py',
                    'src/amr_physical_ai/amr_physical_ai/command_pipeline.py',
                    'src/amr_physical_ai/amr_physical_ai/explor*.py',
                    'src/amr_physical_ai/rviz/*.rviz')
        for pattern in patterns:
            for path in sorted(self.root.glob(pattern)):
                if path.is_file():
                    h.update(str(path.relative_to(self.root)).encode())
                    h.update(path.read_bytes())
        h.update(runtime.git_commit(self.root).encode())
        context = (self.state, self.session.get('run_id') if self.session else None)
        h.update(json.dumps(context).encode())
        return h.hexdigest()

    def preview(self, action_id, params):
        with self.lock:
            action = self._action(action_id)
            normalized = self._validate(action, params)
            reason = self._blocked(action)
            run_id = (self.session['run_id'] if action.get('requires_session') and self.session
                      else datetime.fromtimestamp(self.wall_clock(), ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S')
                      + '_' + action_id.replace('.', '_') + '_' + secrets.token_hex(4))
            env = {}
            if action['session']:
                try:
                    env = self.transport_selector(action['session'])
                except Exception as exc:
                    reason = reason or str(exc)
            elif action.get('requires_session') and self.session:
                env = {'ROS_DOMAIN_ID': str(self.session['ros_domain_id']),
                       'GZ_PARTITION': self.session['gz_partition']}
            context = dict(self.session or {}, root=str(self.root))
            spec = registry.build(action, normalized, run_id, env, session=context)
            spec.update(root=str(self.root), fingerprint=self._fingerprint(),
                        created=self.clock(), action=action)
            spec_id = 's_' + secrets.token_hex(16)
            self.specs[spec_id] = spec
            # Expiration and cap bound abandoned previews from UI refreshes.
            self.specs = {key: value for key, value in self.specs.items()
                          if self.clock() - value['created'] < 600}
            if len(self.specs) > 256:
                del self.specs[next(iter(self.specs))]
            result = {'kind': spec['preview_kind'], 'env': spec['env'],
                      'spec_id': spec_id, 'blocked_reason': reason}
            if spec['preview_kind'] == 'operation':
                result['operation'] = spec['operation']
            else:
                result['command'] = spec['command']
            return result

    def run(self, action_id, params, confirm_text=None, spec_id=None):
        with self.lock:
            action = self._action(action_id)
            normalized = self._validate(action, params)
            spec = self.specs.get(spec_id) if isinstance(spec_id, str) else None
            if (not spec or self.clock() - spec['created'] >= 600
                    or spec['action_id'] != action_id or spec['params'] != normalized
                    or spec['fingerprint'] != self._fingerprint()):
                raise ConsoleError(409, 'preview_stale', 'The prepared preview is missing, expired or changed. Request a fresh preview.')
            # Recheck output symlink boundaries as well as the content fingerprint.
            registry.build(action, normalized, spec['run_id'], spec['env'],
                           session=dict(self.session or {}, root=str(self.root)))
            reason = self._blocked(action)
            if reason:
                raise ConsoleError(409, 'interlock', reason)
            if action.get('confirm_phrase') and confirm_text != action['confirm_phrase']:
                raise ConsoleError(422, 'confirmation', 'Type ' + action['confirm_phrase'] + ' exactly.')
            if action.get('requires_session'):
                del self.specs[spec_id]
                self._set_operation_busy(True)
                request_id = secrets.token_hex(12)
                waiter = threading.Event()
                slot = {'event': waiter, 'response': None, 'record': self.record}
                self.pending[request_id] = slot
                if self.record is not None:
                    slot['operation'] = {'request_id': request_id, 'action_id': action_id,
                        'params': normalized, 'started_at': int(self.wall_clock()*1000),
                        'end_at': None, 'ok': None}
                    self.record.setdefault('operations', []).append(slot['operation'])
                    self._save_record()
                operation_params = dict(normalized)
                if spec.get('map_stem'):
                    operation_params['map_stem'] = spec['map_stem']
                try:
                    self.supervisor.control('operation', action_id=action_id,
                                            params=operation_params, request_id=request_id)
                except Exception as exc:
                    self._complete_pending(slot, {'ok': False, 'error': {
                        'error': 'operation_send_failed', 'reason': str(exc)}})
                    self._set_operation_busy(False)
                    self.pending.pop(request_id, None)
                    raise
                run_id = self.session['run_id']
            else:
                if action['session'] in ('mapping', 'navigation'):
                    try:
                        current_transport = self.transport_selector(action['session'])
                    except Exception as exc:
                        raise ConsoleError(409, 'preview_stale',
                            f'The prepared transport is no longer available: {exc}. Request a fresh preview.') from exc
                    if current_transport != spec['env']:
                        raise ConsoleError(409, 'preview_stale', 'The prepared transport is occupied. Request a fresh preview.')
                # No preview creates output files. Refuse collisions at execution.
                log_dir = Path(spec['log_dir'])
                if log_dir.exists() or (spec.get('output_dir') and Path(spec['output_dir']).exists()):
                    raise ConsoleError(409, 'preview_stale', 'A prepared output folder already exists. Request a fresh preview.')
                log_dir.mkdir(parents=True, exist_ok=False)
                if spec.get('rviz_source'):
                    private_rviz_copy(spec, self.root, log_dir / 'rviz_config.rviz')
                self.state = 'starting'
                self.stopping = False
                self.waiting = None
                self.current_trial = None
                self.stopped_trial = None
                self.last_run = spec['run_id']
                map_id = spec.get('map_id')
                self.session = {'kind': spec['kind'], 'action_id': action_id,
                    'label': action['label'], 'started_at': int(self.wall_clock()*1000),
                    'ros_domain_id': int(spec['env']['ROS_DOMAIN_ID']) if 'ROS_DOMAIN_ID' in spec['env'] else None,
                    'gz_partition': spec['env'].get('GZ_PARTITION'), 'teleop': bool(action['teleop']),
                    'pid_group': None, 'run_id': self.last_run, 'map': map_id,
                    'record_truth': spec.get('record_truth', False)}
                self.record = {'run_id': self.last_run, 'action_id': action_id, 'params': normalized,
                    'argv': spec['argv'], 'sequence': spec.get('sequence', []), 'env': spec['env'],
                    'started_at': self.session['started_at'], 'end_at': None, 'exit_code': None,
                    'status': 'starting', 'trials': [], 'log_dir': str(log_dir),
                    'output_dir': spec.get('output_dir'), 'commit': self.environment['commit']}
                self._save_record()
                del self.specs[spec_id]
                self.supervisor = self.supervisor_factory(spec, self._worker_event)
                self._state_event()
                try:
                    self.supervisor.start()
                except Exception as exc:
                    self.state = 'error'
                    self.record['error'] = str(exc)
                    self._save_record()
                    self._state_event()
                    raise ConsoleError(500, 'supervisor_start', f'Supervisor could not start: {exc}; inspect {log_dir}.') from exc
                return {'run_id': self.last_run}
        # Operations keep the parent session; waiting here does not lock status,
        # STOP, SSE or the 300ms deadman in the supervisor.
        if not waiter.wait(360):
            with self.lock:
                slot['abandoned'] = True
                if slot.get('operation'):
                    slot['operation']['wait_timeout_at'] = int(self.wall_clock()*1000)
                    save_json(Path(slot['record']['log_dir']) / 'run.json', slot['record'])
                if slot['response'] is not None:
                    self.pending.pop(request_id, None)
                    self._set_operation_busy(False)
            raise ConsoleError(500, 'operation_timeout', 'The operation has not completed. Press STOP and inspect the session log.')
        with self.lock:
            response = slot['response'] or {}
            self.pending.pop(request_id, None)
            self._set_operation_busy(False)
            if not response.get('ok'):
                error = response.get('error', {})
                if isinstance(error, str):
                    error = {'reason': error}
                raise ConsoleError(error.get('status', 500), error.get('error', 'operation_failed'),
                                   error.get('reason', 'The operation failed; inspect the session log.'))
            return {'run_id': run_id, **response.get('result', {})}

    def _save_record(self):
        if self.record:
            save_json(Path(self.record['log_dir']) / 'run.json', self.record)

    def _complete_pending(self, slot, response):
        # A late STOP must not replace a completed save/goal response. Keep the
        # result in its original run even if the next session has already begun.
        if slot['response'] is not None:
            return
        slot['response'] = response
        if slot.get('operation'):
            slot['operation'].update(end_at=int(self.wall_clock()*1000),
                ok=bool(response.get('ok')),
                result=copy.deepcopy(response.get('result')),
                error=copy.deepcopy(response.get('error')))
            save_json(Path(slot['record']['log_dir']) / 'run.json', slot['record'])
        slot['event'].set()

    def _worker_event(self, event):
        with self.lock:
            type_ = event.get('type')
            if type_ == 'ready':
                if self.session:
                    self.session['pid_group'] = event.get('pid_group')
                    self.state = 'stopping' if self.stopping else 'running'
                    self.record['status'] = self.state
                    self._save_record()
                    self._state_event()
                return
            if type_ == 'operation_result':
                slot = self.pending.get(event.get('request_id'))
                if slot:
                    self._complete_pending(slot, event)
                    if slot.get('operation') and slot.get('abandoned'):
                        self.pending.pop(event['request_id'], None)
                        self._set_operation_busy(False)
                return
            if type_ == 'waiting':
                if not self.stopping:
                    self.waiting = copy.deepcopy(event)
                    self.events.publish(event)
                return
            if type_ == 'trial':
                event = dict(event)
                self.waiting = None
                self.current_trial = event.get('index')
                if self.stopping and event.get('index') == self.stopped_trial:
                    event['outcome'] = 'stopped'
                    event['detail'] = 'User pressed STOP; excluded from pass/fail counts.'
                if self.session and event.get('ros_domain_id') is not None:
                    self.session['ros_domain_id'] = int(event['ros_domain_id'])
                    self.session['gz_partition'] = event.get('gz_partition')
                    self._state_event()
                if self.record:
                    self.record['trials'].append(event)
                    if event.get('bond_abort'):
                        key = (self.last_run, event.get('index'))
                        if key not in self.bond_abort_seen:
                            self.bond_abort_seen.add(key)
                            self.bond_aborts += 1
                    self._save_record()
            if type_ == 'finished':
                cleanup = event.get('cleanup', {})
                cleanup = dict(cleanup) if isinstance(cleanup, dict) else {'clean': False}
                cleanup.setdefault('report', f'data/logs/console/{self.last_run}/cleanup.json')
                if cleanup.get('bond_abort') and self.record and self.record['action_id'] not in ('trials.visual', 'trials.one'):
                    key = (self.last_run, 'manual_shutdown')
                    if key not in self.bond_abort_seen:
                        self.bond_abort_seen.add(key)
                        self.bond_aborts += int(cleanup.get('bond_abort_count', 1))
                if self.record:
                    save_json(Path(self.record['log_dir']) / 'cleanup.json', cleanup)
                    self.record.update(cleanup=cleanup, end_at=int(self.wall_clock()*1000),
                        exit_code=event.get('exit_code', cleanup.get('exit_code')))
                count = cleanup.get('survivors', 0)
                count = len(count) if isinstance(count, list) else count
                okay = not count and not cleanup.get('forced') and cleanup.get('clean', False)
                if not okay and not self.stopping and self.record and self.record['trials']:
                    latest = self.record['trials'][-1]
                    if latest.get('outcome') != 'harness':
                        self._worker_event(dict(latest, outcome='harness',
                            detail='The session teardown or harness failed; inspect ' + cleanup['report']))
                self.state = 'idle' if okay else 'error'
                if self.record:
                    self.record['status'] = 'stopped' if self.stopping and okay else self.state
                    self._save_record()
                for slot in self.pending.values():
                    self._complete_pending(slot, {'ok': False, 'error': {'status': 409,
                        'error': 'session_ended',
                        'reason': 'The session ended before the operation finished.'}})
                self._set_operation_busy(False)
                self.session = None if okay else self.session
                self.waiting = None
                self._state_event()
                # The office conversation ends with the session (outside this lock).
                threading.Thread(target=self.office.on_session_end, daemon=True).start()
                if not okay:
                    self.events.publish({'type': 'alarm', 'level': 'alarm',
                        'text': 'Cleanup or execution failed; inspect ' + cleanup['report'] + ' and press STOP to audit again.',
                        'ts': int(self.wall_clock()*1000)})
                return
            if type_ in ('log', 'alarm'):
                event = dict(event, ts=event.get('ts', int(self.wall_clock()*1000)))
            if type_ in ('log', 'alarm', 'progress', 'trial'):
                self.events.publish(event)

    def continue_run(self):
        with self.lock:
            if self.state != 'running' or not self.waiting or not self.session or self.session['kind'] != 'trials':
                raise ConsoleError(409, 'not_waiting', 'No trial batch is waiting for Continue.')
            self.supervisor.control('continue')
            self.waiting = None
            return {'ok': True}

    def teleop(self, linear_x, angular_z):
        with self.lock:
            if self.state != 'running' or not self.session or not self.session['teleop'] or self.session['kind'] != 'mapping':
                raise ConsoleError(409, 'teleop_unavailable', 'The drive pad requires a running mapping session.')
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (linear_x, angular_z)):
                raise ConsoleError(422, 'velocity', 'Velocity values must be finite numbers.')
            linear_x = max(-.26, min(.26, linear_x))
            angular_z = max(-1.82, min(1.82, angular_z))
            self.supervisor.control('teleop', linear_x=linear_x, angular_z=angular_z)
            return {'ok': True}

    def stop(self):
        # The office conversation hears STOP at once, in its own thread, so STOP never waits for
        # the office lock before it reaches the robot; the session's ordered cleanup follows.
        threading.Thread(target=self.office.on_session_stop, daemon=True).start()
        with self.lock:
            active = self.supervisor if self.state in ('starting', 'running', 'stopping') else None
            if self.supervisor and getattr(self.supervisor, 'proc', None) is not None:
                if self.supervisor.proc.poll() is None:
                    active = self.supervisor
            if active:
                self.stopping = True
                self.state = 'stopping'
                self.waiting = None
                if self.current_trial is not None:
                    self.stopped_trial = self.current_trial
                    self._worker_event({'type': 'trial', 'index': self.current_trial,
                        'outcome': 'stopped', 'navigation_outcome': None,
                        'detail': 'User pressed STOP; excluded from pass/fail counts.'})
                self._state_event()
        if active:
            try:
                cleanup = active.stop()
            except Exception as exc:
                with self.lock:
                    self.state = 'error'
                    self._state_event()
                raise ConsoleError(500, 'cleanup_failed', f'Cleanup failed: {exc}; inspect data/logs/console/{self.last_run}.') from exc
            # Fake supervisors may only return a result; real workers emit it.
            with self.lock:
                if self.state == 'stopping':
                    self._worker_event({'type': 'finished', 'cleanup': cleanup,
                                        'exit_code': cleanup.get('exit_code', 0)})
            return {key: cleanup.get(key, False if key in ('forced', 'bond_abort') else 0)
                    for key in ('survivors', 'forced', 'bond_abort', 'report')}
        with self.lock:
            try:
                found = self.inventory()
                locked = self.lock_probe()
            except Exception as exc:
                raise ConsoleError(500, 'audit_failed', f'Cleanup inventory could not be read: {exc}') from exc
            report = {'survivors': len(found), 'processes': found, 'forced': False,
                      'bond_abort': False, 'clean': not found and not locked,
                      'simulation_lock_held': locked,
                      'report': f'data/logs/console/{self.last_run}/cleanup_recheck.json' if self.last_run else None}
            if self.record:
                save_json(Path(self.record['log_dir']) / 'cleanup_recheck.json', report)
            self.state = 'idle' if report['clean'] else 'error'
            if report['clean']:
                self.session = None
            else:
                self.events.publish({'type': 'alarm', 'level': 'alarm',
                    'text': 'Unowned simulation processes or a terminal harness remain; none were signalled.',
                    'ts': int(self.wall_clock()*1000)})
            self._state_event()
            return report

    # -- office tasks ---------------------------------------------------------------------
    def office_navigate(self, pose, *, map_id, job_id, nav_id, purpose=None, timeout=300, expected_generation=None):
        """Ask the navigation session to drive to an approved office stop; wait for its record.

        expected_generation: the Cancel number when the office task asked for this drive; any
        Cancel since then refuses it here, before anything is sent to the session.
        """
        with self.lock:
            if expected_generation is not None and expected_generation != self.office_cancel_generation:
                raise ConsoleError(409, 'office_cancelled', 'The office task was cancelled before this drive started.')
            session = self.session or {}
            if self.state != 'running' or session.get('kind') != 'navigation' or self.stopping:
                raise ConsoleError(409, 'office_no_session', 'No running navigation session.')
            if session.get('map') != map_id or not session.get('record_truth'):
                raise ConsoleError(409, 'office_session', 'The session is not on the office map with ground truth on.')
            if self.operation_busy:
                raise ConsoleError(409, 'office_busy', 'Another goal is still running.')
            tags = registry.COMMAND_MAPS[map_id].get('tags')
            import yaml
            tag_data = yaml.safe_load((self.root / tags).read_text())
            map_yaml = str(((self.root / tags).parent / tag_data['map_yaml']).resolve().relative_to(self.root))
            params = {'x': float(pose['x']), 'y': float(pose['y']), 'yaw': float(pose['yaw']),
                      'place': pose['key'], 'label': pose['label'], 'job_id': job_id, 'nav_id': nav_id,
                      'purpose': purpose, 'map_yaml': map_yaml,
                      'minimum_clearance_m': float(tag_data['minimum_clearance_m']),
                      'cancel_generation': self.office_cancel_generation}
            self._set_operation_busy(True)
            request_id = secrets.token_hex(12)
            slot = {'event': threading.Event(), 'response': None, 'record': self.record}
            self.pending[request_id] = slot
            if self.record is not None:
                slot['operation'] = {'request_id': request_id, 'action_id': 'office.navigate', 'params': params,
                                     'started_at': int(self.wall_clock()*1000), 'end_at': None, 'ok': None}
                self.record.setdefault('operations', []).append(slot['operation'])
                self._save_record()
            try:
                self.supervisor.control('operation', action_id='office.navigate', params=params,
                                        request_id=request_id)
            except Exception as exc:
                self.pending.pop(request_id, None)
                self._set_operation_busy(False)
                raise ConsoleError(500, 'operation_send_failed', str(exc)) from exc
        if not slot['event'].wait(timeout):
            with self.lock:
                slot['abandoned'] = True
                if slot['response'] is not None:
                    self.pending.pop(request_id, None)
                    self._set_operation_busy(False)
            raise ConsoleError(500, 'operation_timeout', 'The office drive has not finished. Press STOP.')
        with self.lock:
            response = slot['response'] or {}
            self.pending.pop(request_id, None)
            self._set_operation_busy(False)
        if not response.get('ok'):
            error = response.get('error', {})
            reason = error.get('reason') if isinstance(error, dict) else str(error)
            raise ConsoleError(409, 'office_drive_failed', reason or 'The drive could not be started.')
        return response.get('result', {})

    def office_cancel_goal(self):
        """Stop the current office drive (the session keeps running). No-op without a session."""
        with self.lock:
            self.office_cancel_generation += 1
            if self.state == 'running' and self.supervisor is not None and (self.session or {}).get('kind') == 'navigation':
                self.supervisor.control('office_cancel', generation=self.office_cancel_generation)
                return True
            return False

    def file(self, path):
        return paths.file_response(self.root, path)

    def arrange(self):
        if not self.environment.get('arrange_supported'):
            return {'supported': False, 'reason': self.environment.get('arrange_reason', 'Window arranging is unavailable.')}
        with self.lock:
            if self.state != 'running' or not self.session or self.session['kind'] == 'job':
                raise ConsoleError(409, 'windows_not_ready', 'Start a session with both Gazebo and RViz windows open.')
            request_id = secrets.token_hex(12)
            slot = {'event': threading.Event(), 'response': None}
            self.pending[request_id] = slot
            self.supervisor.control('arrange', request_id=request_id)
        if not slot['event'].wait(30):
            raise ConsoleError(500, 'arrange_timeout', 'The owned-window request timed out; inspect the session log.')
        with self.lock:
            self.pending.pop(request_id, None)
            response = slot['response'] or {}
        if not response.get('ok'):
            raise ConsoleError(409, 'windows_not_ready', str(response.get('error', 'Owned windows are not ready.')))
        return response['result']

    def results(self):
        return paths.list_results(self.root)

    def open(self, path):
        target = paths.resolve_allowed(self.root, path)
        if not shutil.which('xdg-open'):
            raise ConsoleError(500, 'open_unavailable', 'xdg-open is unavailable; open the approved path manually.')
        # This HTTP child is outside the dedicated worker's ownership scope.
        subprocess.Popen(['xdg-open', str(target)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {'ok': True, 'path': str(target.relative_to(self.root))}
