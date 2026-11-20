"""Exercise real PID identity/inheritance and fail-stop without ROS or Gazebo."""
import os
from pathlib import Path
import subprocess
import sys
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from transport_isolation import process_inventory, verify_transport
from process_ownership import OwnedLaunch


def test_live_foreign_process_blocks_without_signal_and_owned_transport_checked(tmp_path):
    executable = tmp_path / 'map_server'
    executable.symlink_to(sys.executable)
    ready = tmp_path / 'ready'
    program = ('import signal,time,sys; from pathlib import Path; '
               'signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); '
               f'Path({str(ready)!r}).write_text("ready"); time.sleep(30)')
    env = dict(os.environ, ROS_DOMAIN_ID='97', GZ_PARTITION='isolation_test_no_network',
               ROS_LOCALHOST_ONLY='1', ROS_AUTOMATIC_DISCOVERY_RANGE='LOCALHOST')
    with (tmp_path / 'dummy.log').open('w') as log:
        owned = OwnedLaunch.start([str(executable), '-c', program], stdout=log,
            stderr=subprocess.STDOUT, env=env, start_new_session=True, stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists()
            before = next(p for p in process_inventory() if p['pid'] == owned.proc.pid)
            assert before['owned'] is False
            assert owned.proc.poll() is None
            good = verify_transport(owned, env)
            assert good['ok'], good
            bad = verify_transport(owned, dict(env, GZ_PARTITION='wrong'))
            assert not bad['ok']
        finally:
            cleanup = owned.shutdown()
        assert not cleanup['live_survivors']
        assert cleanup['launch_returncode'] == 0
        assert not any(p['pid'] == owned.proc.pid for p in process_inventory())


