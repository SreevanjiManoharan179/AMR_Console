from pathlib import Path
import sys
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from window_capture import parse_owned_window, transform_world_pose, recording_capability


def test_window_filter_excludes_dialog_and_unowned():
    normal = '_NET_WM_PID(CARDINAL) = 42\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\nWM_NAME(STRING) = "RViz"'
    live = {42: {'comm': 'rviz2', 'start_ticks': 500}}
    assert parse_owned_window('0x123', normal, live)['start_ticks'] == 500
    assert parse_owned_window('0x123', normal, {}) is None
    assert parse_owned_window('0x123', normal+'\nWM_TRANSIENT_FOR(WINDOW): window id # 0x345', live) is None
    assert parse_owned_window('0x123', normal.replace('TYPE_NORMAL', 'TYPE_DIALOG'), live) is None


def test_gazebo_window_requires_owned_identity_and_title():
    normal = '_NET_WM_PID(CARDINAL) = 42\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\nWM_NAME(STRING) = "Gazebo Sim"'
    assert parse_owned_window('0x123', normal, {42: {'comm': 'ruby', 'start_ticks': 500}})['application'] == 'gazebo'


def test_profile_transform_handles_rotation():
    result = transform_world_pose(1, 0, 0, {'x': 2, 'y': .5, 'yaw': math.pi/2})
    assert abs(result[0]-2) < 1e-9 and abs(result[1]-1.5) < 1e-9
    assert transform_world_pose(-2, -.5, 0, {'x': 2, 'y': .5, 'yaw': 0}) == (0, 0, 0)


def test_recording_reports_missing_dependency_without_install(monkeypatch):
    monkeypatch.setattr('window_capture.shutil.which', lambda _: None)
    result = recording_capability({'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':1'})
    assert not result['available'] and 'ffmpeg' in result['reason']
    assert not recording_capability({'XDG_SESSION_TYPE': 'wayland'})['available']
