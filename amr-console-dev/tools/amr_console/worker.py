#!/usr/bin/env python3
"""One console session's private child/subreaper scope and JSONL controls."""
from __future__ import annotations

import argparse
import codecs
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

try:
    from .supervision import (ROOT, OwnedLaunch, enable_subreaper, full_inventory,
                              finish_tracking, stop_owned, PromptDetector, ContinueGate)
    from .ros_adapter import RosSession
except ImportError:
    from supervision import (ROOT, OwnedLaunch, enable_subreaper, full_inventory,
                             finish_tracking, stop_owned, PromptDetector, ContinueGate)
    from ros_adapter import RosSession

from shutdown_records import SHUTDOWN_MARKER, classify_shutdown, selected_crash_bond_evidence
from rviz_windows import close_rviz
import rviz_windows as window_helpers
from transport_isolation import transport_snapshot, transport_collisions, verify_transport
from gazebo_shutdown import stop_gazebo
from windows import save_private_rviz


def settle_after_halt(adapter, *, timeout=3.0, quiet=1.5, clock=time.monotonic, sleep=time.sleep):
    """Wait for a cancelled goal's result, then a quiet period, before the world pauses."""
    started = clock()
    had_goal = getattr(adapter, 'active_goal', None) is not None
    while getattr(adapter, 'active_goal', None) is not None and clock() - started < timeout:
        sleep(.05)
    released = getattr(adapter, 'active_goal', None) is None
    if had_goal:
        sleep(quiet)
    return {'had_goal': had_goal, 'goal_released': released,
            'waited_s': round(clock() - started, 3), 'quiet_s': quiet if had_goal else 0.0,
            'timeout_s': timeout}


def trial_record_event(record, audit, transport, *, index, identity, stopped=False):
    """Navigation success never hides a failed authoritative trial audit."""
    navigation = record.get('outcome')
    outcome = 'pass' if navigation == 'SUCCEEDED' else 'fail'
    if (not record.get('isolation_pass') or not record.get('continue_allowed')
            or record.get('error') or record.get('survivors')):
        outcome = 'harness'
    if stopped:
        outcome = 'stopped'
    proven = bool(audit and audit.get('allowed_exception') and audit.get('bond_abort')
                  and audit.get('isolation_pass') and not audit.get('survivors'))
    return {'index': index, 'outcome': outcome, 'navigation_outcome': navigation,
            'time_s': record.get('time_s') if record.get('time_s') is not None else record.get('wall_time_s'),
            'detail': record.get('error') or f'{record.get("controller")}/{record.get("tier")}/{record.get("goal_id")}',
            'bond_abort': proven, 'trial_dir': identity,
            'ros_domain_id': int(transport['ROS_DOMAIN_ID']),
            'gz_partition': transport['GZ_PARTITION']}


def progress_items(params):
    controller = params.get('controller', 'mppi')
    controllers = ['mppi', 'dwb', 'rpp'] if controller == 'all' else [controller]
    return [{'controller': name, 'tier': tier, 'goal': f'{tier}_{ordinal:02d}', 'state': 'pending'}
            for name in controllers
            for tier, count in [('medium', params.get('medium', 1)), ('hard', params.get('hard', 0))]
            for ordinal in range(1, count+1)]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def forced_shutdown_evidence(session_log, trial_output=None):
    """Read forced steps from this console log and its runner-owned records."""
    events, errors = [], []
    sources = [(Path(session_log), None)]
    if trial_output is not None:
        output = Path(trial_output).resolve()
        sources += [(path, output) for pattern in ('*/*/trial_*/launch.log',
                                                   '*/*/trial_*/process_cleanup.json')
                    for path in sorted(output.glob(pattern))]
    pattern = re.compile(r'\bescalating to\s+[\'"]?(SIGTERM|SIGKILL)\b', re.IGNORECASE)
    for path, boundary in sources:
        try:
            if boundary is not None and not path.resolve().is_relative_to(boundary):
                raise ValueError('Runner cleanup source escapes the current output folder.')
            try:
                label = path.relative_to(ROOT).as_posix()
            except ValueError:
                label = str(path)
            text = path.read_text(errors='replace')
            if path.name == 'process_cleanup.json':
                cleanup = json.loads(text)
                if not isinstance(cleanup, dict) or not isinstance(cleanup.get('signals'), list):
                    raise ValueError('Runner process cleanup lacks its signal audit.')
                for index, item in enumerate(cleanup['signals']):
                    if isinstance(item, dict) and item.get('signal') in ('SIGTERM', 'SIGKILL'):
                        events.append({'path': label, 'signal_index': index,
                            'signal': item['signal'], 'pid': item.get('pid'),
                            'source': 'runner_owned_signal'})
            else:
                for number, line in enumerate(text.splitlines(), 1):
                    match = pattern.search(line)
                    if match:
                        events.append({'path': label, 'line_number': number,
                            'signal': match.group(1).upper(), 'line': line,
                            'source': 'launcher_escalation'})
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({'path': str(path), 'error': str(exc)})
    return {'events': events, 'read_errors': errors}


class SessionWorker:
    def __init__(self, spec):
        self.spec = spec
        self.kind = spec['kind']
        self.trials = self.kind == 'trials'
        self.log_dir = Path(spec['log_dir'])
        self.output_dir = Path(spec['output_dir']) if spec.get('output_dir') else self.log_dir
        self.controls = queue.Queue()
        self.stop_requested = threading.Event()
        self.emission_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.owned = self.adapter = self.collector = None
        self.continue_gate = ContinueGate()
        self.detector = PromptDetector()
        self.operation_busy = False
        self.ready = False
        self.readers = []
        self.current_trial = None
        self.seen_transport = set()
        self.seen_records = set()
        self.bond_abort_trials = set()
        self.run_records = []
        self.setup_error = None
        self.collector_code = None
        self.requested_stop = False
        self.setup_thread = None
        self.operation_threads = []
        # Office tasks: Cancel stops the current drive but keeps the session; each Cancel
        # carries a generation so a drive requested before it can never start after it.
        self.office_cancel = threading.Event()
        self.office_cancel_generation = 0
        # Checking the generation and clearing the event happen together, so a Cancel that
        # arrives while a drive is being set up can never be wiped out by that drive.
        self.office_lock = threading.Lock()

    def emit(self, kind, **fields):
        event = {'type': kind, **fields}
        with self.emission_lock:
            try:
                sys.stdout.write(json.dumps(event, allow_nan=False) + '\n')
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                # The private owner must finish its cleanup even if the HTTP
                # process died and no longer reads the protocol pipe.
                self.requested_stop = True
                self.stop_requested.set()

    def log(self, text, level='info'):
        self.emit('log', text=text, level=level, ts=int(time.time()*1000))

    def _append_log(self, text):
        with self.log_lock:
            self.combined_log.write(text)
            self.combined_log.flush()

    def read_output(self, stream, level):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        pending = ''
        try:
            while True:
                data = os.read(stream.fileno(), 4096)
                if not data:
                    break
                text = decoder.decode(data)
                self._append_log(text)
                if self.trials and level == 'info':
                    for prompt in self.detector.feed(text):
                        # Serialize prompts with record polling. Otherwise a
                        # late final-trial event can erase a just-emitted
                        # between-controller waiting state in the frontend.
                        self.controls.put({'type': '_waiting', 'prompt': prompt})
                pending += text
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    self.log(line, level)
            pending += decoder.decode(b'', final=True)
            if pending:
                self.log(pending, level)
        except (OSError, ValueError) as exc:
            self.log(f'Output reader: {exc}', 'warn')

    def start_readers(self, proc):
        for stream, level in ((proc.stdout, 'info'), (proc.stderr, 'warn')):
            thread = threading.Thread(target=self.read_output, args=(stream, level), daemon=True)
            self.readers.append(thread)
            thread.start()

    def read_controls(self):
        for line in sys.stdin:
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('A control must be a JSON object.')
                self.controls.put(value)
            except ValueError as exc:
                self.log(f'Invalid worker control: {exc}', 'warn')
        # Loss of the HTTP supervisor must not leave a session running.
        self.controls.put({'type': 'stop'})

    def setup_ros(self):
        try:
            params = self.spec.get('params', {})
            self.adapter = RosSession(self.kind, self.stop_requested,
                                      controller=params.get('controller', 'mppi'),
                                      record_truth=self.spec.get('record_truth', False),
                                      explore=bool(self.spec.get('explore')),
                                      notify=lambda text: self.log(text, 'warn'))
            bounds = self.adapter.readiness()
            transport = verify_transport(self.owned, self.environment)
            write_json(self.log_dir / 'transport_verification.json', transport)
            if not transport['ok']:
                raise RuntimeError('A manual-session launch did not inherit its transport pair.')
            foreign = [item for item in full_inventory(self.owned) if not item['owned']]
            if foreign:
                raise RuntimeError('Unowned simulation processes appeared during manual startup.')
            if self.stop_requested.is_set():
                return
            self.ready = True
            self.emit_ready(map=bounds)
            if self.spec.get('stage1_argv') and not self.stop_requested.is_set():
                self.collector = subprocess.Popen(self.spec['stage1_argv'], cwd=ROOT,
                    env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, start_new_session=True, bufsize=0)
                self.start_readers(self.collector)
        except Exception as exc:
            if not self.stop_requested.is_set():
                self.setup_error = str(exc)
                self.log('Manual session readiness failed: ' + str(exc), 'alarm')
            self.stop_requested.set()

    def emit_ready(self, **extra):
        self.emit('ready', pid_group=self.owned.proc.pid,
            ros_domain_id=int(self.environment['ROS_DOMAIN_ID']) if not self.trials and self.kind != 'job' else None,
            gz_partition=self.environment.get('GZ_PARTITION') if not self.trials and self.kind != 'job' else None,
            **extra)

    def operation(self, message):
        request_id = message.get('request_id')
        if self.operation_busy:
            self.emit('operation_result', request_id=request_id, ok=False,
                      error='Another session operation is still running.')
            return
        if not self.ready or not self.adapter:
            self.emit('operation_result', request_id=request_id, ok=False,
                      error='The manual session is not ready.')
            return
        self.operation_busy = True
        def run():
            try:
                action = message.get('action_id')
                params = message.get('params', {})
                if action == 'map.save' and self.kind == 'mapping':
                    name = params.get('name')
                    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9_]{3,40}', name):
                        raise ValueError('Map names need 3–40 lowercase letters, digits or underscores.')
                    target = self.output_dir / 'maps' / name / name
                    result = self.adapter.save_map(target)
                elif action == 'nav.goal' and self.kind == 'navigation':
                    if self.spec.get('stage1_argv'):
                        raise RuntimeError('The scripted trial collector owns navigation goals until this session finishes.')
                    result = self.adapter.navigate(params)
                elif action == 'commands.typed' and self.kind == 'navigation':
                    if self.spec.get('stage1_argv') or not self.spec.get('record_truth'):
                        raise RuntimeError('Start navigation on a map with approved named places, with ground-truth recording on, first.')
                    from typed_commands import command
                    # Backend-generated request IDs only; never use user text as a path.
                    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{24}', request_id):
                        raise ValueError('Invalid operation request ID.')
                    target = self.output_dir / 'commands' / (request_id + '.json')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('x') as output:
                        # The session's map decides the places, approval key and grounding
                        # threshold; sessions without a profile are traversal sessions.
                        result = command(ROOT, self.adapter, params.get('text'),
                                         profile=self.spec.get('command_profile'))
                        result['transport'] = {key: self.environment[key]
                                               for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION')}
                        output.write(json.dumps(result, indent=2, allow_nan=False) + '\n')
                    result['record_path'] = str(target.relative_to(ROOT))
                    reason = (result.get('error') or result.get('evidence_error')
                              or result['validation'].get('question') or result['nav2']['status'])
                    self.log(f"Typed command — {result['parser']} — {reason}; record: {result['record_path']}",
                             'info' if result['nav2']['status'] == 'SUCCEEDED' else 'warn')
                elif action == 'office.navigate' and self.kind == 'navigation':
                    result = self.office_leg(request_id, params)
                else:
                    raise ValueError('This operation does not match the running session.')
                self.emit('operation_result', request_id=request_id, ok=True, result=result)
            except Exception as exc:
                self.emit('operation_result', request_id=request_id, ok=False, error=str(exc))
            finally:
                self.operation_busy = False
        thread = threading.Thread(target=run, daemon=True)
        self.operation_threads.append(thread)
        thread.start()

    def office_leg(self, request_id, params):
        """One office-task drive to an approved stop, recorded under the run's office folder."""
        if self.spec.get('stage1_argv') or not self.spec.get('record_truth'):
            raise RuntimeError('Office tasks need a navigation session with ground-truth recording on.')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{24}', request_id):
            raise ValueError('Invalid operation request ID.')
        job_id, nav_id = params.get('job_id'), params.get('nav_id')
        if not (isinstance(job_id, str) and re.fullmatch(r'job_[a-f0-9]{12}', job_id)
                and isinstance(nav_id, str) and re.fullmatch(r'[a-f0-9]{32}', nav_id)):
            raise ValueError('Invalid office task or drive ID.')
        generation = params.get('cancel_generation')
        with self.office_lock:
            if type(generation) is not int or generation < self.office_cancel_generation:
                raise InterruptedError('The office task was cancelled before this drive started.')
            self.office_cancel.clear()
        from office_navigation import navigate_leg
        target = self.output_dir / 'office' / job_id / (nav_id + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x') as output:
            result = navigate_leg(ROOT, self.adapter, params, self.office_cancel)
            result['transport'] = {key: self.environment[key] for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION')}
            output.write(json.dumps(result, indent=2, allow_nan=False) + '\n')
        result['record_path'] = str(target.relative_to(ROOT))
        self.log(f"Office task drive to {result.get('label')}: {result['nav2']['status']}; "
                 f"record: {result['record_path']}",
                 'info' if result['nav2']['status'] == 'SUCCEEDED' else 'warn')
        return result

    def handle_control(self, message):
        kind = message.get('type')
        if kind == 'office_cancel':
            generation = message.get('generation')
            with self.office_lock:
                if type(generation) is int:
                    self.office_cancel_generation = max(self.office_cancel_generation, generation)
                self.office_cancel.set()
            return
        if kind == 'stop':
            self.requested_stop = True
            self.stop_requested.set()
        elif kind == '_waiting':
            self.watch_trials()
            self.continue_gate.observe()
            self.emit('waiting', trial_index=(self.current_trial or {}).get('index'), prompt=message['prompt'])
        elif kind == 'continue':
            try:
                if not self.trials or self.owned.proc.poll() is not None:
                    raise RuntimeError('No trial batch is waiting for Continue.')
                self.continue_gate.send(self.owned.proc.stdin)
                self.emit('continued', ok=True, request_id=message.get('request_id'))
            except (RuntimeError, OSError) as exc:
                self.emit('continued', ok=False, request_id=message.get('request_id'), error=str(exc))
        elif kind == 'teleop':
            if not self.ready or self.kind != 'mapping' or self.stop_requested.is_set():
                self.emit('teleop_result', ok=False, request_id=message.get('request_id'),
                          error='Drive pad is available only in a ready mapping session.')
                return
            try:
                params = message.get('params', message)
                self.adapter.deadman.request(params['linear_x'], params['angular_z'])
                self.emit('teleop_result', ok=True, request_id=message.get('request_id'))
            except (KeyError, ValueError) as exc:
                self.emit('teleop_result', ok=False, request_id=message.get('request_id'), error=str(exc))
        elif kind == 'operation':
            self.operation(message)
        elif kind == 'arrange':
            try:
                try:
                    from .windows import arrange_owned
                except ImportError:
                    from windows import arrange_owned
                result = arrange_owned(self.owned)
                self.emit('operation_result', request_id=message.get('request_id'), ok=True, result=result)
            except Exception as exc:
                self.emit('operation_result', request_id=message.get('request_id'), ok=False, error=str(exc))
        else:
            self.log('Unknown worker control: ' + str(kind), 'warn')

    def keep_windows_arranged(self, interval=2., wait=None):
        """Apply the user's window layout (Gazebo left-top, RViz left-bottom) once
        to every new pair of owned Gazebo/RViz windows: a manual session's pair,
        or each trial's fresh pair. Windows the user moves afterwards are left alone."""
        if os.environ.get('XDG_SESSION_TYPE') != 'x11':
            return
        try:
            from .windows import arrange_owned, owned_window_pair
        except ImportError:
            from windows import arrange_owned, owned_window_pair
        wait = wait or self.stop_requested.wait
        arranged, warned = None, set()
        while not self.stop_requested.is_set():
            try:
                pair = owned_window_pair(self.owned)
                if pair is not None and pair != arranged:
                    result = arrange_owned(self.owned)
                    if not result.get('supported'):
                        return
                    arranged = pair
                    self.log('Windows arranged: Gazebo left-top, RViz left-bottom.')
            except Exception as exc:
                if str(exc) not in warned:
                    warned.add(str(exc))
                    self.log('Window layout not applied yet: ' + str(exc), 'warn')
            if wait(interval):
                return

    def watch_trials(self):
        if not self.trials:
            return
        paths = []
        for path in self.output_dir.glob('*/*/trial_*/transport.json'):
            transport = read_json(path)
            if transport:
                paths.append((transport.get('run_ordinal', 0), path, transport))
        for ordinal, path, transport in sorted(paths, key=lambda item: item[0]):
            directory = path.parent
            identity = str(directory.relative_to(self.output_dir))
            controller, tier, trial_id = directory.relative_to(self.output_dir).parts
            pair = {'ros_domain_id': int(transport['ROS_DOMAIN_ID']),
                    'gz_partition': transport['GZ_PARTITION']}
            if identity not in self.seen_transport:
                self.seen_transport.add(identity)
                self.current_trial = {'index': ordinal-1, 'controller': controller, 'tier': tier,
                                      'goal': f'{tier}_{int(trial_id.split("_")[-1]):02d}', **pair}
                self.emit('trial', **self.current_trial, outcome='running', navigation_outcome=None,
                          time_s=None, detail=f'{controller}/{tier}/{trial_id}', trial_dir=identity)
            record = read_json(directory / 'record.json')
            audit = read_json(directory / 'shutdown_audit.json')
            if not record or identity in self.seen_records:
                continue
            self.seen_records.add(identity)
            event = trial_record_event(record, audit, transport, index=ordinal-1, identity=identity,
                stopped=bool(self.requested_stop and self.current_trial
                             and self.current_trial['index'] == ordinal-1))
            if event['bond_abort']:
                self.bond_abort_trials.add(identity)
            self.run_records.append(event)
            self.emit('trial', **event)

    def stop_explorer(self):
        """Autonomous exploration sessions: the explorer cancels its Nav2 goal and records the run's end while
        SLAM and Nav2 still run. A refused or failed stop makes the session's cleanup unclean."""
        if not self.spec.get('explore') or not self.adapter:
            return None
        try:
            report = self.adapter.stop_explorer()
        except Exception as exc:
            report = {'service': '/amr_explorer/stop', 'called': True, 'success': False, 'error': str(exc)}
        write_json(self.log_dir / 'explore_stop.json', report)
        if report.get('called') and not report.get('success'):
            self.setup_error = self.setup_error or ('Explorer stop failed: '
                + str(report.get('error') or report.get('message') or 'unsuccessful response'))
        return report

    def cleanup(self):
        shutdown_at = time.time()
        self._append_log('\n' + SHUTDOWN_MARKER + '\n')
        window = {'complete': True, 'note': 'No manually owned RViz closure needed.'}
        if self.setup_thread:
            self.setup_thread.join(timeout=3)
        if self.adapter:
            errors = self.adapter.halt()
            if errors:
                self.setup_error = self.setup_error or '; '.join(errors)
            if self.kind == 'navigation':
                # Let a cancelled goal finish and the velocity commands go quiet before the
                # world pauses below: pausing simulated time mid-drive froze Nav2's collision
                # monitor (3 Nov live STOP check) and forced a kill during shutdown.
                write_json(self.log_dir / 'stop_settle.json', settle_after_halt(self.adapter))
        for thread in self.operation_threads:
            thread.join(timeout=3)
        self.stop_explorer()
        if self.kind == 'mapping':
            try:
                lifecycle = (self.adapter.quiesce_mapping() if self.adapter else
                    {'node': 'slam_toolbox', 'success': False, 'records': [],
                     'error': 'ROS adapter is unavailable for SLAM lifecycle shutdown.'})
            except Exception as exc:
                lifecycle = {'node': 'slam_toolbox', 'success': False, 'records': [],
                             'error': str(exc)}
            write_json(self.log_dir / 'lifecycle_shutdown.json', lifecycle)
            if not lifecycle['success']:
                self.setup_error = self.setup_error or ('SLAM lifecycle shutdown failed: '
                    + lifecycle.get('error', 'unsuccessful lifecycle response'))
        if not self.trials and self.kind in ('mapping', 'navigation') and os.getenv('DISPLAY'):
            try:
                # End Gazebo publications before bridge destruction, scoped
                # through this manual session's exact transport pair.
                pause = subprocess.run(['gz', 'service', '-s', '/world/default/control',
                    '--reqtype', 'gz.msgs.WorldControl', '--reptype', 'gz.msgs.Boolean',
                    '--timeout', '2000', '--req', 'pause: true'], env=self.environment,
                    capture_output=True, text=True, timeout=6)
                quiesce = {'returncode': pause.returncode, 'stdout': pause.stdout,
                    'stderr': pause.stderr, 'paused': pause.returncode == 0 and 'data: true' in pause.stdout}
                write_json(self.log_dir / 'world_quiesce.json', quiesce)
                if not quiesce['paused']:
                    self.setup_error = self.setup_error or 'World pause before bridge teardown failed.'
                time.sleep(.5)
                # Match the established isolated-trial shutdown sequence: save the disposable view
                # at the final instant, after changing paths/views have stopped,
                # so normal close does not leave a save prompt pending.
                if self.spec.get('no_rviz'):
                    # The session was launched with use_rviz:=false: there is no view to save or
                    # window to close. Gazebo's orderly stop below must still run.
                    window = {'complete': True, 'note': 'RViz was not launched (no_rviz); nothing to save or close.'}
                else:
                    save_private_rviz(self.owned, self.log_dir)
                    # Route this helper's note without replacing process-wide
                    # stdout while output/control threads are emitting JSONL.
                    previous = vars(window_helpers).get('print')
                    window_helpers.print = lambda *values, sep=' ', **_kwargs: self.log(
                        sep.join(str(value) for value in values))
                    try:
                        window = close_rviz(self.owned)
                    finally:
                        if previous is None:
                            del window_helpers.print
                        else:
                            window_helpers.print = previous
                # Keep Gazebo GUI alive while the transport request lets the
                # server finish; Ruby then closes its other child naturally.
                # Do not race that wait with a second launcher/GUI signal.
                gazebo = stop_gazebo(self.owned, self.environment)
                write_json(self.log_dir / 'gazebo_shutdown.json', gazebo)
                if not gazebo['complete'] or (gazebo['targets'] and not gazebo['accepted']):
                    self.setup_error = self.setup_error or gazebo.get('error', 'Gazebo orderly stop failed.')
            except Exception as exc:
                window = {'complete': False, 'error': str(exc)}
        if self.trials and not self.stop_requested.is_set() and not self.owned.refresh():
            tracked, forced, signal_errors = finish_tracking(self.owned), False, []
        elif self.kind == 'job' and not self.owned.refresh():
            tracked, forced, signal_errors = finish_tracking(self.owned), False, []
        else:
            tracked, forced, signal_errors = stop_owned(self.owned, trials=self.trials,
                launch_leader_only=self.kind in ('mapping', 'navigation'))
        for thread in self.readers:
            thread.join(timeout=3)
        self.watch_trials()
        inventory = full_inventory(self.owned)
        log_text = (self.log_dir / 'session.log').read_text(errors='replace')
        forced_evidence = forced_shutdown_evidence(self.log_dir / 'session.log',
            self.output_dir if self.trials else None)
        forced = bool(forced or forced_evidence['events'])
        if forced_evidence['read_errors']:
            self.setup_error = self.setup_error or 'Forced-shutdown evidence could not be read completely.'
        if self.trials:
            # The runner's per-trial classifiers remain authoritative. A
            # canceled runner's nonzero leader code is not a navigation result.
            failed_audits = []
            for path in self.output_dir.glob('*/*/trial_*/shutdown_audit.json'):
                audit = read_json(path)
                if not audit or not audit.get('continue_allowed'):
                    failed_audits.append(str(path.relative_to(ROOT)))
            metadata = read_json(self.output_dir / 'metadata.json') or {}
            records_complete = (metadata.get('status') == 'COMPLETE'
                and metadata.get('completed_trials') == metadata.get('expected_trials')
                and len(self.seen_records) == metadata.get('expected_trials')
                and all(item['outcome'] in ('pass', 'fail') for item in self.run_records))
            launch_ok = tracked['launch_returncode'] == 0 or self.requested_stop
            clean = (not inventory and not tracked['live_survivors'] and not forced
                     and not signal_errors and not failed_audits and launch_ok
                     and (self.requested_stop or records_complete))
            audit = {'clean': clean, 'isolation_pass': not inventory and not tracked['live_survivors'],
                     'failed_trial_audits': failed_audits, 'survivors': inventory,
                     'runner_records_complete': records_complete,
                     'bond_abort': bool(self.bond_abort_trials), 'bond_abort_count': len(self.bond_abort_trials),
                     'scope': 'Full host inventory plus unchanged runner per-trial classifiers.'}
            if self.requested_stop and self.current_trial:
                event = {**self.current_trial, 'outcome': 'stopped', 'navigation_outcome': None,
                         'time_s': None, 'detail': 'User pressed STOP; runner records were preserved.'}
                self.emit('trial', **event)
        else:
            bond = selected_crash_bond_evidence(log_text, tracked, shutdown_at)
            audit = classify_shutdown(log_text, tracked, inventory, window,
                                      bond_evidence=bond, shutdown_at_epoch=shutdown_at)
            clean = audit['continue_allowed'] and not forced and not signal_errors
        if self.setup_error:
            clean = False
        if self.adapter:
            try:
                self.adapter.close()
            except Exception as exc:
                self.setup_error = self.setup_error or 'ROS adapter cleanup failed: ' + str(exc)
                clean = False
        result = {'survivors': len(inventory) + len([item for item in tracked['live_survivors']
                   if item['pid'] not in {record['pid'] for record in inventory}]),
                  'forced': forced, 'bond_abort': bool(audit.get('bond_abort')),
                  'bond_abort_count': len(self.bond_abort_trials) if self.trials else int(bool(audit.get('bond_abort')
                       and audit.get('allowed_exception') and not audit.get('survivors'))),
                  'clean': bool(clean), 'requested_stop': self.requested_stop,
                  'owned_cleanup': tracked, 'postflight': inventory,
                  'window_shutdown': window, 'audit': audit,
                  'forced_evidence': forced_evidence,
                  'initial_signal_policy': tracked.get('initial_signal_policy',
                    'none_processes_already_exited'),
                  'signal_errors': signal_errors, 'readiness_error': self.setup_error}
        result['report'] = str((self.log_dir / 'cleanup.json').relative_to(ROOT))
        write_json(self.log_dir / 'cleanup.json', result)
        if forced or not clean:
            self.emit('alarm', level='alarm', text='Session cleanup requires review: '
                      + str(self.log_dir / 'cleanup.json'), ts=int(time.time()*1000))
        self.emit('finished', cleanup=result, report=str((self.log_dir / 'cleanup.json').relative_to(ROOT)),
                  exit_code=self.collector_code if self.collector_code is not None else self.owned.proc.returncode,
                  forced=forced, bond_abort_count=result['bond_abort_count'])

    def run(self):
        # Trials acquire the same lock inside the unchanged runner. Every
        # manual session/job holds it here for its whole supervisor lifetime.
        if self.trials:
            return self._run()
        lock_path = ROOT / 'log/simulation.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                result = {'survivors': 0, 'forced': False, 'bond_abort': False, 'clean': False,
                          'lock_refused': True, 'report': str((self.log_dir / 'cleanup.json').relative_to(ROOT))}
                write_json(self.log_dir / 'cleanup.json', result)
                self.emit('alarm', level='alarm', text='A terminal simulation holds the workspace lock.',
                          ts=int(time.time()*1000))
                self.emit('finished', cleanup=result, report=result['report'], exit_code=None, forced=False)
                return
            return self._run()

    def _run(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.environment = os.environ.copy()
        self.environment.update(self.spec.get('env', {}))
        self.environment.pop('transport_policy', None)
        if self.trials:
            self.environment.pop('ROS_DOMAIN_ID', None)
            self.environment.pop('GZ_PARTITION', None)
        self.environment['RMW_IMPLEMENTATION'] = 'rmw_cyclonedds_cpp'
        self.environment['ROS_LOG_DIR'] = str(self.log_dir / 'ros_logs')
        # rclpy initializes in this worker, so its context must see the same
        # pair as the launch (Popen's env does not mutate our own environment).
        if self.kind in ('mapping', 'navigation'):
            for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION', 'RMW_IMPLEMENTATION', 'ROS_LOG_DIR'):
                os.environ[key] = self.environment[key]
        with (self.log_dir / 'session.log').open('w') as self.combined_log:
            enable_subreaper()
            preflight = full_inventory()
            write_json(self.log_dir / 'preflight.json', {'processes': preflight, 'passed': not preflight})
            if preflight:
                self.emit('alarm', level='alarm', text='Preflight found simulation processes; none signalled.',
                          ts=int(time.time()*1000))
                result = {'survivors': len(preflight), 'forced': False, 'bond_abort': False,
                          'clean': False, 'postflight': preflight, 'preflight_refused': True}
                write_json(self.log_dir / 'cleanup.json', result)
                self.emit('finished', cleanup=result, exit_code=None, forced=False,
                          report=str((self.log_dir / 'cleanup.json').relative_to(ROOT)))
                return
            if self.kind in ('mapping', 'navigation'):
                # The supervisor itself was started with this pair but has
                # not initialized ROS yet. Exclude only this exact reservation
                # holder; every other process and every UDP port stays checked.
                snapshot = transport_snapshot()
                snapshot['processes'] = [item for item in snapshot['processes']
                                         if item['pid'] != os.getpid()]
                collisions = transport_collisions(self.environment, snapshot)
                write_json(self.log_dir / 'transport_preflight.json', snapshot)
                if collisions:
                    raise RuntimeError('Manual-session transport became occupied; no launch started: '
                                       + str(collisions))
            self.owned = OwnedLaunch.start(self.spec['argv'], cwd=ROOT, env=self.environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if self.trials else subprocess.DEVNULL,
                start_new_session=True, bufsize=0)
            self.start_readers(self.owned.proc)
            threading.Thread(target=self.read_controls, daemon=True).start()
            if self.kind in ('mapping', 'navigation', 'trials'):
                threading.Thread(target=self.keep_windows_arranged, daemon=True).start()
            if self.kind in ('mapping', 'navigation'):
                self.setup_thread = threading.Thread(target=self.setup_ros, daemon=True)
                self.setup_thread.start()
            else:
                self.ready = True
                self.emit_ready()
                if self.trials:
                    items = progress_items(self.spec.get('params', {}))
                    self.emit('progress', total=len(items), items=items)
            try:
                while not self.stop_requested.is_set():
                    if self.setup_error:
                        break
                    self.watch_trials()
                    if self.adapter and self.adapter.deadman:
                        self.adapter.deadman.tick()
                    if self.owned.proc.poll() is not None:
                        if self.kind in ('mapping', 'navigation'):
                            self.setup_error = self.setup_error or 'The manual launch exited before STOP.'
                        break
                    if self.collector and self.collector.poll() is not None:
                        self.collector_code = self.collector.returncode
                        if self.collector_code != 0:
                            self.setup_error = self.setup_error or (
                                f'The scripted trial collector exited with code {self.collector_code}.')
                        break
                    try:
                        self.handle_control(self.controls.get(timeout=.025))
                    except queue.Empty:
                        pass
            except Exception as exc:
                self.setup_error = self.setup_error or str(exc)
                self.log('Session supervisor failed: ' + str(exc), 'alarm')
            finally:
                self.stop_requested.set()
                self.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    args = parser.parse_args()
    worker = SessionWorker(json.loads(args.spec.read_text()))
    # Emergency supervisor termination still requests the same scoped cleanup.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: worker.stop_requested.set())
    worker.run()


if __name__ == '__main__':
    main()
