"""Owned-window X11 capture for process-isolated visual trials, independent of ROS callback execution.

Only windows whose PID/start identity belongs to the active launch are used.
The collector exchanges small stage/ack JSON files with this worker; image
encoding, X11 calls and optional video never run in the ROS executor.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time

from PIL import Image


STAGES = ('start', 'mid', 'final')


def transform_world_pose(x, y, yaw, offset):
    """Rigid Gazebo-world -> saved-map transform from the approved profile."""
    angle = float(offset['yaw'])
    c, s = math.cos(angle), math.sin(angle)
    return (c*x-s*y+float(offset['x']), s*x+c*y+float(offset['y']),
            (yaw+angle+math.pi) % (2*math.pi)-math.pi)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def recording_capability(environ=None):
    env = os.environ if environ is None else environ
    session = env.get('XDG_SESSION_TYPE', '')
    if session != 'x11':
        return {'available': False, 'session_type': session or 'unset',
                'reason': 'Screen recording requires the tested X11 session; '
                          'Wayland portal recording is not implemented.'}
    if not env.get('DISPLAY'):
        return {'available': False, 'session_type': session,
                'reason': 'DISPLAY is unavailable in this session.'}
    executable = shutil.which('ffmpeg')
    if not executable:
        return {'available': False, 'session_type': session,
                'reason': 'ffmpeg is not installed/on PATH; no dependency was installed.'}
    return {'available': True, 'session_type': session, 'executable': executable,
            'reason': 'X11 and ffmpeg detected; actual encoding is checked per trial.'}


def parse_owned_window(window, properties, live):
    """Pure filter: managed normal/nontransient and exact live owned PID."""
    pid = re.search(r'^_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)', properties, re.M)
    kind = re.search(r'^_NET_WM_WINDOW_TYPE\(ATOM\)\s*=\s*(.*)', properties, re.M)
    transient = re.search(r'^WM_TRANSIENT_FOR.*?(0x[0-9a-fA-F]+)', properties, re.M)
    if not pid or int(pid.group(1)) not in live or not kind:
        return None
    if kind.group(1).strip() != '_NET_WM_WINDOW_TYPE_NORMAL':
        return None
    if transient and int(transient.group(1), 16):
        return None
    record = live[int(pid.group(1))]
    comm = record['comm']
    if comm == 'rviz2':
        application = 'rviz'
    elif comm in ('ruby', 'gz', 'gz-sim', 'gz sim', 'gz-sim-gui') and re.search(
            r'gazebo|gz[ -]sim', properties, re.I):
        application = 'gazebo'
    else:
        return None
    return {'id': window, 'pid': int(pid.group(1)),
            'start_ticks': record['start_ticks'], 'application': application}


def owned_windows(owned):
    live = {}
    for record in owned.refresh():
        try:
            comm = Path(f'/proc/{record["pid"]}/comm').read_text().strip()
            live[record['pid']] = {**record, 'comm': comm}
        except OSError:
            pass
    clients = subprocess.check_output(['xprop', '-root', '_NET_CLIENT_LIST'],
                                      text=True, timeout=5)
    windows = {}
    for window in dict.fromkeys(re.findall(r'0x[0-9a-fA-F]+', clients)):
        result = subprocess.run(['xprop', '-id', window, '_NET_WM_PID',
                                 '_NET_WM_WINDOW_TYPE', 'WM_TRANSIENT_FOR',
                                 'WM_NAME', 'WM_CLASS'], capture_output=True,
                                text=True, timeout=5)
        value = parse_owned_window(window, result.stdout, live)
        if value and value['application'] not in windows:
            windows[value['application']] = value
    return windows


class _X11:
    def __init__(self):
        self.lib = lib = ctypes.CDLL(ctypes.util.find_library('X11'))
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XFlush.argtypes = lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.display = lib.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('Cannot open the X11 display')
        self.root = lib.XDefaultRootWindow(self.display)

    def activate(self, window):
        class Data(ctypes.Union):
            _fields_ = [('l', ctypes.c_long * 5)]
        class ClientMessage(ctypes.Structure):
            _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong),
                        ('send_event', ctypes.c_int), ('display', ctypes.c_void_p),
                        ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                        ('format', ctypes.c_int), ('data', Data)]
        class Event(ctypes.Union):
            _fields_ = [('client', ClientMessage), ('pad', ctypes.c_long * 24)]
        event = Event()
        event.client.type = 33
        event.client.display = self.display
        event.client.window = int(window, 16)
        event.client.message_type = self.lib.XInternAtom(self.display, b'_NET_ACTIVE_WINDOW', 0)
        event.client.format = 32
        event.client.data.l[0] = 2
        self.lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_long, ctypes.POINTER(Event)]
        self.lib.XSendEvent(self.display, self.root, 0, (1 << 20) | (1 << 19),
                            ctypes.byref(event))
        self.lib.XFlush(self.display)

    def save_shortcut(self, window):
        """Focus the verified owned client, check focus, then send Ctrl+S.

        Qt ignores synthetic XSendEvent shortcuts on this host. XTest sends real
        key transitions; they are issued only after confirming the owned window
        has keyboard focus, and all pressed keys are released in finally.
        """
        self.activate(window)
        time.sleep(.2)
        lib = self.lib
        lib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        lib.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XKeysymToKeycode.restype = ctypes.c_uint
        lib.XSetInputFocus(self.display, int(window, 16), 2, 0)
        lib.XSync(self.display, 0)
        focus, revert = ctypes.c_ulong(), ctypes.c_int()
        lib.XGetInputFocus(self.display, ctypes.byref(focus), ctypes.byref(revert))
        if focus.value != int(window, 16):
            raise RuntimeError('Owned RViz did not obtain keyboard focus; no keys sent')
        xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
        xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        ctrl = lib.XKeysymToKeycode(self.display, 0xffe3)
        key = lib.XKeysymToKeycode(self.display, ord('s'))
        try:
            xtst.XTestFakeKeyEvent(self.display, ctrl, 1, 0)
            xtst.XTestFakeKeyEvent(self.display, key, 1, 0)
        finally:
            xtst.XTestFakeKeyEvent(self.display, key, 0, 0)
            xtst.XTestFakeKeyEvent(self.display, ctrl, 0, 0)
            lib.XSync(self.display, 0)

    def close(self):
        self.lib.XCloseDisplay(self.display)


def window_geometry(window):
    result = subprocess.check_output(['xwininfo', '-id', window], text=True, timeout=5)
    def number(label):
        match = re.search(re.escape(label) + r':\s*(-?\d+)', result)
        if not match:
            raise RuntimeError(f'Missing {label} for owned window {window}')
        return int(match.group(1))
    return {name: number(label) for name, label in (
        ('x', 'Absolute upper-left X'), ('y', 'Absolute upper-left Y'),
        ('width', 'Width'), ('height', 'Height'))}


class VisualCapture:
    def __init__(self, trial_dir, owned, label, record=False):
        self.trial_dir = Path(trial_dir)
        self.owned, self.label = owned, label
        self.events = self.trial_dir / 'capture_events'
        self.screenshots = self.trial_dir / 'screenshots'
        self.events.mkdir(parents=True, exist_ok=True)
        self.screenshots.mkdir(parents=True, exist_ok=True)
        self.end = threading.Event()
        self.thread = None
        self.completed = set()
        self.video = None
        self.video_log = None
        self.report = {'label': label, 'screenshots': [], 'errors': [],
                       'record_requested': record,
                       'recording': recording_capability(), 'complete': False}

    def start(self):
        if os.environ.get('XDG_SESSION_TYPE') != 'x11':
            raise RuntimeError('Owned-window screenshots require the tested X11 session')
        for executable in ('xprop', 'xwininfo', 'import'):
            if not shutil.which(executable):
                raise RuntimeError(f'{executable} unavailable; no package installed')
        if self.report['record_requested'] and not self.report['recording']['available']:
            print('Video unavailable: ' + self.report['recording']['reason'], flush=True)
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        return self

    def _current(self, window):
        return any(record['pid'] == window['pid'] and
                   record['start_ticks'] == window['start_ticks']
                   for record in self.owned.refresh())

    def _windows(self, timeout=12):
        end = time.monotonic() + timeout
        while True:
            windows = owned_windows(self.owned)
            if set(windows) == {'rviz', 'gazebo'}:
                return windows
            if time.monotonic() >= end:
                raise RuntimeError(f'Both owned GUI windows required, found {sorted(windows)}')
            time.sleep(.1)

    def _capture(self, stage, event):
        windows = self._windows()
        for application, window in windows.items():
            if not self._current(window):
                raise RuntimeError('Window process disappeared before capture')
            title = f'{self.label} | {stage} | {application}'
            subprocess.run(['xprop', '-id', window['id'], '-f', '_NET_WM_NAME', '8u',
                            '-set', '_NET_WM_NAME', title], check=True,
                           capture_output=True, timeout=5)
            display = _X11()
            try:
                display.activate(window['id'])
                time.sleep(.2)
                geometry = window_geometry(window['id'])
                if not self._current(window):
                    raise RuntimeError('Window ownership expired during capture')
                path = self.screenshots / f'{stage}_{application}.png'
                subprocess.run(['import', '-silent', '-window', window['id'], str(path)],
                               check=True, capture_output=True, timeout=10)
            finally:
                display.close()
            with Image.open(path) as image:
                image.load()
                size = image.size
                if size[0] < 100 or size[1] < 100:
                    raise RuntimeError(f'Implausibly small {application} capture: {size}')
            self.report['screenshots'].append({
                'stage': stage, 'application': application,
                'path': str(path.relative_to(self.trial_dir)),
                'utc': datetime.now(timezone.utc).isoformat(),
                'monotonic': time.monotonic(), 'collector_event': event,
                'window': window, 'geometry': geometry, 'size': list(size),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        if stage == 'start' and self.report['record_requested']:
            self._start_video(windows)

    def _start_video(self, windows):
        capability = self.report['recording']
        if not capability['available']:
            return
        # One exact owned RViz window; no full-desktop recording or unrelated apps.
        window = windows['rviz']
        if not self._current(window):
            raise RuntimeError('RViz ownership expired before recording')
        path = self.trial_dir / 'rviz_screen.mkv'
        self.video_log = (self.trial_dir / 'video.log').open('w')
        command = [capability['executable'], '-nostdin', '-loglevel', 'warning',
            '-f', 'x11grab', '-framerate', '10', '-window_id', str(int(window['id'], 16)),
            '-i', os.environ['DISPLAY'], '-an', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-crf', '26', '-pix_fmt', 'yuv420p', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
            str(path)]
        self.video = subprocess.Popen(command, stdout=self.video_log, stderr=self.video_log,
                                      stdin=subprocess.DEVNULL)
        time.sleep(.2)
        if self.video.poll() is not None:
            capability.update(available=False, reason='ffmpeg failed; inspect video.log',
                              returncode=self.video.returncode)
            print('Video unavailable: ' + capability['reason'], flush=True)
        else:
            capability.update(path=str(path.relative_to(self.trial_dir)),
                              window=window, scope='Owned RViz window only, 10 fps')

    def _worker(self):
        while not self.end.wait(.05):
            for stage in STAGES:
                event_file = self.events / f'{stage}.json'
                if stage in self.completed or not event_file.exists():
                    continue
                try:
                    event = json.loads(event_file.read_text())
                    self._capture(stage, event)
                    ack = {'complete': True, 'stage': stage}
                except Exception as exc:
                    ack = {'complete': False, 'stage': stage, 'error': str(exc)}
                    self.report['errors'].append(ack)
                self.completed.add(stage)
                write_json(self.events / f'{stage}_ack.json', ack)
                write_json(self.trial_dir / 'capture.json', self.report)

    def finish(self, timeout=35):
        deadline = time.monotonic() + timeout
        requested = {stage for stage in STAGES if (self.events / f'{stage}.json').exists()}
        while self.thread and not requested.issubset(self.completed) and time.monotonic() < deadline:
            time.sleep(.05)
        self.end.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.video is not None and self.video.poll() is None:
            self.video.send_signal(signal.SIGINT)
            try:
                self.video.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.video.terminate()
                self.video.wait(timeout=3)
            self.report['recording']['returncode'] = self.video.returncode
            path = self.trial_dir / 'rviz_screen.mkv'
            self.report['recording']['completed'] = self.video.returncode == 0 and path.exists() and path.stat().st_size > 0
        if self.video_log:
            self.video_log.close()
        missing = sorted(set(STAGES) - self.completed)
        self.report['missing_stages'] = missing
        self.report['complete'] = not missing and not self.report['errors'] and not (self.thread and self.thread.is_alive())
        write_json(self.trial_dir / 'capture.json', self.report)
        return self.report

    def save_rviz_config(self, config_path, timeout=4):
        """Save only the runner's disposable per-trial RViz configuration."""
        path = Path(config_path).resolve()
        if not path.is_relative_to(self.trial_dir.resolve()) or not path.is_file():
            raise ValueError('RViz save is restricted to an existing file inside this trial')
        windows = owned_windows(self.owned)
        window = windows.get('rviz')
        if not window or not self._current(window):
            raise RuntimeError('No live owned RViz main window to save')
        before = path.stat().st_mtime_ns
        display = _X11()
        try:
            display.save_shortcut(window['id'])
        finally:
            display.close()
        end = time.monotonic() + timeout
        while path.stat().st_mtime_ns == before and time.monotonic() < end:
            time.sleep(.05)
        result = {'path': str(path), 'window': window,
                  'saved': path.stat().st_mtime_ns != before}
        write_json(self.trial_dir / 'rviz_save.json', result)
        return result
