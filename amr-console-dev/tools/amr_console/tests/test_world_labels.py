"""World and map choices carry display labels; the API still accepts only the ids."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENT = {"ros_ready": True}


class WorldLabelTests(unittest.TestCase):
    def test_world_and_map_selects_label_every_option(self):
        actions = registry.actions(ROOT, ENVIRONMENT)
        for action_id, name in (("map.start", "world"), ("map.explore", "world"), ("nav.start", "map")):
            param = next(p for p in registry.find_action(actions, action_id)["params"] if p["name"] == name)
            self.assertEqual(set(param["options"]), set(param["option_labels"]) & set(param["options"]), action_id)
            for option in param["options"]:
                self.assertTrue(param["option_labels"][option].strip(), option)
        self.assertEqual("Demo office (18 × 12 m)", registry.LABELS["proposed_system"])
        self.assertEqual("TurtleBot3 sandbox", registry.LABELS["tb3_sandbox"])

    def test_labels_are_not_accepted_as_values(self):
        action = registry.find_action(registry.actions(ROOT, ENVIRONMENT), "map.start")
        with self.assertRaises(registry.ConsoleError):
            registry.validate(action, {"world": "Demo office (18 × 12 m)"})


if __name__ == "__main__":
    unittest.main()
