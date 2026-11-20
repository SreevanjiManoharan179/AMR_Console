"""Arrange only windows identified by the existing owned-window helper.

Layout (the user's choice, 18 Oct 2026): Gazebo at the left-top and RViz at
the left-bottom, each half the screen wide, leaving the right half free for the
console. The arithmetic is the user's tested wmctrl command of that day.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

LAYOUT = 'left-stack'


def save_private_rviz(owned, log_dir):
    """Use the established runner helper on this session's disposable copy."""
    from window_capture import VisualCapture
    log_dir = Path(log_dir)
    capture = VisualCapture(log_dir, owned, 'AMR console manual shutdown')
    return capture.save_rviz_config(log_dir / 'rviz_config.rviz')


def layout_geometry(width, height, layout=LAYOUT):
    """(x, y, w, h) for the owned Gazebo and RViz main windows."""
    half = width // 2
    if layout == 'left-stack':
        h = max(100, (height - 90) // 2)
        return {'gazebo': (0, 30, half, h), 'rviz': (0, 60 + h, half, h)}
    if layout == 'left-right':
        h = max(100, height - 70)
        return {'gazebo': (0, 30, half, h), 'rviz': (half, 30, half, h)}
    raise ValueError(f'Unknown window layout {layout!r}.')


def owned_window_pair(owned):
    """IDs of the owned Gazebo and RViz main windows, or None until both exist."""
    from window_capture import owned_windows
    windows = owned_windows(owned)
    if 'gazebo' in windows and 'rviz' in windows:
        return windows['gazebo']['id'], windows['rviz']['id']
    return None


def arrange_owned(owned, layout=LAYOUT):
    if os.environ.get('XDG_SESSION_TYPE') != 'x11':
        return {'supported': False, 'reason': 'Window arranging requires X11.'}
    tool = shutil.which('wmctrl') or shutil.which('xdotool')
    if not tool:
        return {'supported': False, 'reason': "wmctrl and xdotool aren't installed."}
    from window_capture import owned_windows
    windows = owned_windows(owned)
    if set(windows) != {'gazebo', 'rviz'}:
        raise RuntimeError('Both owned Gazebo and RViz main windows must be open.')
    screen = subprocess.check_output(['xwininfo', '-root'], text=True, timeout=5)
    width = int(re.search(r'Width:\s*(\d+)', screen).group(1))
    height = int(re.search(r'Height:\s*(\d+)', screen).group(1))
    geometry = layout_geometry(width, height, layout)
    live = {(rec['pid'], rec['start_ticks']) for rec in owned.refresh()}
    for name in ('gazebo', 'rviz'):
        window = windows[name]
        if (window['pid'], window['start_ticks']) not in live:
            raise RuntimeError('The owned window disappeared; try arranging again.')
        window_id = window['id']
        x, y, w, h = geometry[name]
        if tool.endswith('wmctrl'):
            commands = [[tool, '-i', '-r', window_id, '-b', 'remove,maximized_vert,maximized_horz'],
                        [tool, '-i', '-r', window_id, '-e', f'0,{x},{y},{w},{h}']]
        else:
            commands = [[tool, 'windowmove', window_id, str(x), str(y)],
                        [tool, 'windowsize', window_id, str(w), str(h)]]
        for argv in commands:
            subprocess.run(argv, check=True, capture_output=True, timeout=5)
    return {'supported': True, 'layout': layout}
