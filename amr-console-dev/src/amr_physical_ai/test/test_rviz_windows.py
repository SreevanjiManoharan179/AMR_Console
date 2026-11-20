"""Window selection/wait regressions without opening or controlling a desktop."""
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rviz_windows as windows


def test_only_one_owned_normal_nontransient_client_is_closed(monkeypatch):
    properties = {
        '0x1': (42, 'NORMAL', None),
        '0x2': (42, 'NORMAL', None),  # duplicate main for the same process
        '0x3': (42, 'DIALOG', '0x1'),
        '0x4': (42, 'SPLASH', None),
        '0x5': (42, 'NORMAL', '0x0001'),  # transient even if it calls itself normal
        '0x6': (99, 'NORMAL', None),  # unowned RViz
    }
    monkeypatch.setattr(windows.subprocess, 'check_output',
                        lambda *a, **k: '_NET_CLIENT_LIST(WINDOW): window id # ' + ', '.join(properties))

    def xprop(args, **kwargs):
        pid, kind, transient = properties[args[2]]
        return SimpleNamespace(stdout=(f'_NET_WM_PID(CARDINAL) = {pid}\n'
            f'_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_{kind}\n'
            + (f'WM_TRANSIENT_FOR(WINDOW): window id # {transient}\n'
               if transient else 'WM_TRANSIENT_FOR: not found.\n')))

    monkeypatch.setattr(windows.subprocess, 'run', xprop)
    assert windows.main_rviz_windows({42}) == {'0x1': 42}


def test_close_waits_for_process_exit_not_just_a_sent_request(monkeypatch):
    state = {'now': 0., 'closed': [], 'polls': 0}

    def live(_):
        state['polls'] += 1
        return {42} if state['now'] < .3 else set()

    monkeypatch.setattr(windows, '_live_rviz', live)
    monkeypatch.setattr(windows, 'main_rviz_windows', lambda p: {'0x1': 42})
    monkeypatch.setattr(windows, '_send_close', lambda w: state['closed'].append(w))
    monkeypatch.setattr(windows.time, 'monotonic', lambda: state['now'])
    monkeypatch.setattr(windows.time, 'sleep', lambda s: state.update(now=state['now'] + s))
    report = windows.close_rviz(object(), timeout=1)
    assert report['complete']
    assert report['wait_seconds'] >= .3
    assert state['closed'] == [{'0x1': 42}]


def test_pending_save_dialog_is_reported_as_incomplete(monkeypatch):
    monkeypatch.setattr(windows, '_live_rviz', lambda _: {42})
    monkeypatch.setattr(windows, 'main_rviz_windows', lambda _: {'0x1': 42})
    monkeypatch.setattr(windows, '_send_close', lambda _: None)
    report = windows.close_rviz(object(), timeout=0)
    assert not report['complete']
    assert report['pending_rviz_pids'] == [42]


def test_no_close_request_if_process_identity_disappears(monkeypatch):
    replies = iter([{42}, set(), set()])
    monkeypatch.setattr(windows, '_live_rviz', lambda _: next(replies))
    monkeypatch.setattr(windows, 'main_rviz_windows', lambda _: {'0x1': 42})

    def forbidden(_):
        raise AssertionError('A stale window must not receive a close request')

    monkeypatch.setattr(windows, '_send_close', forbidden)
    assert windows.close_rviz(object())['owned_rviz_close_requests'] == []
