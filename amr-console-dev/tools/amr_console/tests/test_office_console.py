"""The console offers the proposed_system office and the TurtleBot3 sandbox; typed commands to named places stay per map. Offline; no processes."""
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import registry
from errors import ConsoleError
from registry import actions, build, find_action, read_approvals, validate
from test_support import assert_guard_active
import worker

ROOT = Path(__file__).resolve().parents[3]
ENV = {"recording_supported": False, "recording_reason": "ffmpeg isn't installed"}
OFFICE_V2_WORLD = "world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro"
# Explicit approvals: these tests never depend on your real approvals.yaml.
NOT_APPROVED = {"proposed_system_places": False}
OFFICE_V2_ONLY = {"proposed_system_places": True}
OFFICE_V2_PLACES = ["home", "reception", "conference hall", "demo area", "cafeteria", "open office",
                    "team office", "manager cabin", "storage"]


def setUpModule():
    assert_guard_active()



def without_grid(displays):
    """The displays text with the Grid display block removed (it is laid over the map)."""
    import re
    return "".join(b for b in re.split(r"(?m)^(?=    - )", displays) if "rviz_default_plugins/Grid" not in b)

class OfficeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.approvals = Path(self.temp.name) / "approvals.yaml"
        self.approvals.write_text("proposed_system_places: false\n")

    def registry(self):
        return actions(ROOT, ENV, self.approvals)

    def nav_spec(self, map_id, approvals):
        with patch("registry.read_approvals", return_value=approvals):
            action = find_action(self.registry(), "nav.start")
            values = validate(action, {"map": map_id})
            return build(action, values, "20261018_150000_office", {"ROS_DOMAIN_ID": "80"}, {"root": str(ROOT)})

    def test_both_maps_are_offered_and_the_sandbox_opens_the_reference_world(self):
        self.assertEqual(("proposed_system", "tb3_sandbox"), registry.MAP_IDS)
        nav = find_action(self.registry(), "nav.start")
        maps = next(p for p in nav["params"] if p["name"] == "map")
        self.assertEqual(["proposed_system", "tb3_sandbox"], maps["options"])
        self.assertEqual("proposed_system", maps["default"])
        self.assertEqual({}, maps["locked_options"])
        with patch("registry.places_confirmed", return_value=True):
            spec = self.nav_spec("tb3_sandbox", NOT_APPROVED)
        self.assertFalse(any(arg.startswith("world:=") for arg in spec["argv"]))   # the TurtleBot3 world
        self.assertIn("map:=src/amr_physical_ai/maps/tb3_sandbox.yaml", spec["argv"])
        self.assertEqual(("tb3_sandbox", 80), (spec["view_map"], spec["view_scale"]))
        self.assertEqual("tb3_sandbox", spec["world"])
        self.assertFalse(spec["record_truth"])
        self.assertNotIn("ground_truth:=true", spec["argv"])
        self.assertEqual(spec["argv"], shlex.split(spec["command"]))

    def test_sandbox_session_never_gets_an_command_profile(self):
        # The sandbox has no named places, so even an approved office does not give it typed commands.
        with patch("registry.places_confirmed", return_value=True):
            spec = self.nav_spec("tb3_sandbox", OFFICE_V2_ONLY)
        self.assertNotIn("tb3_sandbox", registry.COMMAND_MAPS)
        self.assertFalse(spec["record_truth"])
        self.assertNotIn("command_profile", spec)
        self.assertNotIn("ground_truth:=true", spec["argv"])

    def test_office_files_missing_lock_only_that_option(self):
        available = Path.is_file
        hidden = ROOT / "src/amr_physical_ai/worlds/proposed_system.sdf.xacro"
        with patch.object(Path, "is_file", autospec=True,
                          side_effect=lambda path: path != hidden and available(path)):
            items = self.registry()
        nav = find_action(items, "nav.start")
        maps = next(p for p in nav["params"] if p["name"] == "map")
        self.assertEqual(["tb3_sandbox"], maps["options"])                       # the sandbox is unaffected
        self.assertEqual("tb3_sandbox", maps["default"])
        self.assertIn("proposed_system.sdf.xacro", maps["locked_options"]["proposed_system"])
        self.assertFalse(nav["locked"])
        start = find_action(items, "map.start")
        world = next(p for p in start["params"] if p["name"] == "world")
        self.assertEqual(["tb3_sandbox"], world["options"])
        self.assertIn("proposed_system", world["locked_options"])
        self.assertFalse(start["locked"])
        self.assertTrue(find_action(items, "map.explore")["locked"])             # no world can be scored
        with self.assertRaises(ConsoleError) as caught:
            validate(nav, {"map": "proposed_system"})
        self.assertEqual(409, caught.exception.status)

    def test_mapping_world_choice(self):
        action = find_action(self.registry(), "map.start")
        default = build(action, validate(action, {}), "20261018_150001_map", {}, {"root": str(ROOT)})
        self.assertIn(OFFICE_V2_WORLD, default["argv"])                          # the default world
        self.assertEqual(("proposed_system", 30), (default["view_map"], default["view_scale"]))
        sandbox = build(action, validate(action, {"world": "tb3_sandbox"}), "20261018_150002_map", {},
                        {"root": str(ROOT)})
        self.assertFalse(any(arg.startswith("world:=") for arg in sandbox["argv"]))
        self.assertEqual(("tb3_sandbox", 80), (sandbox["view_map"], sandbox["view_scale"]))
        self.assertEqual(sandbox["argv"], shlex.split(sandbox["command"]))

    def test_approval_keys_are_optional_and_unknown_keys_fail_closed(self):
        self.assertEqual({"proposed_system_places": False}, read_approvals(self.approvals))
        self.approvals.write_text("# no approvals yet\n")
        self.assertEqual({"proposed_system_places": False}, read_approvals(self.approvals))
        self.approvals.write_text("proposed_system_places: true\n")
        self.assertEqual({"proposed_system_places": True}, read_approvals(self.approvals))
        for content in ("proposed_system_places: true\no2_office_v3_locations: true\n",
                        "proposed_system_places: true\no2_office_v1_locations: true\n",
                        "legacy_office_places: true\n"):
            with self.subTest(content=content):
                self.approvals.write_text(content)
                parsed = read_approvals(self.approvals)
                self.assertIn("error", parsed)
                self.assertFalse(parsed["proposed_system_places"])

    def test_commands_tab_lists_each_maps_places(self):
        with patch("registry.places_confirmed", return_value=True):
            action = find_action(self.registry(), "commands.typed")
            self.assertEqual({"proposed_system": OFFICE_V2_PLACES}, action["places"])
            self.assertEqual([], action["places_ready"])                         # awaits its approval key
            self.approvals.write_text("proposed_system_places: true\n")
            self.assertEqual(["proposed_system"], find_action(self.registry(), "commands.typed")["places_ready"])

    def test_world_without_its_map_is_locked_and_keeps_the_template_view(self):
        available = Path.is_file
        hidden = ROOT / "src/amr_physical_ai/maps/proposed_system.pgm"
        with patch.object(Path, "is_file", autospec=True,
                          side_effect=lambda path: path != hidden and available(path)):
            items = self.registry()
            action = find_action(items, "map.start")
            with self.assertRaises(ConsoleError) as caught:
                validate(action, {"world": "proposed_system"})
            self.assertEqual(409, caught.exception.status)
            # Even if asked directly, the build does not frame a view on a map that is not there.
            spec = build(action, {"world": "proposed_system"}, "20261018_150003_map", {}, {"root": str(ROOT)})
        self.assertIn(OFFICE_V2_WORLD, spec["argv"])
        self.assertNotIn("view_map", spec)
        maps = next(p for p in find_action(items, "nav.start")["params"] if p["name"] == "map")
        self.assertIn("proposed_system.pgm", maps["locked_options"]["proposed_system"])

    def test_places_file_that_is_not_utf8_cannot_break_the_action_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "src/amr_physical_ai/config/named_locations_proposed_system.yaml"
            target.parent.mkdir(parents=True)
            target.write_bytes("approval_status: confirmed\nlocations:\n  - name: caf\xe9\n".encode("latin-1"))
            self.approvals.write_text("proposed_system_places: true\n")
            items = actions(root, ENV, self.approvals)
            self.assertNotIn("proposed_system", find_action(items, "commands.typed")["places"])
            self.assertFalse(registry.places_confirmed(root, "proposed_system"))
            self.assertTrue(find_action(items, "commands.typed")["locked"])

    def test_office_needs_both_its_approval_and_confirmation_to_unlock(self):
        for approved, confirmed in ((False, True), (True, False), (True, True)):
            with self.subTest(approved=approved, confirmed=confirmed), \
                    patch("registry.places_confirmed", return_value=confirmed):
                self.approvals.write_text(f"proposed_system_places: {'true' if approved else 'false'}\n")
                action = find_action(self.registry(), "commands.typed")
                self.assertEqual(not (approved and confirmed), action["locked"])

    def test_proposed_system_navigation_opens_its_world_and_a_wider_view(self):
        with patch("registry.places_confirmed", return_value=True):
            spec = self.nav_spec("proposed_system", NOT_APPROVED)
        self.assertIn(OFFICE_V2_WORLD, spec["argv"])
        self.assertIn("map:=src/amr_physical_ai/maps/proposed_system.yaml", spec["argv"])
        self.assertEqual(("proposed_system", 30), (spec["view_map"], spec["view_scale"]))
        self.assertFalse(spec["record_truth"])
        self.assertNotIn("ground_truth:=true", spec["argv"])
        self.assertEqual(spec["argv"], shlex.split(spec["command"]))

    def test_proposed_system_o2_needs_its_own_approval_and_confirmation(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed), patch("registry.places_confirmed", return_value=confirmed):
                spec = self.nav_spec("proposed_system", OFFICE_V2_ONLY)
                self.assertEqual(confirmed, spec["record_truth"])
                if confirmed:
                    self.assertEqual({"map_id": "proposed_system", "approval": "proposed_system_places",
                                      "locations": "src/amr_physical_ai/config/named_locations_proposed_system.yaml",
                                      "grounding_results": "config/calibration/proposed_system/grounding/results.json"},
                                     spec["command_profile"])
                    self.assertIn("ground_truth:=true", spec["argv"])

    def test_mapping_in_proposed_system(self):
        action = find_action(self.registry(), "map.start")
        world = next(p for p in action["params"] if p["name"] == "world")
        self.assertEqual(["proposed_system", "tb3_sandbox"], world["options"])
        spec = build(action, validate(action, {"world": "proposed_system"}), "20261018_150004_map", {},
                     {"root": str(ROOT)})
        self.assertIn(OFFICE_V2_WORLD, spec["argv"])
        self.assertEqual(("proposed_system", 30), (spec["view_map"], spec["view_scale"]))
        self.assertEqual(spec["argv"], shlex.split(spec["command"]))

    def test_every_world_has_its_files_and_view_and_every_command_map_its_places_and_key(self):
        self.assertEqual(set(registry.MAP_IDS), set(registry.VIEW_SCALES))
        self.assertEqual({"proposed_system": 30, "tb3_sandbox": 80}, registry.VIEW_SCALES)
        self.assertEqual(("proposed_system",), registry.EXPLORE_WORLDS)
        self.assertEqual({"proposed_system": "proposed_system"}, registry.OPTIONAL_MAPS)
        self.assertIsNone(registry.WORLDS["tb3_sandbox"])
        for map_id in registry.MAP_IDS:
            for path in registry.map_files(map_id):
                self.assertTrue((ROOT / path).is_file(), (map_id, path))
        self.assertEqual(["proposed_system"], list(registry.COMMAND_MAPS))
        self.assertEqual({"proposed_system_places": False}, registry._OPTIONAL_APPROVALS)
        for map_id, profile in registry.COMMAND_MAPS.items():
            self.assertIn(profile["approval"], registry._OPTIONAL_APPROVALS)
            self.assertEqual(f"{map_id}_places", profile["approval"])
            self.assertEqual(f"src/amr_physical_ai/config/named_locations_{map_id}.yaml", profile["locations"])
            self.assertTrue((ROOT / profile["locations"]).is_file(), map_id)
            self.assertTrue((ROOT / profile["grounding_results"]).is_file(), map_id)


class OfficeViewTests(unittest.TestCase):
    def copy(self, spec):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "rviz_config.rviz"
            engine.private_rviz_copy(spec, ROOT, target)
            return target.read_text()

    def test_sandbox_navigation_view_is_centred_on_its_map(self):
        source = ROOT / "src/amr_physical_ai/rviz/tb3_navigation.rviz"
        text = self.copy({"action_id": "nav.start", "rviz_source": str(source),
                          "view_map": "tb3_sandbox", "view_scale": 80})
        views = text.split("  Views:\n", 1)[1]
        self.assertIn("      X: 1.846\n", views)      # origin -0.954 + 112 px * 0.05 m / 2
        self.assertIn("      Y: 0.506\n", views)      # origin -2.069 + 103 px * 0.05 m / 2
        self.assertIn("      Scale: 80\n", views)
        # Only the Grid display moves (laid over the office); every other display is the template's.
        self.assertEqual(without_grid(source.read_text().split("  Views:\n")[0]),
                         without_grid(text.split("  Views:\n")[0]))

    def test_proposed_system_view_is_centred_on_the_larger_office(self):
        source = ROOT / "src/amr_physical_ai/rviz/tb3_navigation.rviz"
        text = self.copy({"action_id": "nav.start", "rviz_source": str(source),
                          "view_map": "proposed_system", "view_scale": 30})
        views = text.split("  Views:\n", 1)[1]
        self.assertIn("      X: 6\n", views)
        self.assertIn("      Y: 0\n", views)
        self.assertIn("      Scale: 30\n", views)

    def grid(self, text):
        block = next(b for b in text.split("  Views:\n")[0].split("    - ") if "rviz_default_plugins/Grid" in b)
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.strip().partition(": ")
            if sep:
                fields.setdefault(key, value)
        return float(fields["X"]), float(fields["Y"]), int(fields["Plane Cell Count"])

    def assert_grid_covers(self, text, map_id):
        x, y, count = self.grid(text)
        bounds = registry.map_bounds(ROOT, map_id)
        left, bottom, _ = bounds["origin"]
        right = left + bounds["width"] * bounds["resolution"]
        top = bottom + bounds["height"] * bounds["resolution"]
        self.assertEqual((x, y), (round(x), round(y)))      # 1 m lines stay on whole map metres
        self.assertEqual(0, count % 2)
        self.assertTrue(x - count / 2 <= left and right <= x + count / 2, (x, count, left, right))
        self.assertTrue(y - count / 2 <= bottom and top <= y + count / 2, (y, count, bottom, top))

    def test_proposed_system_grid_is_laid_over_the_whole_office(self):
        for action_id, template in (("map.explore", "tb3_navigation.rviz"), ("nav.start", "tb3_navigation.rviz"),
                                    ("map.start", "tb3_mapping.rviz")):
            source = ROOT / "src/amr_physical_ai/rviz" / template
            text = self.copy({"action_id": action_id, "rviz_source": str(source),
                              "view_map": "proposed_system", "view_scale": 30})
            self.assertEqual((6.0, 0.0, 20), self.grid(text), action_id)
            self.assert_grid_covers(text, "proposed_system")

    def test_sandbox_grids_cover_its_map(self):
        source = ROOT / "src/amr_physical_ai/rviz/tb3_navigation.rviz"
        self.assert_grid_covers(self.copy({"action_id": "nav.start", "rviz_source": str(source),
                                           "view_map": "tb3_sandbox", "view_scale": 80}), "tb3_sandbox")
        mapping = ROOT / "src/amr_physical_ai/rviz/tb3_mapping.rviz"
        self.assert_grid_covers(self.copy({"action_id": "map.start", "rviz_source": str(mapping)}), "tb3_sandbox")

    def test_templates_without_a_grid_are_left_alone(self):
        displays = "Visualization Manager:\n  Displays:\n    - Class: rviz_default_plugins/Map\n      Name: Map\n"
        self.assertEqual(displays, engine.grid_on_map(displays, 6, 0, 9.5))

    def test_other_navigation_views_stay_byte_identical(self):
        source = ROOT / "src/amr_physical_ai/rviz/tb3_navigation.rviz"
        text = self.copy({"action_id": "nav.start", "rviz_source": str(source)})
        self.assertEqual(source.read_text(), text)


class OfficeWorkerTests(unittest.TestCase):
    def test_worker_passes_the_session_profile_to_o2(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = {"map_id": "proposed_system", "approval": "proposed_system_places",
                       "locations": "src/amr_physical_ai/config/named_locations_proposed_system.yaml",
                       "grounding_results": "config/calibration/proposed_system/grounding/results.json"}
            session = worker.SessionWorker({"kind": "navigation", "record_truth": True, "command_profile": profile,
                                            "output_dir": str(Path(temp) / "out"), "log_dir": str(Path(temp) / "log")})
            session.ready, session.adapter = True, object()
            session.environment = {"ROS_DOMAIN_ID": "80", "GZ_PARTITION": "amr_console_navigation_80"}
            session.emit, session.log = Mock(), Mock()
            result = {"parser": "RULE-BASED FALLBACK (no LLM)", "validation": {"question": "Which one?"},
                      "nav2": {"status": "CLARIFICATION", "goal_sent": False}}
            fake = Mock(return_value=result)
            with patch.object(worker, "ROOT", Path(temp)), patch.dict(sys.modules, {"typed_commands": SimpleNamespace(command=fake)}), \
                    patch.object(worker.threading, "Thread", lambda *, target, **kwargs: SimpleNamespace(start=target)):
                session.operation({"action_id": "commands.typed", "request_id": "b" * 24, "params": {"text": "go to the meeting room"}})
            self.assertEqual(profile, fake.call_args.kwargs["profile"])
            self.assertTrue(session.emit.call_args.kwargs["ok"])


if __name__ == "__main__":
    unittest.main()
