"""Read-only environment checks and reuse of the existing isolation library."""
import contextlib
import fcntl
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def helpers(root=ROOT):
    path = str(Path(root) / 'src/amr_physical_ai/scripts')
    if path not in sys.path:
        sys.path.insert(0, path)
    import transport_isolation
    return transport_isolation


def inventory(root=ROOT):
    return helpers(root).process_inventory()


def lock_held(root=ROOT):
    path = Path(root) / 'log/simulation.lock'
    if not path.exists():
        return False
    with path.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            # Unlocking an unacquired lock is harmless and never unlocks another fd.
            fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def choose_transport(kind, root=ROOT):
    lib = helpers(root)
    snapshot = lib.transport_snapshot()
    for domain in range(80, 100):
        env = {'ROS_DOMAIN_ID': str(domain),
               'GZ_PARTITION': f'amr_console_{kind}_{domain}'}
        if not lib.transport_collisions(env, snapshot):
            return env
    raise RuntimeError('No unoccupied console ROS domain remains in 80–99.')


def transport_available(env, root=ROOT):
    lib = helpers(root)
    return not lib.transport_collisions(env, lib.transport_snapshot())


def git_commit(root=ROOT):
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return 'unavailable'


def environment(root=ROOT):
    ros_ready = (os.environ.get('ROS_DISTRO') == 'jazzy'
                 and shutil.which('ros2') is not None
                 and importlib.util.find_spec('rclpy') is not None
                 and (Path(root) / 'install/setup.bash').is_file())
    session = os.environ.get('XDG_SESSION_TYPE', 'unknown')
    arranger = shutil.which('wmctrl') or shutil.which('xdotool')
    arrange = session == 'x11' and bool(arranger)
    reason = '' if arrange else (
        'This session is not X11; move Gazebo and RViz by hand.' if session != 'x11'
        else "wmctrl and xdotool aren't installed; move Gazebo and RViz by hand.")
    recording = session == 'x11' and bool(shutil.which('ffmpeg'))
    record_reason = '' if recording else (
        "ffmpeg isn't installed" if not shutil.which('ffmpeg') else 'Screen recording needs X11.')
    # Report the measured workspace baseline without launching a transient gz
    # process that could interfere with a terminal harness's process preflight.
    gazebo = 'unknown'
    ledger = Path(root) / 'data/environment.txt'   # written by setup/check_system.sh
    if ledger.exists():
        match = re.search(r'Gazebo Sim (\d+\.\d+\.\d+)', ledger.read_text())
        if match:
            gazebo = match.group(1)
    return {'ros_distro': os.environ.get('ROS_DISTRO', 'unsourced'),
            'ros_ready': ros_ready,
            'ros_reason': '' if ros_ready else 'Source /opt/ros/jazzy/setup.bash and install/setup.bash before starting the server.',
            'rmw': os.environ.get('RMW_IMPLEMENTATION', 'unset'),
            'gazebo': gazebo, 'gazebo_source': 'recorded by setup/check_system.sh in data/environment.txt',
            'commit': git_commit(root), 'session_type': session,
            'arrange_supported': arrange, 'arrange_reason': reason,
            'recording_supported': recording, 'recording_reason': record_reason}
