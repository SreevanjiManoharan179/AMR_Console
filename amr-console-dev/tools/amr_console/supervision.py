"""Console supervision, reusing the existing process identities and audits.

This module starts no process and imports no ROS libraries. The session worker
is the only caller that adopts children or sends signals.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'src/amr_physical_ai/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from process_ownership import OwnedLaunch, enable_subreaper, process_table  # noqa: F401  (OwnedLaunch, enable_subreaper: re-exported for worker.py)
from transport_isolation import process_inventory


def full_inventory(owned=None):
    """Never reduce the process-isolation inventory to the older, incomplete title filter."""
    return process_inventory(owned)


def finish_tracking(owned):
    """Collect the existing OwnedLaunch record after our contractual timings."""
    owned.proc.poll()
    survivors = owned.refresh()
    owned.end.set()
    owned.thread.join(timeout=1)
    with owned.lock:
        records = list(owned.records.values())
    for record in records:
        if record['pid'] == owned.proc.pid:
            continue
        try:
            os.waitpid(record['pid'], os.WNOHANG)
        except ChildProcessError:
            pass
    result = {'tracked_processes': records, 'signals': list(owned.signals),
              'live_survivors': survivors,
              'launch_returncode': owned.proc.poll()}
    owned.done = result
    return result


def signal_group(owned, sig, *, table_reader=process_table, killer=os.killpg):
    """Signal the launch group only after every live member is proven owned."""
    owned.refresh()
    table = table_reader()
    with owned.lock:
        identities = {(pid, item['start_ticks'])
                      for pid, item in owned.records.items()}
    group = owned.proc.pid  # start_new_session=True makes the leader its PGID.
    members = [item for item in table.values()
               if item['pgid'] == group and item['state'] != 'Z']
    foreign = [item for item in members
               if (item['pid'], item['start_ticks']) not in identities]
    if foreign:
        raise RuntimeError('Refusing to signal a group containing unowned processes: '
                           + ', '.join(str(item['pid']) for item in foreign))
    if members:
        try:
            killer(group, sig)
            owned.signals.extend({'pid': item['pid'], 'signal': sig.name,
                                  'scope': 'owned process group'} for item in members)
        except ProcessLookupError:
            pass


def stop_owned(owned, *, trials=False, launch_leader_only=False,
               graceful=90, interrupt=10, terminate=5,
               sleep=time.sleep, clock=time.monotonic, group_signal=signal_group):
    """Runner-only grace; approved manual launch leader INT; job group INT.

    Any escalation beyond the initial INT is forced. Detached descendants use
    OwnedLaunch._signal, retaining its PID/start-tick reuse protection.
    """
    if trials and launch_leader_only:
        raise ValueError('Trial runner cancellation and manual launch policy are separate.')
    policy = ('trial_runner_only_sigint_90s' if trials else
              'manual_ros_launch_leader_only_sigint_10s' if launch_leader_only else
              'owned_process_group_sigint_10s')
    def finish():
        report = finish_tracking(owned)
        report['initial_signal_policy'] = policy
        return report
    forced = False
    errors = []
    owned.refresh()
    if trials and owned.proc.poll() is not None and owned.refresh():
        # The runner has already returned, so any emergency descendant stop
        # is forced even if its first SIGINT succeeds.
        forced = True
    if trials and owned.proc.poll() is None:
        owned._signal(owned.proc.pid, signal.SIGINT)
        end = clock() + graceful
        while owned.proc.poll() is None and clock() < end:
            owned.refresh()
            sleep(.05)
        if owned.proc.poll() is None:
            forced = True
            owned._signal(owned.proc.pid, signal.SIGTERM)
        elif not owned.refresh():
            return finish(), forced, errors
        else:
            # A runner that exited without clearing its scope failed its own
            # cleanup; emergency descendant cleanup is therefore forced too.
            forced = True

    if owned.refresh():
        if launch_leader_only:
            # ROS launch already forwards INT to every child. Signalling its
            # group as well delivers a second INT to the Gazebo wrapper and
            # races its GUI/server wait/kill sequence (Errno::ESRCH).
            if owned.proc.poll() is None:
                owned._signal(owned.proc.pid, signal.SIGINT)
        else:
            try:
                group_signal(owned, signal.SIGINT)
            except RuntimeError as exc:
                errors.append(str(exc))
                forced = True
        # The approved initial target gets its full grace before any remaining
        # owned descendant receives TERM/KILL, including detached processes.
        end = clock() + interrupt
        while owned.refresh() and clock() < end:
            sleep(.05)
    for sig, seconds in ((signal.SIGTERM, terminate), (signal.SIGKILL, 1)):
        live = owned.refresh()
        if not live:
            break
        forced = True
        for record in live:
            owned._signal(record['pid'], sig)
        end = clock() + seconds
        while owned.refresh() and clock() < end:
            sleep(.05)
    return finish(), forced, errors


class PromptDetector:
    """input() has no newline: match complete known prompts across chunks."""
    import re as _re
    pattern = _re.compile(
        r'Inspect Gazebo/RViz; press Enter to close this trial and continue: |'
        r'Controller complete\. Press Enter to start (?:MPPI|DWB|RPP): ')

    def __init__(self):
        self.tail = ''

    def feed(self, text):
        self.tail += text
        prompts = []
        while True:
            match = self.pattern.search(self.tail)
            if not match:
                self.tail = self.tail[-300:]
                return prompts
            prompts.append(match.group())
            self.tail = self.tail[match.end():]


class ContinueGate:
    """One observed prompt permits precisely one newline, never a queue."""
    def __init__(self):
        self.waiting = False

    def observe(self):
        self.waiting = True

    def send(self, stream):
        if not self.waiting:
            raise RuntimeError('No trial batch is waiting for Continue.')
        self.waiting = False
        stream.write(b'\n')
        stream.flush()
