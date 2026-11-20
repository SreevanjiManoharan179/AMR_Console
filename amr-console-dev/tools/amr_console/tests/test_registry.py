"""Pure offline registry/argv/approval checks; these tests start no processes."""
import math
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from errors import ConsoleError
from registry import actions, build, find_action, goal_in_bounds, map_bounds, read_approvals, validate
from test_support import assert_guard_active


ROOT = Path(__file__).resolve().parents[3]
ENV = {"recording_supported": False, "recording_reason": "ffmpeg isn't installed"}


def setUpModule():
    assert_guard_active()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.approvals = Path(self.temp.name) / "approvals.yaml"
        self.approvals.write_text("proposed_system_places: false\n")
        confirmation = patch('registry.places_confirmed', return_value=True)
        self.confirmation = confirmation.start()
        self.addCleanup(confirmation.stop)
        self.registry = actions(ROOT, ENV, self.approvals)

    def test_registry_is_complete_unique_and_defaults_validate(self):
        expected = {"env.build", "env.check", "map.start", "map.explore", "map.save", "nav.start", "nav.goal",
                    "commands.typed"}
        self.assertEqual(expected, {a["id"] for a in self.registry})
        self.assertEqual(len(expected), len(self.registry))
        for action in self.registry:
            with self.subTest(action=action["id"]):
                values = validate(action, {})
                self.assertEqual({p["name"] for p in action["params"]}, set(values))
                self.assertIn(action["kind"], {"command", "operation"})
                self.assertNotIn("nav2_shutdown_overlay", str(action))

    def test_approval_enables_o2_but_removed_maps_stay_rejected(self):
        for flag in ("false", "true"):
            self.approvals.write_text(f"proposed_system_places: {flag}\n")
            registry = actions(ROOT, ENV, self.approvals)
            self.assertEqual(flag == "false", find_action(registry, "commands.typed")["locked"])
            self.assertEqual(["proposed_system"] if flag == "true" else [],
                             find_action(registry, "commands.typed")["places_ready"])
            for action_id, name in (("map.start", "world"), ("nav.start", "map"), ("map.explore", "world")):
                for map_id in ("traversal_20260930_121704", "office_v1", "objective1_map", "office_v2", "../maps/x"):
                    with self.subTest(flag=flag, action=action_id, map=map_id), \
                            self.assertRaises(ConsoleError) as caught:
                        validate(find_action(registry, action_id), {name: map_id})
                    self.assertEqual(422, caught.exception.status)

    def test_o2_needs_both_confirmation_and_adapter_assets(self):
        self.approvals.write_text('proposed_system_places: true\n')
        self.assertFalse(find_action(actions(ROOT, ENV, self.approvals), 'commands.typed')['locked'])
        self.confirmation.return_value = False
        action = find_action(actions(ROOT, ENV, self.approvals), 'commands.typed')
        self.assertTrue(action['locked'])
        self.assertIn('approval_status: confirmed', action['lock_reason'])
        self.confirmation.return_value = True
        available = Path.is_file
        with patch.object(Path, 'is_file', autospec=True,
                          side_effect=lambda path: path.name != 'typed_commands.py' and available(path)):
            action = find_action(actions(ROOT, ENV, self.approvals), 'commands.typed')
        self.assertTrue(action['locked'])
        self.assertIn('typed_commands.py', action['lock_reason'])

    def test_approval_changes_are_read_again_and_malformed_fails_closed(self):
        key = "proposed_system_places"
        self.approvals.write_text(f"{key}: true\n")
        self.assertFalse(find_action(actions(ROOT, ENV, self.approvals), "commands.typed")["locked"])
        # Every key is optional: an empty or comment-only file reads as all false, without an error.
        for content in ("", "# nothing approved yet\n", f"{key}: false  # pending\n"):
            with self.subTest(content=content):
                self.approvals.write_text(content)
                self.assertEqual({key: False}, read_approvals(self.approvals))
                self.assertTrue(find_action(actions(ROOT, ENV, self.approvals), "commands.typed")["locked"])
        malformed = [f"{key}: yes", f"{key}: True", f"{key}: 'true'", f"{key}: 1", f"{key} true",
                     f"{key}: false\n{key}: true", f"{key}: true\n{key}: true",
                     f"{key}: true\no2_office_v3_locations: true",
                     # the retired lab keys are unknown now
                     "paired_study_90: false", "legacy_locations: true", "maps: [objective1]",
                     f"{key}: true\nmaps: [proposed_system]",
                     f"{key}: true\n" + "#" * 16384]
        for content in malformed:
            with self.subTest(content=content[:60]):
                self.approvals.write_text(content)
                parsed = read_approvals(self.approvals)
                self.assertEqual({key}, set(parsed) - {"error"})
                self.assertFalse(parsed[key])
                self.assertIn("error", parsed)
                action = find_action(actions(ROOT, ENV, self.approvals), "commands.typed")
                self.assertTrue(action["locked"])
                self.assertIn("could not be read safely", action["lock_reason"])
        self.approvals.unlink()
        parsed = read_approvals(self.approvals)
        self.assertFalse(parsed[key])
        self.assertIn("error", parsed)

    def test_unknown_types_nonfinite_values_and_locked_controls_are_rejected(self):
        hidden = ROOT / "src/amr_physical_ai/maps/proposed_system.pgm"
        available = Path.is_file
        with patch.object(Path, "is_file", autospec=True,
                          side_effect=lambda path: path != hidden and available(path)):
            nav = find_action(actions(ROOT, ENV, self.approvals), "nav.start")
        cases = [({"unknown": 1}, 422), ({"controller": True}, 422), ({"controller": ["mppi"]}, 422),
                 ({"controller": "mppi; echo injected"}, 422), ({"map": 1}, 422),
                 ({"map": "src/amr_physical_ai/maps/tb3_sandbox.yaml"}, 422),
                 ({"map": "proposed_system"}, 409)]
        for params, expected in cases:
            with self.subTest(params=params), self.assertRaises(ConsoleError) as caught:
                validate(nav, params)
            self.assertEqual(expected, caught.exception.status)
        self.assertEqual({"map": "tb3_sandbox", "controller": "mppi"}, validate(nav, {}))
        for value in (float("nan"), float("inf"), -float("inf"), True, "0.1"):
            with self.subTest(value=value), self.assertRaises(ConsoleError) as caught:
                validate(find_action(self.registry, "nav.goal"), {"x": value})
            self.assertEqual(422, caught.exception.status)

    def test_map_names_do_not_become_shell_or_path_text(self):
        save = find_action(self.registry, "map.save")
        for name in ("../map", "a/b", "my map", "$(touch pwned)", "`id`", "UPPER", "ab", "a" * 41):
            with self.subTest(name=name), self.assertRaises(ConsoleError):
                validate(save, {"name": name})
        self.assertEqual("test_console_map", validate(save, {"name": "test_console_map"})["name"])

    def test_commands_for_every_available_action_and_operation_run_id(self):
        for id_ in ("env.build", "env.check", "map.start", "map.explore", "nav.start"):
            action = find_action(self.registry, id_)
            spec = build(action, validate(action, {}), "20261005_111111_test", {"ROS_DOMAIN_ID": "80"}, {"root": str(ROOT)})
            self.assertIsInstance(spec["argv"], list)
            self.assertNotIn("nav2_shutdown_overlay", str(spec))
            sequences = spec["sequence"] or [spec["argv"]]
            self.assertEqual(sequences, [shlex.split(line) for line in spec["command"].split("\nthen: ")])
            self.assertEqual(str(ROOT / "data/runs/20261005_111111_test"), spec["output_dir"])
            self.assertEqual(str(ROOT / "data/logs/console/20261005_111111_test"), spec["log_dir"])
            if id_ in {"nav.start", "map.start", "map.explore"}:
                self.assertTrue(any(s.startswith("rviz_config:=data/logs/console/") for s in spec["argv"]))
                # The default world is the office: its own world file and a view framed on its map.
                self.assertIn("world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro", spec["argv"])
                self.assertEqual(("proposed_system", 30), (spec["view_map"], spec["view_scale"]))
            if id_ == "nav.start":
                self.assertFalse(any(s.startswith("controller:=") for s in spec["argv"]))
                self.assertIn("map:=src/amr_physical_ai/maps/proposed_system.yaml", spec["argv"])
        save = find_action(self.registry, "map.save")
        spec = build(save, validate(save, {"name": "test_console_map"}), "different_preview_id", {},
                     {"root": str(ROOT), "run_id": "active_mapping_id"})
        self.assertEqual("active_mapping_id", spec["run_id"])
        self.assertEqual("operation", spec["preview_kind"])
        self.assertEqual([], spec["argv"])
        self.assertTrue(spec["map_stem"].endswith("data/runs/active_mapping_id/maps/test_console_map/test_console_map"))

    def test_display_quotes_workspace_with_spaces_without_shell_execution(self):
        # argv stays relative to the explicit cwd, including a workspace with spaces.
        action = find_action(self.registry, "map.start")
        spec = build(action, {}, "run_quote_test", {}, {"root": str(Path(self.temp.name) / "with spaces")})
        self.assertEqual(spec["argv"], shlex.split(spec["command"]))

    def test_saved_map_bounds_and_rotated_rectangle(self):
        bounds = map_bounds(ROOT, "tb3_sandbox")
        self.assertEqual((112, 103), (bounds["width"], bounds["height"]))
        goal = find_action(self.registry, "nav.goal")
        validate(goal, {"x": 0, "y": 0}, bounds)
        with self.assertRaises(ConsoleError) as caught:
            validate(goal, {"x": 50, "y": 50}, bounds)
        self.assertEqual(422, caught.exception.status)
        rotated = {"origin": [0, 0, math.pi / 2], "resolution": 1., "width": 2, "height": 1}
        self.assertTrue(goal_in_bounds(-.5, 1., rotated))
        self.assertFalse(goal_in_bounds(.5, 1., rotated))

    def test_missing_assets_and_unknown_action_are_explicit(self):
        registry = actions(self.temp.name, ENV, self.approvals)
        self.assertTrue(find_action(registry, "map.start")["locked"])
        self.assertIn("Required assets", find_action(registry, "map.start")["lock_reason"])
        with self.assertRaises(ConsoleError) as caught:
            find_action(self.registry, "trials.unknown")
        self.assertEqual(404, caught.exception.status)

    def test_missing_shared_shutdown_helper_locks_every_launching_action(self):
        helper = ROOT / "src/amr_physical_ai/scripts/gazebo_stop.py"
        available = Path.is_file
        with patch.object(Path, "is_file", autospec=True,
                          side_effect=lambda path: path != helper and available(path)):
            registry = actions(ROOT, ENV, self.approvals)
        for id_ in ("map.start", "map.explore", "nav.start"):
            with self.subTest(action=id_):
                self.assertFalse(find_action(self.registry, id_)["locked"])
                action = find_action(registry, id_)
                self.assertTrue(action["locked"])
                self.assertIn("gazebo_stop.py", action["lock_reason"])
        self.assertFalse(find_action(registry, "env.check")["locked"])
        self.assertFalse(find_action(registry, "env.build")["locked"])

    def test_prepared_output_paths_reject_escaping_symlink_ancestors(self):
        fixture = Path(self.temp.name) / "workspace"
        fixture.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        for link in ("data", "data/runs", "data/logs", "data/logs/console", "data/runs/symlink_output_run"):
            with self.subTest(link=link):
                path = fixture / link
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside, target_is_directory=True)
                for action_id, params in (("map.start", {}), ("nav.start", {"map": "tb3_sandbox", "controller": "mppi"}),
                                          ("map.explore", {"world": "proposed_system", "method": "ours"}),
                                          ("env.check", {})):
                    with self.assertRaises(ConsoleError) as caught:
                        build(find_action(self.registry, action_id), params, "symlink_output_run", {},
                              {"root": str(fixture)})
                    self.assertEqual((409, "output_path_unsafe"), (caught.exception.status, caught.exception.code))
                path.unlink()
                shutil.rmtree(fixture / "data", ignore_errors=True)
        save = find_action(self.registry, "map.save")
        (fixture / "data/runs/active_run/maps").mkdir(parents=True)
        (fixture / "data/runs/active_run/maps/escaped").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ConsoleError) as caught:
            build(save, {"name": "escaped"}, "preview_id", {}, {"root": str(fixture), "run_id": "active_run"})
        self.assertEqual(409, caught.exception.status)

    def test_ros_readiness_gate_preserves_permanent_lock_reasons(self):
        env = dict(ENV, ros_ready=False, ros_reason="Source Jazzy and the workspace setup before starting the console.")
        registry = actions(ROOT, env, self.approvals)
        for id_ in ("map.start", "map.explore", "map.save", "nav.start", "nav.goal", "commands.typed"):
            action = find_action(registry, id_)
            self.assertTrue(action["locked"])
            self.assertIn(env["ros_reason"], action["lock_reason"])
        for id_ in ("env.build", "env.check"):
            self.assertFalse(find_action(registry, id_)["locked"])
        # The permanent named-place reason (approval false) is kept alongside the ROS reason.
        self.assertIn("proposed_system_places: true", find_action(registry, "commands.typed")["lock_reason"])


if __name__ == "__main__":
    unittest.main()
