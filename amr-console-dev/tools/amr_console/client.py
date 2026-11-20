"""HTTP-side transport to the dedicated session supervisor process."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


class WorkerClient:
    def __init__(self, spec, on_event):
        self.spec = spec
        self.on_event = on_event
        self.proc = None
        self.pid = None
        self.write_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.finished = threading.Event()
        self.cleanup = None

    def start(self):
        path = Path(self.spec['log_dir']) / 'spec.json'
        path.write_text(json.dumps(self.spec, indent=2, allow_nan=False) + '\n')
        env = os.environ.copy()
        env['RMW_IMPLEMENTATION'] = 'rmw_cyclonedds_cpp'
        # Transport policy is a display/record field, never a subprocess variable.
        if self.spec['kind'] == 'trials':
            env.pop('ROS_DOMAIN_ID', None)
            env.pop('GZ_PARTITION', None)
        else:
            for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION'):
                if key in self.spec['env']:
                    env[key] = str(self.spec['env'][key])
        self.proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('worker.py')), '--spec', str(path)],
            cwd=self.spec['root'], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1, start_new_session=True)
        self.pid = self.proc.pid
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _stderr(self):
        for line in self.proc.stderr:
            self._worker_log(line)
            self.on_event({'type': 'log', 'level': 'warn', 'text': line.rstrip()})

    def _worker_log(self, line):
        with self.log_lock:
            with (Path(self.spec['log_dir']) / 'worker.log').open('a') as stream:
                stream.write(line)

    def _read(self):
        for line in self.proc.stdout:
            self._worker_log(line)
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Expected event object')
            except (ValueError, json.JSONDecodeError):
                self.on_event({'type': 'log', 'level': 'warn',
                               'text': 'Supervisor emitted an invalid event; inspect worker.log.'})
                continue
            if event.get('type') == 'finished':
                self.cleanup = event.get('cleanup', event.get('report', {}))
            self.on_event(event)
        code = self.proc.wait()
        if self.cleanup is None:
            self.cleanup = {'survivors': 0, 'forced': False, 'clean': False,
                            'error': f'Supervisor exited {code} without a cleanup certificate.',
                            'exit_code': code}
            self.on_event({'type': 'finished', 'cleanup': self.cleanup, 'exit_code': code})
        self.finished.set()

    def control(self, type_, **payload):
        with self.write_lock:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError('Session supervisor has already exited.')
            self.proc.stdin.write(json.dumps({'type': type_, **payload}, allow_nan=False) + '\n')
            self.proc.stdin.flush()

    def stop(self):
        if not self.finished.is_set():
            self.control('stop')
        # Trial allowance90 + emergency waits15 + audit/window grace; HTTP thread
        # can wait while status/SSE remain responsive in other handlers.
        if not self.finished.wait(155):
            # Never kill the owner of detached descendants from this HTTP process.
            # Keep it alive for tracking and surface the missing certificate.
            raise RuntimeError('Supervisor cleanup did not finish; inspect logs and retry STOP.')
        return self.cleanup
