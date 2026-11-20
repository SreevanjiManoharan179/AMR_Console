"""Linux ownership tracking for launch children, including detached Gazebo forks.

No ROS imports or process-name kills. PID start ticks prevent PID reuse from
turning a recorded child into an unrelated process at shutdown.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def process_table():
    records = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            pid = int(path.name)
            records[pid] = {'pid': pid, 'state': fields[0], 'ppid': int(fields[1]),
                            'pgid': int(fields[2]), 'session': int(fields[3]),
                            'start_ticks': int(fields[19])}
        except (OSError, ValueError, IndexError):
            continue
    return records


def enable_subreaper():
    # Orphaned grandchildren are adopted by this runner instead of the desktop
    # session. This applies only to this process and its descendants.
    lib = ctypes.CDLL(None, use_errno=True)
    if lib.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Cannot enable child subreaper')


class OwnedLaunch:
    """Track one launch while it runs; clean it even if its leader exits early."""
    @classmethod
    def start(cls, *args, **kwargs):
        # Snapshot before Popen: even a child orphaned immediately at startup
        # is new to this run, rather than incorrectly classified as preexisting.
        owner = os.getpid()
        existing = {pid for pid, r in process_table().items() if r['ppid'] == owner}
        return cls(subprocess.Popen(*args, **kwargs), existing)

    def __init__(self, proc, existing_children):
        self.proc = proc
        self.owner = os.getpid()
        self.records = {}
        self.signals = []
        self.done = None
        self.lock = threading.Lock()
        self.end = threading.Event()
        # A session has one launch at a time. Other direct children already
        # present here are excluded (e.g. an unrelated test fixture).
        self.excluded = set(existing_children)
        self.refresh()
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.thread.start()

    def _monitor(self):
        while not self.end.wait(.05):
            self.refresh()

    def refresh(self):
        table = process_table()
        with self.lock:
            roots = {pid for pid, old in self.records.items()
                     if pid in table and table[pid]['start_ticks'] == old['start_ticks']}
            initial = self.records.get(self.proc.pid)
            if self.proc.pid in table and (initial is None or
                    table[self.proc.pid]['start_ticks'] == initial['start_ticks']):
                roots.add(self.proc.pid)
            # The subreaper catches even children whose parents exit before a
            # polling interval. All newly adopted children belong to this run.
            roots.update(pid for pid, r in table.items()
                         if r['ppid'] == self.owner and pid not in self.excluded)
            changed = True
            while changed:
                children = {pid for pid, r in table.items() if r['ppid'] in roots}
                changed = not children.issubset(roots)
                roots.update(children)
            for pid in roots:
                self.records.setdefault(pid, table[pid].copy())
            return [r.copy() for pid, r in table.items()
                    if pid in self.records and r['start_ticks'] == self.records[pid]['start_ticks']
                    and r['state'] != 'Z']

    def _signal(self, pid, sig):
        current = process_table().get(pid)
        old = self.records.get(pid)
        if current and old and current['start_ticks'] == old['start_ticks'] and current['state'] != 'Z':
            try:
                os.kill(pid, sig)
                self.signals.append({'pid': pid, 'signal': sig.name})
            except ProcessLookupError:
                pass

    def _wait(self, seconds):
        end = time.monotonic() + seconds
        while self.refresh() and time.monotonic() < end:
            time.sleep(.05)

    def shutdown(self):
        if self.done is not None:
            return self.done
        try:
            self.refresh()
            if self.proc.poll() is None:
                self._signal(self.proc.pid, signal.SIGINT)
                # Let launch's default 5s TERM + 5s KILL escalation and reporting
                # finish before fallback cleanup; a second SIGINT can cut it off.
                end = time.monotonic() + 20
                while self.proc.poll() is None and time.monotonic() < end:
                    time.sleep(.05)
            # Gazebo can create new sessions. A launch group alone does not own
            # those groups; signal only recorded descendants, never by name.
            for sig, seconds in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
                live = self.refresh()
                if not live:
                    break
                for rec in live:
                    if sig == signal.SIGINT and rec['pid'] == self.proc.pid:
                        continue
                    self._signal(rec['pid'], sig)
                self._wait(seconds)
            self.proc.poll()
            survivors = self.refresh()
            self.end.set()
            self.thread.join(timeout=1)
            with self.lock:
                records = list(self.records.values())
            for rec in records:
                pid = rec['pid']
                if pid == self.proc.pid:
                    continue
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            self.done = {'tracked_processes': records,
                         'signals': self.signals, 'live_survivors': survivors,
                         'launch_returncode': self.proc.poll()}
            return self.done
        finally:
            self.end.set()
            self.thread.join(timeout=1)


def simulation_conflicts():
    """Read command titles only for relevant apps; never read their environment."""
    conflicts = []
    for pid, rec in process_table().items():
        if rec['state'] == 'Z':
            continue
        try:
            comm = Path(f'/proc/{pid}/comm').read_text().strip()
            if comm not in ('ruby', 'gz', 'gz-sim', 'rviz2', 'component_container',
                            'component_conta', 'planner_server', 'controller_serv',
                            'amcl', 'parameter_bridg', 'bt_navigator'):
                continue
            command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
            if comm == 'ruby' and not command.startswith('gz sim '):
                continue
            conflicts.append({'pid': pid, 'command': command})
        except OSError:
            continue
    return conflicts
