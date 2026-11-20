#!/usr/bin/env python3
"""Office tasks: the language worker, one separate process with no ROS.

The console starts it with  python3 -I tools/amr_console/language_worker.py --spec <file>
and talks to it in JSON lines (commands on stdin, events on stdout). It runs the amr-language
module: it understands a request, reads the steps back, waits for a yes and holds the
conversation at every stop. It never drives the robot. Each move is a request naming an
approved place key; the console checks the stop pose and the session, drives, and answers.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

# Events whose question is for the person who asked (at the console), not the person at a stop.
REQUESTER_EVENTS = {'confirm_plan', 'person_seen_elsewhere', 'new_person', 'queue_parts', 'split_job',
                    'omissions', 'confirm_collect', 'place_not_on_map', 'place_unreachable',
                    'ask_where_person', 'unknown_name', 'ask_which', 'cannot_do', 'carrier_refusal'}
REQUESTER_PROMPTS = {'mapped place, later, or cancel> ', 'exists yes/no, or approved identity> ',
                     'mapped place> '}
STOP_PROMPTS = {'exact answer> ', 'available person> ', 'where is the person?> '}
MINIMUM_VERSION = (0, 5, 0)
REQUESTER_WAIT_S = 120.0
NAVIGATION_LIMIT_S = 420.0


def version_tuple(text):
    try:
        return tuple(int(part) for part in str(text).split('.')[:3])
    except ValueError:
        return (0, 0, 0)


def clean(value, depth=0):
    """A JSON-safe, bounded copy of event data for the console page."""
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {str(k): clean(v, depth + 1) for k, v in value.items() if k != 'trace'}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        return [clean(v, depth + 1) for v in items[:100]]
    if isinstance(value, str):
        return value[:4000]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float('inf') else None
    return str(value)[:4000]


class Output:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()
        self.closed = threading.Event()

    def emit(self, type_, **fields):
        line = json.dumps({'type': type_, **clean(fields)}, ensure_ascii=False, allow_nan=False)
        with self.lock:
            try:
                self.stream.write(line + '\n')
                self.stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.closed.set()


def import_language(root):
    root = Path(root).resolve()
    for path in (root, root / 'scripts'):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import amr_language
    from amr_language.rehearsal import Rehearsal, RehearsalStopped
    if version_tuple(getattr(amr_language, '__version__', '0')) < MINIMUM_VERSION or not hasattr(Rehearsal, 'move_to'):
        raise RuntimeError('This console needs amr-language 0.5.0 or newer (robot movement hooks); '
                           f'found {getattr(amr_language, "__version__", "unknown")}.')
    return amr_language, Rehearsal, RehearsalStopped


class ModelService:
    """The local model runner: started and owned here, or an identical one already running."""

    def __init__(self, root, log_dir, settings, mode, out):
        self.root, self.log_dir, self.settings, self.mode, self.out = Path(root), Path(log_dir), settings, mode, out
        self.runner = None
        self.process = None
        self.owned = False
        self.kind = None

    def _request(self, path, payload=None, timeout=60):
        from runner_control import request
        return request(path, payload, timeout)[0]

    def _port_in_use(self):
        try:
            self._request('/api/version', timeout=.4)
            return True
        except Exception:
            return False

    def _diagnose(self, log_path):
        text = ''
        try:
            text = Path(log_path).read_text(errors='replace')[-4000:]
        except (OSError, TypeError):
            pass
        if 'setting up uid map' in text or 'Permission denied' in text and 'bwrap' in text:
            return ('runner_isolation_blocked',
                    'The model runner could not start its bubblewrap sandbox: this Ubuntu blocks it '
                    '(AppArmor user-namespace restriction). Either allow bubblewrap with an AppArmor '
                    'rule (one admin step, see docs/OFFICE_TASKS.md), or set '
                    'language_module.model_runner: uncontained in config/office_tasks.yaml.')
        return ('runner_failed', 'The model runner exited during start-up. Its private log is ' + str(log_path))

    def start(self):
        from amr_language.office_backend import make_backend
        model = self.settings['model']
        if self._port_in_use():
            # Never stop an unowned server. Use it only if it is the same runner and model.
            backend = make_backend(self.settings)
            backend.verify(self.settings, 15)
            self.kind = 'external'
        elif self.mode == 'uncontained':
            self._start_uncontained()
            self.kind = 'uncontained'
        else:
            from runner_control import Runner
            self.runner = Runner(self.log_dir / 'runner')
            try:
                self.runner.start()
            except Exception as exc:
                code, reason = self._diagnose(getattr(self.runner, 'log_path', None))
                self.runner.close()
                self.runner = None
                raise ServiceError(code, reason) from exc
            self.owned = True
            self.kind = 'contained'
        self._request('/api/generate', {'model': model, 'prompt': '', 'stream': False, 'keep_alive': '30m',
                                        'options': {'num_ctx': self.settings.get('num_ctx', 4096)}}, 90)
        ps = self._request('/api/ps')
        rows = [row for row in ps.get('models', []) if row.get('name') == model]
        on_gpu = bool(rows) and rows[0].get('size') and rows[0].get('size') == rows[0].get('size_vram')
        return {'runner': self.kind, 'model': model, 'fully_on_gpu': bool(on_gpu)}

    def _start_uncontained(self):
        binary = self.root / '.runtime/ollama/bin/ollama'
        if not binary.is_file():
            raise ServiceError('runner_missing', 'The approved model runner is not installed in the language module.')
        runtime = self.root / '.runtime'
        env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'PATH') if key in os.environ}
        env.update({'OLLAMA_HOST': '127.0.0.1:11434', 'OLLAMA_MODELS': str(runtime / 'models'),
                    'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NOHISTORY': '1', 'OLLAMA_NOPRUNE': '1',
                    'OLLAMA_NUM_PARALLEL': '1', 'OLLAMA_MAX_LOADED_MODELS': '1', 'OLLAMA_MAX_QUEUE': '1',
                    'OLLAMA_CONTEXT_LENGTH': '4096', 'OLLAMA_GPU_OVERHEAD': str(4 * 1024 ** 3),
                    'OLLAMA_KEEP_ALIVE': '5m', 'TMPDIR': str(runtime / 'tmp'),
                    'XDG_CACHE_HOME': str(runtime / 'cache'), 'CUDA_CACHE_PATH': str(runtime / 'cache/cuda')})
        for directory in ('models', 'tmp', 'cache'):
            (runtime / directory).mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = (self.log_dir / 'runner_uncontained.log').open('w')
        self.process = subprocess.Popen([str(binary), 'serve'], cwd=self.root, env=env, stdout=subprocess.DEVNULL,
                                        stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        self.owned = True
        started = time.monotonic()
        while time.monotonic() - started < 20:
            if self.process.poll() is not None:
                raise ServiceError('runner_failed', 'The model runner exited during start-up; see '
                                   + str(self.log_dir / 'runner_uncontained.log'))
            if self._port_in_use():
                return
            time.sleep(.1)
        raise ServiceError('runner_failed', 'The model runner did not answer within 20 seconds.')

    def keep_warm(self):
        try:
            self._request('/api/generate', {'model': self.settings['model'], 'prompt': 'Say hello.', 'stream': False,
                                            'think': False, 'keep_alive': '30m',
                                            'options': {'num_ctx': 4096, 'num_predict': 2, 'temperature': 0}}, 30)
            return True
        except Exception:
            return False

    def close(self):
        # Only a runner this worker started is ever stopped.
        if self.runner is not None:
            try:
                self.runner.close()
            finally:
                self.runner = None
        if self.process is not None:
            import signal
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=8)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            self.process = None


class ServiceError(RuntimeError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def session_class(Rehearsal, RehearsalStopped):
    class ConsoleSession(Rehearsal):
        """The language module's job flow, with robot moves and human replies through the console."""
        drives_robot = True

        def __init__(self, jobs, names, settings):
            super().__init__(names, settings, input_fn=self._console_input, output_fn=lambda _line: None,
                             log_path=None, source='UNREVIEWED_TYPED')
            self.jobs = jobs
            self.expecting = None
            self.last_event = None

        def emit(self, kind, **data):
            super().emit(kind, **data)
            job_id = self.current.get('job_id') if isinstance(self.current, dict) else None
            self.jobs.event(self, job_id, kind, data)

        def speak(self, event, language=None, **slots):
            self.last_event = event
            return super().speak(event, language, **slots)

        def reply(self, expect, language=None, recipient=None, item=None):
            self.expecting = sorted(expect)
            try:
                return super().reply(expect, language, recipient, item)
            finally:
                self.expecting = None

        def _confirm(self, result):
            # The yes before moving is a conversation in 0.8.0; the page still shows Yes and No.
            self.expecting = ['no', 'yes']
            try:
                return super()._confirm(result)
            finally:
                self.expecting = None

        def _console_input(self, prompt):
            return self.jobs.wait_input(self, prompt)

        def move_to(self, place, purpose, target=None):
            return self.jobs.move(self, place, purpose, target)

    ConsoleSession.Stopped = RehearsalStopped
    return ConsoleSession


class Jobs:
    """Requests run one after another; a request that arrives while the robot works is queued."""

    def __init__(self, out, mode, settings, Session, stop_wait_s=60.0, rehearsal_move_s=1.0, cancel_epoch=0):
        self.out, self.mode, self.settings, self.Session = out, mode, settings, Session
        self.stop_wait_s = stop_wait_s
        self.rehearsal_move_s = rehearsal_move_s
        self.lock = threading.Lock()
        # The console numbers every Cancel and STOP. Each drive request carries the number seen
        # when it was checked, so the console can refuse a drive asked for before a Cancel.
        self.cancel_epoch = cancel_epoch
        self.forget_position = False
        self.pending = deque()
        self.session = None
        self.thread = None
        self.inputs = queue.Queue()
        self.cancelled = threading.Event()
        self.navigations = {}
        self.position = None
        self.awaiting = None
        self.job_states = {}

    # -- requests ------------------------------------------------------------------------
    def submit(self, job):
        entry = {'job_id': job['job_id'], 'text': job['text'], 'requester': job['requester'],
                 'language': job.get('language'), 'runtime_names': job['names']}
        with self.lock:
            entry['cancel_epoch'] = self.cancel_epoch
            if self.thread is not None and self.thread.is_alive():
                target = self.session.queue if self.session is not None else self.pending
                target.append(entry)
                position = len(target)
                self._state(job['job_id'], 'queued', position=position)
                return
            self.cancelled.clear()
            self.pending.append(entry)
            self._state(job['job_id'], 'queued', position=1)
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def _state(self, job_id, state, **extra):
        if job_id:
            self.job_states[job_id] = state
            self.out.emit('job', job_id=job_id, state=state, **extra)

    def _loop(self):
        while True:
            with self.lock:
                if not self.pending or self.cancelled.is_set():
                    for entry in self.pending:
                        self._state(entry['job_id'], 'cancelled', reason='cancelled before it started')
                    self.pending.clear()
                    self.session = None
                    self.thread = None
                    return
                batch = list(self.pending)
                self.pending.clear()
                session = self.Session(self, batch[0]['runtime_names'], dict(self.settings))
                session.position = self.position
                session.queue.extend(batch)
                self.session = session
            try:
                result = session.run()
            except Exception as exc:  # A bug must end the job visibly, never silently.
                result = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
                self.out.emit('log', level='alarm', text='The office task stopped on an error: ' + result['error'])
            with self.lock:
                self.position = None if self.forget_position else session.position
                self.forget_position = False
                leftover = list(session.queue)
                session.queue.clear()
                for job_id, state in list(self.job_states.items()):
                    if state in ('understanding', 'awaiting_confirmation', 'running'):
                        final = {'stopped': 'stopped', 'error': 'failed'}.get(result.get('status'), 'done')
                        self._state(job_id, final)
                finished = [k for k, v in self.job_states.items() if v not in ('queued', 'understanding',
                                                                                'awaiting_confirmation', 'running')]
                for job_id in finished[:-50]:
                    self.job_states.pop(job_id, None)
                # A Cancel ends everything asked for before it; a request sent after it still runs.
                # A STOP or an error without a Cancel ends the whole queue.
                if self.cancelled.is_set():
                    survivors = [e for e in leftover if e.get('cancel_epoch', -1) >= self.cancel_epoch]
                elif result.get('status') in ('stopped', 'error'):
                    survivors = []
                else:
                    survivors = leftover
                for entry in leftover:
                    if entry not in survivors:
                        self._state(entry['job_id'], 'cancelled', reason='the queue was cancelled')
                self.pending.extendleft(reversed(survivors))
                self.awaiting = None
                self.out.emit('session', status=result.get('status'), position=self.position)
                if self.cancelled.is_set():
                    self.cancelled.clear()
                    later = [e for e in self.pending if e.get('cancel_epoch', -1) >= self.cancel_epoch]
                    for entry in self.pending:
                        if entry not in later:
                            self._state(entry['job_id'], 'cancelled', reason='the queue was cancelled')
                    self.pending.clear()
                    self.pending.extend(later)

    def cancel(self, reason, epoch=None, forget_position=False):
        with self.lock:
            if type(epoch) is int:
                self.cancel_epoch = max(self.cancel_epoch, epoch)
            if forget_position:
                # STOP ends the navigation session: the next one starts the robot at the dock.
                # A job still unwinding must not hand its last stop back afterwards either.
                self.position = None
                self.forget_position = self.session is not None
            self.cancelled.set()
            for entry in self.pending:
                self._state(entry['job_id'], 'cancelled', reason=reason)
            self.pending.clear()
            if self.session is not None:
                for entry in list(self.session.queue):
                    self._state(entry['job_id'], 'cancelled', reason=reason)
                self.session.queue.clear()
        self.out.emit('cancelling', reason=reason)

    # -- conversation --------------------------------------------------------------------
    def event(self, session, job_id, kind, data):
        if kind == 'screen' and data.get('text') == 'Thinking…':
            self._state(job_id, 'understanding')
        elif kind == 'understand':
            result = data.get('result') or {}
            decision = result.get('decision')
            self._state(job_id, 'awaiting_confirmation' if decision == 'act' else 'answered', decision=decision)
        elif kind == 'confirmation':
            self._state(job_id, 'running' if data.get('confirmed') else 'cancelled')
        elif kind == 'robot' and data.get('event') == 'report':
            self._state(job_id, 'done')
        elif kind == 'follow_up_request':
            # A new request given to the robot at its last stop: it runs next, as that person's own job.
            self._state(data.get('job_id'), 'queued', position=1)
        self.out.emit('office_event', job_id=job_id, kind=kind, data=data,
                      position=session.position)

    def audience(self, session, prompt):
        if prompt in REQUESTER_PROMPTS:
            return 'requester'
        if prompt in STOP_PROMPTS:
            return 'stop'
        requester_place = (session.names or {}).get('requester_place')
        if session.last_event in REQUESTER_EVENTS:
            return 'requester'
        if session.last_event in ('pickup_check', 'bag_check') and session.position == requester_place:
            return 'requester'
        if session.last_event in ('reply_prompt', None, 'anything_else', 'anything_else_what', 'what_change',
                                  'say_slowly', 'english_only') and session.current and \
                self.job_states.get(session.current.get('job_id')) == 'awaiting_confirmation':
            return 'requester'
        return 'stop'

    def wait_input(self, session, prompt):
        input_id = uuid.uuid4().hex
        audience = self.audience(session, prompt)
        if audience == 'requester':
            limit = REQUESTER_WAIT_S
        else:
            limit = max(10.0, float(self.stop_wait_s) / 3.0)
        job_id = session.current.get('job_id') if session.current else None
        self.awaiting = {'input_id': input_id, 'job_id': job_id, 'prompt': prompt, 'expect': session.expecting,
                         'audience': audience, 'place': session.position, 'wait_s': limit,
                         'last_event': session.last_event}
        self.out.emit('awaiting_input', **self.awaiting)
        deadline = time.monotonic() + limit
        while True:
            if self.cancelled.is_set():
                self.awaiting = None
                raise session.Stopped()
            try:
                item = self.inputs.get(timeout=.2)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    self.awaiting = None
                    self.out.emit('input_timeout', input_id=input_id, job_id=job_id, waited_s=limit)
                    return ''
                continue
            if item.get('input_id') != input_id:
                continue  # A late or duplicate reply to an earlier question.
            self.awaiting = None
            self.out.emit('input_received', input_id=input_id, job_id=job_id, text=item.get('text', ''))
            return item.get('text', '')

    def deliver_input(self, input_id, text):
        self.inputs.put({'input_id': input_id, 'text': text})

    # -- movement ------------------------------------------------------------------------
    def move(self, session, place, purpose, target):
        job_id = session.current.get('job_id') if session.current else None
        with self.lock:
            # Checked together with the number, so a Cancel cannot slip in between unnoticed.
            if self.cancelled.is_set():
                raise session.Stopped()
            epoch = self.cancel_epoch
        if self.mode == 'rehearsal':
            self.out.emit('navigate_simulated', job_id=job_id, place=place, purpose=purpose, target=target)
            end = time.monotonic() + self.rehearsal_move_s
            while time.monotonic() < end:
                if self.cancelled.is_set():
                    session.position = None      # stopped on the way: not at the last stop any more
                    raise session.Stopped()
                time.sleep(.05)
            return place is not None
        if place is None:
            self.out.emit('log', level='warn', text=f'No approved stop is known for {target}; that step cannot be reached.')
            return False
        nav_id = uuid.uuid4().hex
        waiter = {'event': threading.Event(), 'result': None}
        self.navigations[nav_id] = waiter
        self.out.emit('navigate', nav_id=nav_id, job_id=job_id, place=place, purpose=purpose, target=target,
                      cancel_epoch=epoch)
        started = time.monotonic()
        cancel_seen = None
        try:
            while not waiter['event'].wait(.2):
                if self.cancelled.is_set():
                    # The console cancels the goal and answers at once; never wait long for it.
                    cancel_seen = cancel_seen or time.monotonic()
                    if time.monotonic() - cancel_seen > 15:
                        raise session.Stopped()
                if time.monotonic() - started > NAVIGATION_LIMIT_S:
                    self.out.emit('log', level='alarm', text='No navigation result arrived in time; the step failed.')
                    return False
            result = waiter['result'] or {}
            if self.cancelled.is_set():
                raise session.Stopped()
        except session.Stopped:
            # A drive was asked for: the robot may be anywhere between the two stops now.
            session.position = None
            raise
        finally:
            self.navigations.pop(nav_id, None)
        return bool(result.get('ok'))

    def navigation_result(self, nav_id, result):
        waiter = self.navigations.get(nav_id)
        if waiter is not None:
            waiter['result'] = result
            waiter['event'].set()


class Worker:
    def __init__(self, spec, out=None, stdin=None):
        self.spec = spec
        self.out = out or Output()
        self.stdin = stdin or sys.stdin
        self.service = None
        self.jobs = None
        self.ready = threading.Event()
        self.closing = threading.Event()
        self.cancel_epoch = 0

    def settings(self, amr_language):
        from amr_language.office_backend import load_office_profile
        settings = load_office_profile(self.spec.get('profile', 'office_B'))
        settings.update(timeout_s=float(self.spec.get('timeout_s', 20)), follow_person_hint='ask',
                        input_source='UNREVIEWED_TYPED', capability_profile=self.spec['capability'])
        # The closing question to the last recipient needs amr-language 0.6.0; older modules run without it.
        if version_tuple(getattr(amr_language, '__version__', '0')) >= (0, 6, 0):
            settings.update(closing_question=bool(self.spec.get('closing_question', False)),
                            follow_up_limit=int(self.spec.get('follow_up_limit', 3)))
        # Looking for a person who does not answer, and reading back an uncertain plan: 0.7.0.
        if version_tuple(getattr(amr_language, '__version__', '0')) >= (0, 7, 0):
            settings.update(search_for_person=bool(self.spec.get('search_for_person', False)),
                            tentative_plans=bool(self.spec.get('tentative_plans', False)))
        # The assistant's conversation, English only, and the model reading free replies: 0.8.0.
        if version_tuple(getattr(amr_language, '__version__', '0')) >= (0, 8, 0):
            settings.update(conversational=bool(self.spec.get('conversational', False)),
                            english_only=bool(self.spec.get('english_only', False)),
                            reply_model=bool(self.spec.get('reply_model', False)) and self.spec.get('test_backend') != 'echo')
        if self.spec.get('test_backend') == 'echo':
            if os.environ.get('AMR_CONSOLE_TEST_LANGUAGE') != '1':
                raise ServiceError('test_only', 'The echo language backend exists for automated tests only.')
            from amr_language.office_backend import MockOfficeBackend

            class EchoBackend(MockOfficeBackend):
                name = 'test-echo'

                def __init__(self):
                    super().__init__(['echo'], [{}])

                def rewrite(self, text, roster, language, index, timeout, settings):
                    self.calls.append(('rewrite', index))
                    return text
            settings['backend'] = EchoBackend()
        return settings

    def start(self):
        try:
            self.out.emit('service', state='starting', detail='Loading the language module.')
            amr_language, Rehearsal, RehearsalStopped = import_language(self.spec['language_root'])
            settings = self.settings(amr_language)
            info = {'runner': 'test', 'model': 'test-echo', 'fully_on_gpu': None}
            if self.spec.get('test_backend') != 'echo':
                self.out.emit('service', state='starting', detail='Starting the model runner and loading the model.')
                self.service = ModelService(self.spec['language_root'], Path(self.spec['log_dir']), settings,
                                            self.spec.get('model_runner', 'contained'), self.out)
                info = self.service.start()
                from amr_language.office_backend import prepare_office
                info['prepared'] = prepare_office(settings)
            Session = session_class(Rehearsal, RehearsalStopped)
            self.jobs = Jobs(self.out, self.spec.get('mode', 'robot'), settings, Session,
                             stop_wait_s=float(self.spec['capability'].get('maximum_wait_at_stop_s', 60)),
                             rehearsal_move_s=float(self.spec.get('rehearsal_move_s', 1.0)),
                             cancel_epoch=self.cancel_epoch)
            self.ready.set()
            self.out.emit('service', state='ready', detail='Ready for requests.',
                          version=amr_language.__version__, mode=self.spec.get('mode', 'robot'), **info)
            last = time.monotonic()
            while not self.closing.wait(5):
                if self.service is not None and time.monotonic() - last > 240:
                    busy = self.jobs.session is not None
                    if not busy:
                        self.service.keep_warm()
                    last = time.monotonic()
        except ServiceError as exc:
            self.out.emit('service', state='error', code=exc.code, detail=exc.reason)
        except Exception as exc:
            self.out.emit('service', state='error', code='start_failed', detail=f'{type(exc).__name__}: {exc}')

    def handle(self, message):
        kind = message.get('type')
        if kind == 'shutdown':
            return False
        if kind == 'navigate_result':
            if self.jobs is not None:
                self.jobs.navigation_result(message.get('nav_id'), message)
            return True
        if kind in ('cancel', 'stop'):
            # Always counted, even while starting, so both sides keep the same Cancel number.
            epoch = message.get('epoch')
            if type(epoch) is int:
                self.cancel_epoch = max(self.cancel_epoch, epoch)
            if self.jobs is not None:
                self.jobs.cancel('stopped' if kind == 'stop' else 'cancelled', epoch=self.cancel_epoch,
                                 forget_position=kind == 'stop')
            return True
        if not self.ready.is_set():
            self.out.emit('refused', request_type=kind, reason='The language worker is not ready yet.')
            return True
        if kind == 'submit':
            self.jobs.submit(message)
        elif kind == 'input':
            self.jobs.deliver_input(message.get('input_id'), str(message.get('text', ''))[:2000])
        elif kind == 'position':
            self.jobs.position = message.get('place')
        else:
            self.out.emit('refused', request_type=kind, reason='Unknown command.')
        return True

    def run(self):
        threading.Thread(target=self.start, daemon=True).start()
        try:
            for line in self.stdin:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError('not an object')
                except ValueError:
                    self.out.emit('log', level='warn', text='The language worker received an unreadable command.')
                    continue
                if not self.handle(message):
                    break
        finally:
            self.closing.set()
            if self.jobs is not None:
                self.jobs.cancel('the language service stopped')
            if self.service is not None:
                self.service.close()
            self.out.emit('service', state='off', detail='The language service stopped.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    args = parser.parse_args()
    Worker(json.loads(args.spec.read_text())).run()


if __name__ == '__main__':
    main()
