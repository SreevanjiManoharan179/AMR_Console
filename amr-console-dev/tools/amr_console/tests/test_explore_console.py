"""Autonomous exploration: the console's map.explore action, its session and the explorer stop at STOP. Offline; no processes."""
from pathlib import Path
import json
import shlex
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from engine import ConsoleBackend
from errors import ConsoleError
from fixtures import ENVIRONMENT, FakeClock, FakeSupervisors, FixtureRepo
import registry
from registry import actions, build, find_action, validate
from ros_adapter import RosSession
from test_support import assert_guard_active
import worker

ROOT = Path(__file__).resolve().parents[3]
ENV = {"recording_supported": False, "recording_reason": "ffmpeg isn't installed"}
RUN = "20261018_200000_map_explore_0a1b2c3d"


def setUpModule():
    assert_guard_active()



def without_grid(displays):
    """The displays text with the Grid display block removed (it is laid over the map)."""
    import re
    return "".join(b for b in re.split(r"(?m)^(?=    - )", displays) if "rviz_default_plugins/Grid" not in b)

class ExploreRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.approvals = Path(self.temp.name) / "approvals.yaml"
        self.approvals.write_text("proposed_system_places: false\n")

    def action(self, root=ROOT):
        return find_action(actions(root, ENV, self.approvals), "map.explore")

    def test_action_is_a_mapping_session_without_the_drive_pad(self):
        action = self.action()
        self.assertEqual(("map", "mapping", None, False, False),
                         (action["tab"], action["session"], action["requires_session"], action["teleop"],
                          action["locked"]))
        params = {p["name"]: p for p in action["params"]}
        self.assertEqual(["proposed_system"], params["world"]["options"])
        self.assertEqual("proposed_system", params["world"]["default"])
        self.assertEqual(["ours", "gain", "nearest"], params["method"]["options"])
        self.assertEqual("ours", params["method"]["default"])
        self.assertEqual({"world": "proposed_system", "method": "ours"}, validate(action, {}))
        # The sandbox map is SLAM-built, not exact, so exploration there cannot be scored.
        for bad in ({"method": "random"}, {"world": "tb3_sandbox"}, {"world": "office_v1"},
                    {"method": "ours; rm -rf"}):
            with self.subTest(bad=bad), self.assertRaises(ConsoleError) as caught:
                validate(action, bad)
            self.assertEqual(422, caught.exception.status)

    def test_command_runs_slam_nav2_and_the_explorer_with_truth(self):
        action = self.action()
        spec = build(action, validate(action, {"world": "proposed_system", "method": "gain"}), RUN,
                     {"ROS_DOMAIN_ID": "80"}, {"root": str(ROOT)})
        self.assertEqual(["ros2", "launch", "--noninteractive", "amr_physical_ai", "tb3_mapping.launch.py",
                          "world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro", "explore:=gain",
                          f"explore_out:=data/runs/{RUN}/exploration",
                          "params_file:=src/amr_physical_ai/config/nav2_mppi.yaml", "ground_truth:=true",
                          f"rviz_config:=data/logs/console/{RUN}/rviz_config.rviz"], spec["argv"])
        self.assertEqual(spec["argv"], shlex.split(spec["command"]))
        self.assertEqual(("mapping", "command"), (spec["kind"], spec["preview_kind"]))
        self.assertEqual(("proposed_system", 30), (spec["view_map"], spec["view_scale"]))
        self.assertTrue(spec["rviz_source"].endswith("rviz/tb3_navigation.rviz"))
        self.assertEqual({"method": "gain", "world": "proposed_system",
                          "out": str(ROOT / "data/runs" / RUN / "exploration")}, spec["explore"])

    def test_a_missing_office_locks_only_that_world(self):
        for hidden in ("src/amr_physical_ai/maps/proposed_system.pgm",
                       "src/amr_physical_ai/worlds/proposed_system.sdf.xacro"):
            with self.subTest(hidden=hidden):
                available = Path.is_file
                with patch.object(Path, "is_file", autospec=True,
                                  side_effect=lambda path: path != ROOT / hidden and available(path)):
                    items = actions(ROOT, ENV, self.approvals)
                action = find_action(items, "map.explore")
                world = next(p for p in action["params"] if p["name"] == "world")
                self.assertEqual([], world["options"])
                self.assertIn(Path(hidden).name, world["locked_options"]["proposed_system"])
                self.assertTrue(action["locked"])
                with self.assertRaises(ConsoleError) as caught:
                    validate(action, {"world": "proposed_system"})
                self.assertEqual(409, caught.exception.status)
                # The TurtleBot3 sandbox stays available for mapping and navigation.
                for action_id, name in (("map.start", "world"), ("nav.start", "map")):
                    other = find_action(items, action_id)
                    param = next(p for p in other["params"] if p["name"] == name)
                    self.assertFalse(other["locked"])
                    self.assertEqual(["tb3_sandbox"], param["options"])
                    self.assertEqual("tb3_sandbox", param["default"])
                    self.assertEqual("tb3_sandbox", validate(other, {})[name])

    def test_without_any_office_or_explorer_file_the_action_is_locked(self):
        action = self.action(self.temp.name)
        self.assertTrue(action["locked"])
        available = Path.is_file
        hidden = ROOT / "src/amr_physical_ai/amr_physical_ai/explore_node.py"
        with patch.object(Path, "is_file", autospec=True,
                          side_effect=lambda path: path != hidden and available(path)):
            action = self.action()
        self.assertTrue(action["locked"])
        self.assertIn("explore_node.py", action["lock_reason"])

    def test_explorer_files_are_required_and_its_methods_match(self):
        self.assertTrue(all((ROOT / path).is_file() for path in registry.EXPLORE_FILES))
        self.assertEqual(tuple(registry.EXPLORE_METHODS), ("ours", "gain", "nearest"))
        sys.path.insert(0, str(ROOT / "src/amr_physical_ai"))
        from amr_physical_ai.exploration import METHODS
        self.assertEqual(set(METHODS), set(registry.EXPLORE_METHODS))

    def test_output_folder_symlink_escape_is_refused(self):
        workspace = Path(self.temp.name) / "workspace"
        outside = Path(self.temp.name) / "outside"
        (workspace / "data/runs").mkdir(parents=True)
        outside.mkdir()
        (workspace / "data/runs" / RUN).symlink_to(outside, target_is_directory=True)
        action = self.action()
        with self.assertRaises(ConsoleError) as caught:
            build(action, validate(action, {}), RUN, {}, {"root": str(workspace)})
        self.assertEqual(409, caught.exception.status)


class ExploreViewTests(unittest.TestCase):
    def test_view_is_the_navigation_template_framed_on_the_office(self):
        source = ROOT / "src/amr_physical_ai/rviz/tb3_navigation.rviz"
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "rviz_config.rviz"
            engine.private_rviz_copy({"action_id": "map.explore", "rviz_source": str(source),
                                      "view_map": "proposed_system", "view_scale": 30}, ROOT, target)
            text = target.read_text()
        views = text.split("  Views:\n", 1)[1]
        self.assertIn("      X: 6\n", views)
        self.assertIn("      Y: 0\n", views)
        self.assertIn("      Scale: 30\n", views)
        # Only the Grid display moves (laid over the office); every other display is the template's.
        self.assertEqual(without_grid(source.read_text().split("  Views:\n")[0]),
                         without_grid(text.split("  Views:\n")[0]))


class ExploreEngineTests(unittest.TestCase):
    OFFICE = registry.map_files("proposed_system")

    def setUp(self):
        self.repo = FixtureRepo()
        self.addCleanup(self.repo.close)
        for relative in self.OFFICE + registry.EXPLORE_FILES:
            target = self.repo.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.clock = FakeClock()
        self.supervisors = FakeSupervisors()
        self.backend = ConsoleBackend(
            root=self.repo.root, inventory=lambda: [], lock_probe=lambda: False, clock=self.clock,
            wall_clock=self.clock.wall, supervisor_factory=self.supervisors,
            transport_selector=lambda kind: {'ROS_DOMAIN_ID': '80', 'GZ_PARTITION': f'amr_console_{kind}_80'},
            environment=dict(ENVIRONMENT))
        self.addCleanup(lambda: self.backend.status()['state'] == 'idle' or self.backend.stop())

    def test_an_edited_explorer_invalidates_a_prepared_preview(self):
        preview = self.backend.preview("map.explore", {"world": "proposed_system", "method": "ours"})
        explorer = self.repo.root / "src/amr_physical_ai/amr_physical_ai/exploration.py"
        explorer.write_text(explorer.read_text() + "\n# edited after the preview\n")
        with self.assertRaises(ConsoleError) as caught:
            self.backend.run("map.explore", {"world": "proposed_system", "method": "ours"}, spec_id=preview["spec_id"])
        self.assertEqual("preview_stale", caught.exception.code)

    def test_session_records_truth_keeps_the_pad_off_and_allows_saving(self):
        preview = self.backend.preview("map.explore", {"world": "proposed_system", "method": "nearest"})
        self.assertIsNone(preview["blocked_reason"])
        self.assertIn("explore:=nearest", preview["command"])
        run_id = self.backend.run("map.explore", {"world": "proposed_system", "method": "nearest"},
                                  spec_id=preview["spec_id"])["run_id"]
        status = self.backend.status()
        self.assertEqual(("running", "mapping", False), (status["state"], status["session"]["kind"],
                                                          status["session"]["teleop"]))
        spec = self.supervisors.last.spec
        self.assertEqual("nearest", spec["explore"]["method"])
        self.assertTrue(spec["explore"]["out"].endswith(f"data/runs/{run_id}/exploration"))
        self.assertFalse((self.repo.root / "data/runs" / run_id).exists())   # the node creates it
        rviz = (self.repo.root / "data/logs/console" / run_id / "rviz_config.rviz").read_text()
        self.assertIn("      Scale: 30\n", rviz)
        with self.assertRaises(ConsoleError) as caught:
            self.backend.teleop(.1, 0.)
        self.assertEqual(409, caught.exception.status)
        save = self.backend.preview("map.save", {"name": "explored_office"})
        self.assertIsNone(save["blocked_reason"])
        blocked = self.backend.preview("map.start", {})
        self.assertIn("Another session is running", blocked["blocked_reason"])


class ExplorerStopTests(unittest.TestCase):
    def session(self, *, available=True, success=True, delay=0.):
        calls = []

        class Future:
            def __init__(self, response):
                self.response = response

            def done(self):
                return True

            def exception(self):
                return None

            def result(self):
                return self.response

        class Client:
            def service_is_ready(self):
                return available

            def call_async(self, request):
                calls.append(request)
                return Future(SimpleNamespace(success=success, message='Exploration stop requested.'))

        session = RosSession.__new__(RosSession)
        session.kind = 'mapping'
        session.stop_requested = threading.Event()
        session.stop_requested.set()                    # STOP is set while cleanup asks the explorer
        session.context = SimpleNamespace(ok=lambda: True)
        session.explore_stop = Client()
        session.trigger_type = SimpleNamespace(Request=lambda: 'request')
        return session, calls

    def test_stop_reaches_the_explorer_even_though_stop_is_set(self):
        session, calls = self.session()
        report = session.stop_explorer(timeout=1.)
        self.assertEqual({'service': '/amr_explorer/stop', 'called': True, 'success': True,
                          'message': 'Exploration stop requested.'}, report)
        self.assertEqual(['request'], calls)

    def test_an_absent_explorer_is_not_called(self):
        session, calls = self.session(available=False)
        with patch('ros_adapter.time.sleep'):
            report = session.stop_explorer(timeout=.05)
        self.assertFalse(report['called'] or report['success'])
        self.assertIn('timed out', report['error'])
        self.assertEqual([], calls)
        plain = RosSession.__new__(RosSession)
        plain.explore_stop = None
        self.assertFalse(plain.stop_explorer()['called'])

    def worker(self, temp, adapter, explore=True):
        session = worker.SessionWorker({'kind': 'mapping', 'log_dir': str(Path(temp) / 'log'),
                                        'output_dir': str(Path(temp) / 'out'),
                                        **({'explore': {'method': 'ours'}} if explore else {})})
        (Path(temp) / 'log').mkdir()
        session.adapter = adapter
        return session

    def test_worker_records_the_stop_and_fails_cleanup_when_it_is_refused(self):
        for success, error in ((True, None), (False, 'Explorer stop failed: no')):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as temp:
                report = {'service': '/amr_explorer/stop', 'called': True, 'success': success, 'message': 'no'}
                session = self.worker(temp, SimpleNamespace(stop_explorer=lambda: report))
                self.assertEqual(report, session.stop_explorer())
                self.assertEqual(report, json.loads((Path(temp) / 'log/explore_stop.json').read_text()))
                self.assertEqual(error, session.setup_error)

    def test_worker_without_an_explorer_does_nothing_and_absent_explorer_is_not_a_fault(self):
        with tempfile.TemporaryDirectory() as temp:
            session = self.worker(temp, SimpleNamespace(stop_explorer=lambda: 1 / 0), explore=False)
            self.assertIsNone(session.stop_explorer())
            self.assertFalse((Path(temp) / 'log/explore_stop.json').exists())
        with tempfile.TemporaryDirectory() as temp:
            report = {'service': '/amr_explorer/stop', 'called': False, 'success': False, 'error': 'timed out'}
            session = self.worker(temp, SimpleNamespace(stop_explorer=lambda: report))
            session.stop_explorer()
            self.assertIsNone(session.setup_error)


if __name__ == "__main__":
    unittest.main()
