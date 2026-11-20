"""Harmless real child processes, enabled only by the suite's shared guard."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_support
from supervision import OwnedLaunch, finish_tracking, stop_owned


def setUpModule():
    test_support.assert_guard_active()


HELPER = r'''
import json, signal, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from supervision import OwnedLaunch, enable_subreaper, stop_owned, full_inventory
enable_subreaper()
def interrupt(_sig, _frame):
    raise KeyboardInterrupt()
signal.signal(signal.SIGTERM, interrupt)
kind = sys.argv[2]
if kind == 'foreign':
    argv = ['bash', '-c', 'exec -a planner_server sleep 60']
elif kind == 'coordinator':
    counter = sys.argv[3]
    code = """import signal,sys,time
def interrupted(_sig,_frame):
    with open(sys.argv[1],'a') as stream: stream.write('SIGINT\\n')
    raise SystemExit(0)
signal.signal(signal.SIGINT,interrupted)
print('ready',flush=True)
while True: time.sleep(.1)
"""
    parent = """import os,signal,subprocess,sys,time
child = subprocess.Popen([sys.executable,'-c',CODE,COUNTER],stdout=subprocess.PIPE,text=True)
assert child.stdout.readline().strip() == 'ready'
def interrupted(_sig,_frame):
    # Model a coordinator forwarding after its same-group child would already
    # have received a direct group INT: that path must never be used here.
    time.sleep(.1)
    os.kill(child.pid,signal.SIGINT)
    child.wait(timeout=5)
    raise SystemExit(0)
signal.signal(signal.SIGINT,interrupted)
print('child-ready',flush=True)
while True: time.sleep(.1)
""".replace('CODE', repr(code)).replace('COUNTER', repr(counter))
    argv = [sys.executable, '-c', parent]
else:
    code = 'import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    # Both the leader and its detached descendant resist INT/TERM.
    parent = 'import subprocess,sys,signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);subprocess.Popen([sys.executable,"-c",'+repr(code)+'],start_new_session=True);print("child-ready",flush=True);time.sleep(60)'
    argv = [sys.executable, '-c', parent]
owned = OwnedLaunch.start(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, start_new_session=True, text=True)
try:
    if kind != 'foreign':
        assert owned.proc.stdout.readline().strip() == 'child-ready'
        deadline = time.monotonic()+5
        while len(owned.refresh()) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(owned.refresh()) >= 2
    else:
        time.sleep(.1)
    before = full_inventory(owned)
finally:
    report, forced, errors = stop_owned(owned, launch_leader_only=kind == 'coordinator',
        interrupt=2 if kind == 'coordinator' else .2, terminate=.2)
after = full_inventory(owned)
print(json.dumps(dict(report=report,forced=forced,errors=errors,before=before,after=after,
                     launch_group=owned.proc.pid)),flush=True)
'''


class RealSupervisorBoundaryTests(unittest.TestCase):
    def helper(self, kind, counter=''):
        test_support.assert_guard_active()
        tracker = OwnedLaunch.start([sys.executable, '-c', HELPER,
            str(Path(__file__).resolve().parents[1]), kind, counter], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        helper = tracker.proc
        try:
            stdout, stderr = helper.communicate(timeout=15)
            self.assertEqual(helper.returncode, 0, stderr)
            return json.loads(stdout.splitlines()[-1])
        finally:
            if helper.poll() is None:
                # Preserve the test's own PID/start-tick tracker if the private
                # helper fails, so detached fixtures cannot be orphaned.
                stop_owned(tracker, interrupt=.5, terminate=.5)
            else:
                finish_tracking(tracker)

    def test_group_int_precedes_detached_owned_term_kill(self):
        result = self.helper('descendant')
        self.assertTrue(result['forced'])
        self.assertEqual(result['report']['live_survivors'], [])
        records = result['report']['tracked_processes']
        self.assertGreaterEqual(len(records), 2)
        for record in records:
            signals = [item['signal'] for item in result['report']['signals'] if item['pid'] == record['pid']]
            expected = ['SIGINT', 'SIGTERM', 'SIGKILL'] if record['pgid'] == result['launch_group'] else ['SIGTERM', 'SIGKILL']
            self.assertEqual(signals, expected)

    def test_ros_coordinator_receives_one_int_and_forwards_once(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / 'child_signals.txt'
            result = self.helper('coordinator', str(counter))
            self.assertEqual(counter.read_text().splitlines(), ['SIGINT'])
            self.assertEqual(result['report']['launch_returncode'], 0)
            self.assertFalse(result['forced'])
            self.assertEqual(result['report']['live_survivors'], [])
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['report']['signals'],
                [{'pid': result['launch_group'], 'signal': 'SIGINT'}])
            self.assertEqual(result['report']['initial_signal_policy'],
                             'manual_ros_launch_leader_only_sigint_10s')

    def test_unrelated_http_side_child_is_reported_and_never_killed(self):
        test_support.assert_guard_active()
        foreign = subprocess.Popen(['bash', '-c', 'exec -a rviz2 sleep 60'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            time.sleep(.1)
            result = self.helper('foreign')
            self.assertIsNone(foreign.poll())
            self.assertTrue(any(item['pid'] == foreign.pid and not item['owned'] for item in result['after']))
            self.assertFalse(any(item['pid'] == foreign.pid for item in result['report']['tracked_processes']))
            self.assertFalse(any(item['pid'] == foreign.pid for item in result['report']['signals']))
            self.assertEqual(result['report']['live_survivors'], [])
        finally:
            # The test owns this fixture; the session supervisor never does.
            foreign.terminate()
            foreign.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
