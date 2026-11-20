"""A failed manual setup enters audited cleanup without waiting for STOP.

All launch, ROS and thread boundaries are replaced with synchronous fakes;
this test starts no processes, DDS contexts, or threads.
"""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_support import assert_guard_active
import worker


def setUpModule():
    assert_guard_active()


class ManualReadinessFailureTests(unittest.TestCase):
    def test_readiness_transport_and_foreign_process_failures_stop_live_launch(self):
        cases = (
            ("readiness", "Map, scan and TF readiness timed out.", False, []),
            ("transport", None, False, []),
            ("foreign", None, True, [{"pid": 999, "owned": False}]),
        )
        for name, readiness_error, transport_ok, foreign in cases:
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                session = worker.SessionWorker({"kind": "navigation", "action_id": "nav.start",
                    "params": {}, "argv": ["fake_launch"], "log_dir": str(root / "logs"),
                    "output_dir": str(root / "docs"),
                    "env": {"ROS_DOMAIN_ID": "80", "GZ_PARTITION": "amr_console_navigation_80"}})
                session.emit = Mock()
                session.start_readers = Mock()
                session.cleanup = Mock()
                session.controls = Mock()
                session.controls.get.side_effect = AssertionError("Failed setup must not wait for user controls.")
                proc = Mock()
                proc.poll.return_value = None  # The launch stays alive throughout the test.
                owned = SimpleNamespace(proc=proc)
                adapter = Mock(deadman=None)
                if readiness_error:
                    adapter.readiness.side_effect = RuntimeError(readiness_error)
                else:
                    adapter.readiness.return_value = {}

                def synchronous_thread(*, target, **_kwargs):
                    def start():
                        if target.__name__ == "setup_ros":
                            target()
                    return SimpleNamespace(start=start, join=Mock())

                with patch.object(worker, "ROOT", root), \
                     patch.dict(worker.os.environ, {}, clear=False), \
                     patch.object(worker, "enable_subreaper"), \
                     patch.object(worker, "full_inventory", side_effect=[[], foreign]), \
                     patch.object(worker, "transport_snapshot", return_value={"processes": []}), \
                     patch.object(worker, "transport_collisions", return_value=[]), \
                     patch.object(worker, "verify_transport", return_value={"ok": transport_ok}), \
                     patch.object(worker.OwnedLaunch, "start", return_value=owned), \
                     patch.object(worker, "RosSession", return_value=adapter), \
                     patch.object(worker.threading, "Thread", side_effect=synchronous_thread):
                    session._run()

                self.assertTrue(session.setup_error)
                self.assertTrue(session.stop_requested.is_set())
                self.assertFalse(session.requested_stop)  # No user STOP was needed.
                session.cleanup.assert_called_once_with()
                session.controls.get.assert_not_called()
                proc.poll.assert_not_called()
                self.assertFalse(session.ready)
