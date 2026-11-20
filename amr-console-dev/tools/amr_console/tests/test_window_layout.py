"""Window layout (the user's choice, 18 Oct 2026): Gazebo left-top, RViz left-bottom.

No window, X server or process is touched: wmctrl, xwininfo and the owned-window
lookup are replaced by fakes.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_support import assert_guard_active
import windows
import worker


def setUpModule():
    assert_guard_active()


class LayoutGeometryTests(unittest.TestCase):
    def test_left_stack_matches_the_users_tested_command(self):
        # HW=W/2, HH=(H-90)/2; Gazebo at 0,30 and RViz at 0,60+HH (18 Oct wmctrl command).
        self.assertEqual(windows.layout_geometry(1920, 1080),
                         {'gazebo': (0, 30, 960, 495), 'rviz': (0, 555, 960, 495)})

    def test_left_right_remains_available(self):
        self.assertEqual(windows.layout_geometry(1920, 1080, 'left-right'),
                         {'gazebo': (0, 30, 960, 1010), 'rviz': (960, 30, 960, 1010)})

    def test_unknown_layout_is_refused(self):
        with self.assertRaises(ValueError):
            windows.layout_geometry(1920, 1080, 'diagonal')


class ArrangeOwnedTests(unittest.TestCase):
    def test_wmctrl_places_gazebo_left_top_and_rviz_left_bottom(self):
        owned = SimpleNamespace(refresh=lambda: [{'pid': 11, 'start_ticks': 1}, {'pid': 22, 'start_ticks': 2}])
        found = {'gazebo': {'id': '0x01', 'pid': 11, 'start_ticks': 1},
                 'rviz': {'id': '0x02', 'pid': 22, 'start_ticks': 2}}
        run = Mock()
        with patch.dict(windows.os.environ, {'XDG_SESSION_TYPE': 'x11'}), \
             patch.object(windows.shutil, 'which', return_value='/usr/bin/wmctrl'), \
             patch('window_capture.owned_windows', return_value=found), \
             patch.object(windows.subprocess, 'check_output', return_value='  Width: 1920\n  Height: 1080\n'), \
             patch.object(windows.subprocess, 'run', run):
            self.assertEqual(windows.arrange_owned(owned), {'supported': True, 'layout': 'left-stack'})
        moves = [call.args[0] for call in run.call_args_list if '-e' in call.args[0]]
        self.assertEqual(moves, [['/usr/bin/wmctrl', '-i', '-r', '0x01', '-e', '0,0,30,960,495'],
                                 ['/usr/bin/wmctrl', '-i', '-r', '0x02', '-e', '0,0,555,960,495']])

    def test_wayland_reports_unsupported_without_touching_windows(self):
        with patch.dict(windows.os.environ, {'XDG_SESSION_TYPE': 'wayland'}), \
             patch.object(windows.subprocess, 'run') as run:
            self.assertFalse(windows.arrange_owned(SimpleNamespace())['supported'])
            run.assert_not_called()


class KeeperTests(unittest.TestCase):
    def make(self):
        session = worker.SessionWorker({'kind': 'navigation', 'log_dir': '/tmp/unused', 'output_dir': '/tmp/unused'})
        session.owned = object()
        session.log = Mock()
        return session

    def test_each_new_window_pair_is_arranged_once(self):
        session = self.make()
        pairs = iter([None, ('0x1', '0x2'), ('0x1', '0x2'), ('0x3', '0x4'), ('0x3', '0x4')])
        ticks = iter([False, False, False, False, True])
        arrange = Mock(return_value={'supported': True})
        with patch.dict(worker.os.environ, {'XDG_SESSION_TYPE': 'x11'}), \
             patch.object(windows, 'owned_window_pair', side_effect=lambda owned: next(pairs)), \
             patch.object(windows, 'arrange_owned', arrange):
            session.keep_windows_arranged(wait=lambda interval: next(ticks))
        self.assertEqual(arrange.call_count, 2)
        self.assertEqual(session.log.call_count, 2)

    def test_errors_are_logged_once_and_the_keeper_carries_on(self):
        session = self.make()
        lookups = iter([RuntimeError('xprop failed'), RuntimeError('xprop failed'), ('0x1', '0x2')])
        def pair(owned):
            value = next(lookups)
            if isinstance(value, Exception):
                raise value
            return value
        ticks = iter([False, False, True])
        arrange = Mock(return_value={'supported': True})
        with patch.dict(worker.os.environ, {'XDG_SESSION_TYPE': 'x11'}), \
             patch.object(windows, 'owned_window_pair', side_effect=pair), \
             patch.object(windows, 'arrange_owned', arrange):
            session.keep_windows_arranged(wait=lambda interval: next(ticks))
        arrange.assert_called_once()
        warnings = [c for c in session.log.call_args_list if c.args[1:] == ('warn',)]
        self.assertEqual(len(warnings), 1)

    def test_not_x11_or_unsupported_stops_quietly(self):
        session = self.make()
        with patch.dict(worker.os.environ, {'XDG_SESSION_TYPE': 'wayland'}), \
             patch.object(windows, 'owned_window_pair') as pair:
            session.keep_windows_arranged(wait=lambda interval: True)
            pair.assert_not_called()
        with patch.dict(worker.os.environ, {'XDG_SESSION_TYPE': 'x11'}), \
             patch.object(windows, 'owned_window_pair', return_value=('0x1', '0x2')), \
             patch.object(windows, 'arrange_owned', return_value={'supported': False}):
            session.keep_windows_arranged(wait=lambda interval: self.fail('should have stopped'))
        session.log.assert_not_called()

    def test_stop_ends_the_keeper(self):
        session = self.make()
        session.stop_requested.set()
        with patch.dict(worker.os.environ, {'XDG_SESSION_TYPE': 'x11'}), \
             patch.object(windows, 'owned_window_pair') as pair:
            session.keep_windows_arranged(wait=lambda interval: True)
            pair.assert_not_called()


if __name__ == '__main__':
    unittest.main()
