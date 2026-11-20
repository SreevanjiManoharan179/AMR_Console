"""Shutdown without RViz: a session launched with use_rviz:=false shuts down without touching RViz,
and Gazebo's orderly stop still runs. Offline; no ROS node or process is started."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_support
import worker


def setUpModule():
    test_support.assert_guard_active()


class NoRvizShutdownTests(unittest.TestCase):
    def run_cleanup(self, no_rviz):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log_dir = Path(temp.name)
        (log_dir / 'session.log').write_text('')
        spec = {'kind': 'mapping', 'log_dir': str(log_dir), 'output_dir': str(log_dir), 'run_id': 't'}
        if no_rviz:
            spec['no_rviz'] = True
        w = worker.SessionWorker(spec)
        w.owned, w.environment, w.adapter = SimpleNamespace(refresh=lambda: True), {}, None
        w.setup_error, w.requested_stop = None, True
        w.emit = lambda *a, **k: None
        w._append_log = lambda *a, **k: None
        w.log = lambda *a, **k: None
        w.setup_thread, w.operation_threads, w.collector = None, [], None
        calls, seen = [], {}
        pause = SimpleNamespace(returncode=0, stdout='data: true', stderr='')

        def audit(log_text, tracked, inventory, window, **_kw):
            seen['window'] = window
            return {'continue_allowed': True, 'clean': True, 'bond_abort': False}
        with patch.dict(os.environ, {'DISPLAY': ':0'}), \
             patch.object(worker.subprocess, 'run', return_value=pause), \
             patch.object(worker.time, 'sleep'), \
             patch.object(worker, 'save_private_rviz', side_effect=lambda *a: calls.append('save') or (_ for _ in ()).throw(RuntimeError('No live owned RViz main window to save'))), \
             patch.object(worker, 'close_rviz', side_effect=lambda *a: calls.append('close') or {'complete': True}), \
             patch.object(worker, 'stop_gazebo', side_effect=lambda *a: calls.append('gazebo') or {'complete': True, 'targets': [], 'accepted': True}), \
             patch.object(worker, 'stop_owned', return_value=({'live_survivors': [], 'launch_returncode': 0}, False, [])), \
             patch.object(worker, 'full_inventory', return_value=[]), \
             patch.object(worker, 'forced_shutdown_evidence', return_value={'events': [], 'read_errors': []}), \
             patch.object(worker, 'selected_crash_bond_evidence', return_value=None), \
             patch.object(worker, 'classify_shutdown', side_effect=audit), \
             patch.object(worker, 'ROOT', log_dir):
            try:
                w.cleanup()
            except Exception as exc:  # later reporting needs live process records, not stubbed here
                seen['error'] = repr(exc)
        return calls, seen

    def test_no_rviz_session_skips_rviz_and_still_stops_gazebo(self):
        calls, seen = self.run_cleanup(no_rviz=True)
        self.assertNotIn('save', calls)
        self.assertNotIn('close', calls)
        self.assertIn('gazebo', calls)
        self.assertTrue(seen['window']['complete'])

    def test_session_with_rviz_still_saves_and_closes_it(self):
        calls, seen = self.run_cleanup(no_rviz=False)
        self.assertEqual(calls[:1], ['save'])          # the stubbed save fails, as on 26 Oct
        self.assertFalse(seen['window']['complete'])   # and that failure is still reported


if __name__ == '__main__':
    unittest.main()
