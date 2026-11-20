"""Real process-tree regressions; no ROS/Gazebo or desktop applications."""
import os
from pathlib import Path
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import process_ownership as processes


def test_detached_orphan_cleaned_after_leader_exits_unrelated_child_survives():
    processes.enable_subreaper()
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 start_new_session=True)
    child = 'import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); print("READY",flush=True); time.sleep(60)'
    parent = ('import subprocess,sys,time; '
              f'p=subprocess.Popen([sys.executable,"-c",{child!r}],stdout=subprocess.PIPE,text=True,start_new_session=True); '
              'assert p.stdout.readline().strip()=="READY"; print(p.pid,flush=True); time.sleep(.15)')
    owned = processes.OwnedLaunch.start([sys.executable, '-c', parent],
                                       stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        child_pid = int(owned.proc.stdout.readline())
        owned.proc.wait(timeout=5)
        assert os.getsid(child_pid) == child_pid  # detached from launch session
        result = owned.shutdown()
        assert not result['live_survivors']
        assert any(r['pid'] == child_pid for r in result['tracked_processes'])
        assert any(s['pid'] == child_pid and s['signal'] == 'SIGTERM' for s in result['signals'])
        assert unrelated.poll() is None
        assert owned.shutdown() is result  # cleanup can be called twice safely
    finally:
        owned.shutdown()
        unrelated.terminate()
        unrelated.wait(timeout=5)
        owned.proc.stdout.close()


def test_immediate_failed_launch_still_owns_orphan():
    processes.enable_subreaper()
    child = 'import time; time.sleep(60)'
    parent = ('import subprocess,sys; '
              f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
              'print(p.pid,flush=True); sys.exit(1)')
    owned = processes.OwnedLaunch.start([sys.executable, '-c', parent],
                                       stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        child_pid = int(owned.proc.stdout.readline())
        assert owned.proc.wait(timeout=5) == 1
        result = owned.shutdown()
        assert not result['live_survivors']
        assert any(r['pid'] == child_pid for r in result['tracked_processes'])
    finally:
        owned.shutdown()
        owned.proc.stdout.close()


def test_reused_pid_is_never_signalled(monkeypatch):
    owned = object.__new__(processes.OwnedLaunch)
    owned.records = {42: {'start_ticks': 10}}
    owned.signals = []
    monkeypatch.setattr(processes, 'process_table', lambda: {42: {'start_ticks': 11, 'state': 'S'}})
    def forbidden(*args):
        raise AssertionError('Unrelated PID must not receive a signal')
    monkeypatch.setattr(processes.os, 'kill', forbidden)
    owned._signal(42, processes.signal.SIGTERM)
    assert not owned.signals
