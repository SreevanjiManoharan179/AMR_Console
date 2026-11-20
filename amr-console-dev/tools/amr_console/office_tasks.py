"""Office tasks, console side: owns the language worker process and carries out its moves.

The language worker understands a request, reads the steps back and talks with people at
each stop. It never drives. When it needs the robot somewhere it names an approved place
key; this module checks the owner approvals and the running session, takes the stop pose
from the approved files and asks the navigation session to drive there. STOP and Cancel
always reach the robot first and the conversation second.
"""
from collections import OrderedDict, deque
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

from errors import ConsoleError
import office_context
from office_context import OfficeConfigError, OfficeContext
import office_speech

# A job is open from the moment it is sent until the worker reports its end.
ACTIVE = ('submitted', 'queued', 'understanding', 'awaiting_confirmation', 'running')
TEXT_LIMIT = 1000
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _now_text():
    return datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(timespec='seconds')


class LanguageProcess:
    """The worker process and its JSON-line pipes. Replaceable in tests."""

    def __init__(self, root, spec, on_event, on_exit):
        self.root, self.spec, self.on_event, self.on_exit = Path(root), spec, on_event, on_exit
        self.proc = None
        self.write_lock = threading.Lock()

    def start(self):
        log_dir = Path(self.spec['log_dir'])
        log_dir.mkdir(parents=True, exist_ok=True)
        spec_path = log_dir / 'spec.json'
        spec_path.write_text(json.dumps(self.spec, indent=2, ensure_ascii=False) + '\n')
        # No ROS environment and no inherited Python paths: the worker is plain Python.
        env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'PATH')
               if key in os.environ}
        if os.environ.get('AMR_CONSOLE_TEST_LANGUAGE') == '1':
            env['AMR_CONSOLE_TEST_LANGUAGE'] = '1'
            # Automated tests: what the language module records (learning suggestions) goes into
            # this run's log folder, never into the owner's review queue in ../amr-language/records.
            env['AMR_LANGUAGE_RECORDS_DIR'] = str(log_dir.resolve() / 'language_records')
        self.proc = subprocess.Popen(
            [sys.executable, '-I', str(self.root / 'tools/amr_console/language_worker.py'), '--spec', str(spec_path)],
            cwd=self.root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _log(self, line):
        try:
            with (Path(self.spec['log_dir']) / 'worker.log').open('a') as stream:
                stream.write(line if line.endswith('\n') else line + '\n')
        except OSError:
            pass

    def _read(self):
        for line in self.proc.stdout:
            self._log(line)
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError
            except ValueError:
                self.on_event({'type': 'log', 'level': 'warn', 'text': 'The language worker wrote an unreadable line.'})
                continue
            self.on_event(event)
        self.on_exit(self.proc.wait())

    def _stderr(self):
        for line in self.proc.stderr:
            self._log('[stderr] ' + line)

    def send(self, message):
        with self.write_lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError('The language worker is not running.')
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self, timeout=20):
        if self.proc is None:
            return
        try:
            self.send({'type': 'shutdown'})
        except RuntimeError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Our own child: end it; its owned model runner exits with its process group.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class OfficeTasks:
    def __init__(self, backend, *, process_factory=LanguageProcess, test_backend=None,
                 speech_factory=office_speech.SpeechProcess, test_speech=None):
        self.backend = backend
        self.root = Path(backend.root)
        self.process_factory = process_factory
        self.test_backend = test_backend
        self.speech_factory = speech_factory
        self.test_speech = test_speech
        self.speech = None
        self._speech_cache = None
        self.lock = threading.RLock()
        self.process = None
        self.mode = None
        self.context = None
        self.service = {'state': 'off', 'detail': 'The language service is off.'}
        self.jobs = OrderedDict()
        self.timeline = deque(maxlen=120)
        self.awaiting = None
        self.position = None
        self.moving = None
        self.log_dir = None
        self.sequence = 0
        self.epoch = secrets.token_hex(4)   # changes when the console restarts
        self.version = 0
        self._context_cache = None
        # Every Cancel and STOP gets the next number. A drive request carries the number the
        # worker had seen when it asked; an older number means it was asked for before a Cancel.
        self.cancel_epoch = 0
        self.navigation_threads = []

    # -- configuration -------------------------------------------------------------------
    def _session_map(self):
        session = self.backend.session or {}
        if self.backend.state == 'running' and session.get('kind') == 'navigation':
            return session.get('map')
        return None

    def _office_map(self, config):
        session_map = self._session_map()
        if session_map in config['maps']:
            return session_map
        return next(iter(config['maps']))

    def _context(self):
        config = office_context.load_config(self.root)
        # A separate tool (the Speech Lab) may run the same service with other switches.
        for block, values in (getattr(self, 'config_overrides', None) or {}).items():
            config[block] = {**(config.get(block) or {}), **values}
        return OfficeContext(self.root, self._office_map(config), config=config)

    def _idle_context(self):
        """The office context while the service is off. Building one checks every desk on the map
        (about half a second), so it is reused for a few seconds and never built under the lock."""
        key, now = self._session_map(), time.monotonic()
        cached = self._context_cache
        if cached is None or cached[0] != key or now - cached[1] > 5:
            try:
                value = self._context()
            except OfficeConfigError as exc:
                value = exc
            self._context_cache = cached = (key, now, value)
        if isinstance(cached[2], Exception):
            raise cached[2]
        return cached[2]

    def blocks(self, mode='robot', context=None):
        """Reasons a request cannot run in this mode now; empty means it can."""
        reasons = []
        try:
            context = context or self.context or self._idle_context()
        except OfficeConfigError as exc:
            return [str(exc)]
        if self.service['state'] != 'ready':
            reasons.append('Start the language service first.' if self.service['state'] in ('off', 'error')
                           else 'The language service is still starting.')
        if mode == 'robot':
            session = self.backend.session or {}
            if self.backend.state != 'running' or session.get('kind') != 'navigation':
                reasons.append('Start navigation on the office map (Navigate tab), or use Try without the robot.')
            elif session.get('map') != context.map_id:
                reasons.append(f'The running navigation session is not on the office map ({context.map_id}).')
            elif not session.get('record_truth'):
                reasons.append('Start navigation with ground-truth recording on.')
            if self.backend.stopping:
                reasons.append('The session is stopping.')
            reasons += context.motion_blocks()
            if context.changed():
                reasons.append('An approved office file changed. Stop and start the language service again.')
        return reasons

    # -- status --------------------------------------------------------------------------
    def _speech_status(self, context):
        if self.speech is not None:
            return self.speech.status()
        if context is None:
            return {'state': 'error', 'detail': 'the office settings could not be read.', 'languages': {}}
        now = time.monotonic()
        if self._speech_cache is None or now - self._speech_cache[0] > 5:
            self._speech_cache = (now, office_speech.install_status(self.root, context.config))
        value = self._speech_cache[1]
        return {key: value.get(key) for key in ('state', 'detail', 'languages')}

    def snapshot(self):
        context, config_error = self.context, None
        if context is None:
            try:
                context = self._idle_context()
            except OfficeConfigError as exc:
                config_error = str(exc)
        with self.lock:
            summary = context.summary() if context is not None else None
            mode = self.mode or 'robot'
            jobs = list(self.jobs.values())[-12:]
            return copy.deepcopy({
                'service': self.service, 'mode': self.mode, 'context': summary, 'config_error': config_error,
                'english_only': bool(context is not None and (context.config.get('assistance') or {}).get('english_only')),
                'blocks': {'robot': self.blocks('robot', context) if not config_error else [config_error],
                           'rehearsal': self.blocks('rehearsal', context) if not config_error else [config_error]},
                'jobs': jobs, 'awaiting': self.awaiting, 'position': self.position,
                'position_label': context.label(self.position) if (not config_error and self.position) else None,
                'moving': self.moving, 'timeline': list(self.timeline)[-60:],
                'busy': self.busy(), 'active_mode': mode, 'speech': self._speech_status(context),
                'epoch': self.epoch, 'version': self.version})

    def brief(self):
        with self.lock:
            return {'service': self.service.get('state'), 'mode': self.mode, 'busy': self.busy(),
                    'awaiting': bool(self.awaiting)}

    def busy(self):
        return any(job['state'] in ACTIVE for job in self.jobs.values()) or self.moving is not None

    def robot_busy(self):
        return self.mode == 'robot' and self.busy()

    def _publish(self):
        # Each published snapshot gets the next version, taken under the lock, so a page that
        # receives an older snapshot later (a slow GET, a late event) can recognise and drop it.
        if self.context is None:
            try:
                self._idle_context()     # any slow file reading happens before the lock
            except OfficeConfigError:
                pass
        with self.lock:
            self.version += 1
            snapshot = self.snapshot()
        self.backend.events.publish({'type': 'office', 'office': snapshot})

    def _record(self, entry):
        entry = dict(entry, at=_now_text())
        self.sequence += 1
        entry['seq'] = self.sequence
        self.timeline.append(entry)
        if self.log_dir:
            try:
                with (self.log_dir / 'timeline.jsonl').open('a') as stream:
                    stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except OSError:
                pass

    # -- service -------------------------------------------------------------------------
    def start(self, mode):
        if mode not in ('robot', 'rehearsal'):
            raise ConsoleError(422, 'office_mode', 'Choose robot or rehearsal mode.')
        if self.process is not None and self.process.alive() and self.service.get('state') == 'error':
            # A worker whose model runner failed to start stays alive but useless: Start means retry.
            self.stop_service()
        if self.process is not None and self.process.alive():
            if self.mode == mode:
                return self.snapshot()
            raise ConsoleError(409, 'office_running', 'Stop the language service before changing mode.')
        # Reading the approved files takes about half a second: do it before taking the lock.
        try:
            context = self._context()
            language_root = office_context.language_root(self.root, context.config)
        except OfficeConfigError as exc:
            raise ConsoleError(409, 'office_config', str(exc)) from exc
        with self.lock:
            if self.process is not None and self.process.alive():
                if self.mode == mode:
                    return self.snapshot()
                raise ConsoleError(409, 'office_running', 'Stop the language service before changing mode.')
            self.context = context
            config = context.config
            if mode == 'robot':
                blocks = [b for b in self.context.motion_blocks()]
                if blocks:
                    raise ConsoleError(409, 'office_approval', ' '.join(blocks))
            stamp = datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S') + '_' + secrets.token_hex(3)
            self.log_dir = self.root / 'data/logs/office' / stamp
            module = config['language_module']
            spec = {'language_root': str(language_root), 'log_dir': str(self.log_dir), 'mode': mode,
                    'profile': module.get('profile', 'office_B'), 'timeout_s': module.get('timeout_s', 20),
                    'model_runner': module.get('model_runner', 'contained'),
                    'capability': self.context.worker_capability(rehearsal=(mode == 'rehearsal')),
                    'closing_question': bool((config.get('continued_operation') or {}).get('closing_question', False)),
                    'follow_up_limit': int((config.get('continued_operation') or {}).get('follow_up_limit', 3)),
                    'search_for_person': bool((config.get('assistance') or {}).get('search_for_person', False)),
                    'tentative_plans': bool((config.get('assistance') or {}).get('tentative_plans', False)),
                    'conversational': bool((config.get('assistance') or {}).get('conversational', False)),
                    'english_only': bool((config.get('assistance') or {}).get('english_only', False)),
                    'reply_model': bool((config.get('assistance') or {}).get('model_reads_replies', False))}
            if self.test_backend:
                spec.update(test_backend=self.test_backend, rehearsal_move_s=0.05)
            self.mode = mode
            self.jobs.clear()
            self.timeline.clear()
            self.awaiting = None
            self.moving = None
            self.position = None
            self.cancel_epoch = 0          # the new worker counts from zero as well
            self.service = {'state': 'starting', 'detail': 'Starting the language worker.'}
            self.process = self.process_factory(self.root, spec, self._on_event, self._on_exit)
            try:
                self.process.start()
            except Exception as exc:
                self.process = None
                self.service = {'state': 'error', 'detail': f'The language worker could not start: {exc}'}
                self._publish()
                raise ConsoleError(500, 'office_start', self.service['detail']) from exc
            self._record({'kind': 'service', 'text': f'Language service starting ({"robot" if mode == "robot" else "without the robot"}).'})
            # Speech input starts beside it when installed; typing works either way.
            if self.speech is not None:
                threading.Thread(target=self.speech.stop, daemon=True).start()
            self.speech = office_speech.Speech(self.root, config, process_factory=self.speech_factory,
                                               on_change=self._publish)
            try:
                if not self.speech.start(self.log_dir, self.context, test_python=self.test_speech):
                    self.speech = None
            except Exception as exc:
                self.speech = None
                self._record({'kind': 'note', 'text': f'Speech input could not start: {exc}. Typing still works.'})
        self._publish()
        return self.snapshot()

    def stop_service(self):
        with self.lock:
            process = self.process
            speech, self.speech = self.speech, None
            if process is not None:
                self.service = {'state': 'stopping', 'detail': 'Stopping the language service.'}
        if process is None:
            # The language worker already exited: stop any drive it left, and its speech worker.
            self._cancel_motion()
            if speech is not None:
                speech.stop()
            self._publish()
            return self.snapshot()
        self._cancel_motion()
        self._publish()
        if speech is not None:
            speech.stop()
        process.close()
        with self.lock:
            self.process = None
            self.service = {'state': 'off', 'detail': 'The language service is off.'}
            for job in self.jobs.values():
                if job['state'] in ACTIVE:
                    job['state'] = 'cancelled'
            self.awaiting = None
            self.moving = None
        self._publish()
        return self.snapshot()

    def shutdown(self):
        try:
            self.stop_service()
        except Exception:
            pass

    # -- requests ------------------------------------------------------------------------
    def submit(self, text, requester_id, language, place):
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT or CONTROL.search(text):
            raise ConsoleError(422, 'office_text', f'Type a request of 1–{TEXT_LIMIT} characters.')
        if language not in (None, 'auto', 'en', 'ta', 'ml', 'te'):
            raise ConsoleError(422, 'office_language', 'Choose English, Tamil, Malayalam, Telugu or automatic.')
        with self.lock:
            if self.process is None or not self.process.alive():
                raise ConsoleError(409, 'office_off', 'Start the language service first.')
            reasons = self.blocks(self.mode)
            if reasons:
                raise ConsoleError(409, 'office_blocked', ' '.join(reasons))
            try:
                requester = self.context.requester(requester_id)
                place = place or requester['place']
                self.context.place(place)
                names = self.context.names(requester_place=place)
            except OfficeConfigError as exc:
                raise ConsoleError(422, 'office_requester', str(exc)) from exc
            job_id = 'job_' + secrets.token_hex(6)
            self.jobs[job_id] = {'job_id': job_id, 'text': text.strip(), 'requester': requester['id'],
                                 'language': None if language in (None, 'auto') else language,
                                 'place': place, 'place_label': self.context.label(place),
                                 'state': 'submitted', 'decision': None, 'readback': None, 'steps': None,
                                 'english': None, 'outcomes': [], 'answers': [], 'submitted_at': _now_text()}
            while len(self.jobs) > 40:
                self.jobs.popitem(last=False)
            self._record({'kind': 'request', 'job_id': job_id, 'requester': requester['id'], 'text': text.strip()})
            self.process.send({'type': 'submit', 'job_id': job_id, 'text': text.strip(),
                               'requester': {'id': requester['id'], 'name': requester['name'],
                                             'language': requester['language']},
                               'language': self.jobs[job_id]['language'], 'names': names})
        self._publish()
        return {'job_id': job_id}

    def reply(self, input_id, text):
        if not isinstance(text, str) or len(text) > 2000 or CONTROL.search(text):
            raise ConsoleError(422, 'office_reply', 'A reply is plain text of at most 2000 characters.')
        with self.lock:
            if not self.awaiting or self.awaiting.get('input_id') != input_id:
                raise ConsoleError(409, 'office_reply_stale', 'That question has already been answered or has expired.')
            self.process.send({'type': 'input', 'input_id': input_id, 'text': text})
            self._record({'kind': 'reply', 'job_id': self.awaiting.get('job_id'),
                          'audience': self.awaiting.get('audience'), 'text': text})
            self.awaiting = None
        self._publish()
        return {'ok': True}

    def cancel(self):
        with self.lock:
            alive = self.process is not None and self.process.alive()
            # A drive keeps its Cancel even when the language worker has died under it.
            if not alive and self.moving is None:
                raise ConsoleError(409, 'office_off', 'The language service is off.')
            self.cancel_epoch += 1
            if alive:
                try:
                    self.process.send({'type': 'cancel', 'epoch': self.cancel_epoch})
                except RuntimeError:
                    pass
            self._record({'kind': 'cancel', 'text': 'Cancel pressed: the current task and the queue are cancelled.'})
        self._cancel_motion()
        self._publish()
        return {'ok': True}

    def transcribe(self, raw, language, hint=None):
        """Write a short recording down for the page's text box. Never submits anything."""
        if language not in ('auto', *office_context.LANGUAGES) or hint not in (None, '', *office_context.LANGUAGES):
            raise ConsoleError(422, 'speech_language', 'Choose automatic, English, Tamil, Malayalam or Telugu.')
        with self.lock:
            if self.process is None or not self.process.alive() or self.service.get('state') != 'ready':
                raise ConsoleError(409, 'office_off', 'Start the language service first.')
            speech = self.speech
            fallback = self.context.config['operator'].get('language', 'en') if self.context else 'en'
        if speech is None:
            raise ConsoleError(409, 'speech_off', 'Speech input is not installed on this computer. Type the request instead.')
        try:
            return speech.transcribe(raw, language, hint or None, fallback)
        except office_speech.SpeechError as exc:
            raise ConsoleError(exc.status, exc.code, exc.reason) from exc

    def _cancel_motion(self):
        # Every Cancel advances the console's cancel generation, so a drive that was already
        # requested can never start afterwards; a drive in progress stops where it is.
        if self.mode == 'robot':
            try:
                self.backend.office_cancel_goal()
            except Exception as exc:
                self._record({'kind': 'alarm', 'text': f'The goal cancel request failed: {exc}. Press STOP.'})

    # -- session hooks -------------------------------------------------------------------
    def on_session_stop(self):
        """STOP pressed: the session ends; end the conversation as well (robot mode)."""
        with self.lock:
            if self.mode != 'robot' or self.process is None or not self.process.alive():
                return
            self.cancel_epoch += 1
            try:
                self.process.send({'type': 'stop', 'epoch': self.cancel_epoch})
            except RuntimeError:
                pass
            self._record({'kind': 'stop', 'text': 'STOP: the office task was stopped with the session.'})
        self._publish()

    def on_session_end(self):
        with self.lock:
            if self.mode == 'robot':
                self.position = None
        self.on_session_stop()

    # -- worker events -------------------------------------------------------------------
    def _on_exit(self, code):
        speech, unexpected = None, False
        with self.lock:
            if self.service.get('state') not in ('off', 'stopping'):
                unexpected = True
                self.service = {'state': 'error', 'detail': f'The language worker exited (code {code}).'}
                self._record({'kind': 'alarm', 'text': 'The language worker stopped unexpectedly; any office drive was cancelled.'})
            if self.service.get('state') == 'error':
                self.process = None
                speech, self.speech = self.speech, None
            self.awaiting = None
            for job in self.jobs.values():
                if job['state'] in ACTIVE:
                    job['state'] = 'failed'
        if unexpected:
            # Nobody is left to finish the conversation: the robot must not keep driving for it.
            self._cancel_motion()
        if speech is not None:
            # The speech worker belongs to this run of the service: end it as well.
            threading.Thread(target=speech.stop, daemon=True).start()
        self._publish()

    def _on_event(self, event):
        kind = event.get('type')
        with self.lock:
            if kind == 'service':
                state = event.get('state')
                if state in ('starting', 'ready', 'error', 'off'):
                    self.service = {key: event[key] for key in ('state', 'detail', 'code', 'model', 'runner',
                                                                'fully_on_gpu', 'version', 'mode') if key in event}
                    self._record({'kind': 'service', 'text': event.get('detail', state)})
            elif kind == 'job':
                job = self.jobs.get(event.get('job_id'))
                if job is not None:
                    job['state'] = event.get('state', job['state'])
                    if event.get('decision'):
                        job['decision'] = event['decision']
                    if event.get('position'):
                        job['position'] = event['position']
            elif kind == 'office_event':
                self._office_event(event)
            elif kind == 'awaiting_input':
                self.awaiting = {key: event.get(key) for key in ('input_id', 'job_id', 'prompt', 'expect', 'audience',
                                                                 'place', 'wait_s', 'last_event')}
                if self.context and event.get('place'):
                    self.awaiting['place_label'] = self.context.label(event['place'])
            elif kind in ('input_received', 'input_timeout'):
                if self.awaiting and self.awaiting.get('input_id') == event.get('input_id'):
                    self.awaiting = None
                if kind == 'input_timeout':
                    self._record({'kind': 'timeout', 'job_id': event.get('job_id'),
                                  'text': f"No reply within {int(event.get('waited_s', 0))} s."})
            elif kind == 'navigate':
                self._start_navigation(event)
            elif kind == 'navigate_simulated':
                self.position = event.get('place')
                label = self.context.label(event['place']) if self.context and event.get('place') else 'unknown stop'
                self._record({'kind': 'move', 'job_id': event.get('job_id'), 'text': f'(no robot) moved to {label}',
                              'purpose': event.get('purpose')})
            elif kind == 'session':
                # With the robot, only the console knows where it really stopped (drive results);
                # without it, the worker's simulated position is the only one there is.
                if self.mode != 'robot':
                    self.position = event.get('position', self.position)
                self.awaiting = None
            elif kind in ('log', 'refused', 'cancelling'):
                self._record({'kind': 'note', 'level': event.get('level', 'info'),
                              'text': event.get('text') or event.get('reason') or kind})
        self._publish()

    def _office_event(self, event):
        job = self.jobs.get(event.get('job_id'))
        data = event.get('data') or {}
        kind = event.get('kind')
        if kind == 'robot':
            self._record({'kind': 'robot', 'job_id': event.get('job_id'), 'event': data.get('event'),
                          'language': data.get('language'), 'text': data.get('text')})
            if job is not None and data.get('event') == 'confirm_plan':
                job['readback'] = data.get('text')
        elif kind == 'screen':
            text = data.get('text', '')
            if text and text != 'Thinking…' and not text.startswith('Provisional adaptive agreement'):
                self._record({'kind': 'screen', 'job_id': event.get('job_id'), 'text': text})
        elif kind == 'understand' and job is not None:
            result = data.get('result') or {}
            job.update(english=result.get('english'), steps=result.get('steps'), decision=result.get('decision'),
                       readback=result.get('readback'), planner=result.get('planner'),
                       latency_ms=result.get('latency_ms'), language_detected=result.get('language'),
                       ask=result.get('ask'), refuse=result.get('refuse'),
                       queued_parts=len(result.get('queued_parts') or []), omissions=result.get('omissions'))
        elif kind == 'follow_up_request' and isinstance(data.get('job_id'), str):
            place = data.get('place')
            self.jobs[data['job_id']] = {'job_id': data['job_id'], 'text': str(data.get('text', '')),
                                         'requester': data.get('requester'), 'language': None, 'place': place,
                                         'place_label': self.context.label(place) if self.context and place else None,
                                         'state': 'queued', 'decision': None, 'readback': None, 'steps': None,
                                         'english': None, 'outcomes': [], 'answers': [], 'submitted_at': _now_text(),
                                         'follow_up_of': data.get('after'), 'follow_up_depth': data.get('depth')}
            self._record({'kind': 'request', 'job_id': data['job_id'], 'requester': data.get('requester'),
                          'text': str(data.get('text', '')), 'follow_up': True})
        elif kind == 'correction' and job is not None:
            # The requester changed the request at the read-back: the task carries the new words.
            job['text'] = str(data.get('text', ''))
            self._record({'kind': 'request', 'job_id': event.get('job_id'), 'requester': job.get('requester'),
                          'text': job['text'], 'correction': True})
        elif kind == 'outcome' and job is not None:
            job['outcomes'].append(data.get('outcome'))
        elif kind == 'answer_verification' and job is not None and data.get('confirmed'):
            job['answers'].append(data.get('exact_answer'))
        elif kind in ('session_stopped', 'session_finished', 'session_deferred'):
            self._record({'kind': 'note', 'text': {'session_stopped': 'The task was stopped.',
                                                   'session_finished': 'All tasks finished.',
                                                   'session_deferred': 'A step was left for later.'}[kind]})

    # -- robot moves ---------------------------------------------------------------------
    def _start_navigation(self, event):
        place = event.get('place')
        label = self.context.label(place) if self.context and isinstance(place, str) else str(place)
        epoch = event.get('cancel_epoch')
        if type(epoch) is not int or epoch < self.cancel_epoch:
            # Asked for before the latest Cancel or STOP: never started.
            self._record({'kind': 'note', 'job_id': event.get('job_id'),
                          'text': f'A drive to {label} asked for before Cancel was not started.'})
            try:
                self.process.send({'type': 'navigate_result', 'nav_id': event.get('nav_id'), 'ok': False,
                                   'status': 'CANCELED', 'detail': 'cancelled before the drive started'})
            except (RuntimeError, AttributeError):
                pass
            return
        # The engine refuses the drive if any Cancel happens after this point and before it is sent.
        event['expected_generation'] = getattr(self.backend, 'office_cancel_generation', None)
        self.moving = {'nav_id': event.get('nav_id'), 'job_id': event.get('job_id'), 'place': place,
                       'label': label, 'purpose': event.get('purpose'), 'since': _now_text()}
        self._record({'kind': 'move', 'job_id': event.get('job_id'), 'text': f'Driving to {label}',
                      'purpose': event.get('purpose')})
        thread = threading.Thread(target=self._navigate, args=(dict(event),), daemon=True)
        self.navigation_threads = [t for t in self.navigation_threads if t.is_alive()] + [thread]
        thread.start()

    def _navigate(self, event):
        result = {'ok': False, 'status': 'NOT_SENT'}
        try:
            if self.mode != 'robot':
                raise ConsoleError(409, 'office_mode', 'Robot moves need robot mode.')
            # Every reason counts, including a language service that is no longer ready.
            reasons = self.blocks('robot')
            if self.process is None or not self.process.alive():
                reasons.append('The language worker is not running.')
            if reasons:
                raise ConsoleError(409, 'office_blocked', ' '.join(reasons))
            pose = self.context.place(event['place'])
            outcome = self.backend.office_navigate(pose, map_id=self.context.map_id, job_id=event.get('job_id'),
                                                   nav_id=event.get('nav_id'), purpose=event.get('purpose'),
                                                   expected_generation=event.get('expected_generation'))
            status = (outcome.get('nav2') or {}).get('status') or outcome.get('navigation_outcome')
            result = {'ok': status == 'SUCCEEDED', 'status': status,
                      'true_final_error_m': outcome.get('true_final_error_m'),
                      'navigation_wall_s': (outcome.get('nav2') or {}).get('navigation_wall_s'),
                      'detail': outcome.get('error')}
        except ConsoleError as exc:
            result = {'ok': False, 'status': 'CANCELED' if exc.code == 'office_cancelled' else 'REFUSED',
                      'detail': exc.reason}
        except Exception as exc:
            result = {'ok': False, 'status': 'ERROR', 'detail': str(exc)}
        with self.lock:
            if self.moving and self.moving.get('nav_id') == event.get('nav_id'):
                self.moving = None
            self.position = event.get('place') if result['ok'] else None
            label = self.context.label(event['place']) if self.context and isinstance(event.get('place'), str) else '?'
            text = (f'Arrived at {label}' if result['ok'] else
                    f"Could not reach {label}: {result.get('detail') or str(result.get('status')).lower()}")
            self._record({'kind': 'arrived' if result['ok'] else 'alarm', 'job_id': event.get('job_id'), 'text': text,
                          'final_error_m': result.get('true_final_error_m')})
            try:
                if self.process is not None:
                    self.process.send({'type': 'navigate_result', 'nav_id': event.get('nav_id'), **result})
            except RuntimeError:
                pass
        self._publish()
