"""Text/result path checks using only temporary files; no fake processes."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from errors import ConsoleError
from paths import MAX_TEXT_BYTES, file_response, list_results, resolve_allowed
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "data/runs/run_a").mkdir(parents=True)
        (self.root / "data/logs/console/session").mkdir(parents=True)
        (self.root / "docs/record.md").write_text("old docs record\n")

    def record(self):
        """A readable record in a run folder (written only by tests that do not count result rows)."""
        (self.root / "data/runs/run_a/record.md").write_text("hello\n")

    def test_allowed_data_records_and_docs_refused(self):
        self.record()
        (self.root / "data/logs/console/session/cleanup.json").write_text("{}")
        (self.root / "data/benchmarks/batch").mkdir(parents=True)
        (self.root / "data/benchmarks/batch/manifest.json").write_text("[]")
        self.assertEqual("hello\n", file_response(self.root, "data/runs/run_a/record.md")["text"])
        self.assertEqual("{}", file_response(self.root, "data/logs/console/session/cleanup.json")["text"])
        self.assertEqual("[]", file_response(self.root, "data/benchmarks/batch/manifest.json")["text"])
        self.assertEqual(self.root / "data/runs", resolve_allowed(self.root, "data/runs"))
        (self.root / "data/environment.txt").write_text("Gazebo Sim 8.15.0\n")
        for path in ("docs/record.md", "docs", "data", "data/environment.txt", "data/logs"):
            with self.subTest(path=path), self.assertRaises(ConsoleError) as caught:
                resolve_allowed(self.root, path)
            self.assertEqual("bad_path", caught.exception.code)

    def test_traversal_absolute_and_other_roots_rejected(self):
        self.record()
        cases = ["data/runs/../../docs/record.md", "data/runs/run_a/../../../record.md", "../data/runs/run_a/record.md",
                 "/etc/passwd", str(self.root / "data/runs/run_a/record.md"), "src/file.md",
                 "logs/console/session", "docs/archive/record.md", "data/logs/other.txt",
                 "data\\runs\\run_a\\record.md", "data/runs/run_a/record.md\0", "data/runs\nrecord.md", ""]
        for path in cases:
            with self.subTest(path=path), self.assertRaises(ConsoleError):
                resolve_allowed(self.root, path)

    def test_symlink_escape_and_allowlisted_root_symlink_rejected(self):
        self.record()
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        (self.root / "data/runs/run_a/escape.txt").symlink_to(outside)
        (self.root / "data/runs/run_a/docs.md").symlink_to(self.root / "docs/record.md")
        for path in ("data/runs/run_a/escape.txt", "data/runs/run_a/docs.md"):
            with self.subTest(path=path), self.assertRaises(ConsoleError):
                file_response(self.root, path)
        (self.root / "data/runs/run_a/internal.md").symlink_to(self.root / "data/runs/run_a/record.md")
        self.assertEqual("hello\n", file_response(self.root, "data/runs/run_a/internal.md")["text"])
        for link, target in (("data", "data"), ("data/runs", "data/runs")):
            with self.subTest(link=link):
                separate = Path(tempfile.mkdtemp(dir=self.root))
                (separate / link).parent.mkdir(parents=True, exist_ok=True)
                (separate / link).symlink_to(self.root / target, target_is_directory=True)
                with self.assertRaises(ConsoleError):
                    file_response(separate, "data/runs/run_a/record.md")
                with self.assertRaises(ConsoleError):
                    resolve_allowed(separate, "data/runs/run_a")

    def test_open_images_and_folders_but_never_serve_them(self):
        image = self.root / "data/runs/run_a/image.png"
        image.write_bytes(b"png")
        self.assertEqual(image, resolve_allowed(self.root, "data/runs/run_a/image.png"))
        for path in ("data/runs/run_a/image.png", "data/runs/run_a"):
            with self.subTest(path=path), self.assertRaises(ConsoleError) as caught:
                file_response(self.root, path)
            self.assertEqual(422, caught.exception.status)

    def test_size_limit_is_bytes_and_new_text_suffixes_work(self):
        for suffix in ("jsonl", "log", "yaml", "csv", "txt"):
            path = self.root / f"data/runs/run_a/read.{suffix}"
            path.write_text("test")
            self.assertEqual("test", file_response(self.root, f"data/runs/run_a/read.{suffix}")["text"])
        path = self.root / "data/runs/run_a/large.txt"
        path.write_bytes(b"x" * MAX_TEXT_BYTES)
        self.assertEqual(MAX_TEXT_BYTES, len(file_response(self.root, "data/runs/run_a/large.txt")["text"]))
        path.write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
        with self.assertRaises(ConsoleError) as caught:
            file_response(self.root, "data/runs/run_a/large.txt")
        self.assertEqual("file_too_large", caught.exception.code)

    def test_results_keep_console_runs_and_benchmarks_separate(self):
        for rel in ("data/runs/run_one", "data/runs/run_two", "data/benchmarks/batch_one",
                    "docs/archive/visual/objective1/run_old", "docs/archive/controllers/run_20261001_paired"):
            directory = self.root / rel
            directory.mkdir(parents=True)
            (directory / "CHECKSHEET.md").write_text("record")
            (directory / "image.png").write_bytes(b"png")
        runs = list_results(self.root)["runs"]
        self.assertEqual(3, len(runs))
        kinds = {r["path"]: r["dataset_kind"] for r in runs}
        self.assertEqual({"data/runs/run_one": "console", "data/runs/run_two": "console",
                          "data/benchmarks/batch_one": "benchmark"}, kinds)
        benchmark = next(r for r in runs if r["dataset_kind"] == "benchmark")
        self.assertIn("Exploration benchmark", benchmark["label"])
        for run in runs:
            self.assertEqual(["CHECKSHEET.md"], [f["name"] for f in run["files"]])
            self.assertEqual(run["path"] + "/CHECKSHEET.md", run["files"][0]["path"])
            self.assertIsNone(run["map"])

    def test_results_metadata_symlink_cannot_read_outside_allowed_roots(self):
        import json
        directory = self.root / "data/runs/run_symlink"
        directory.mkdir(parents=True)
        (directory / "CHECKSHEET.md").write_text("record")
        outside = self.root / "outside_metadata.json"
        outside.write_text(json.dumps([{"map": "private_external_map_name", "survivor_count": 99}]))
        (directory / "records.json").symlink_to(outside)
        runs = list_results(self.root)["runs"]
        self.assertEqual(1, len(runs))
        self.assertIsNone(runs[0]["map"])
        self.assertNotIn("private_external_map_name", str(runs))
        self.assertEqual(["CHECKSHEET.md"], [file["name"] for file in runs[0]["files"]])
        # The same records inside the run are read and keep the map label.
        (directory / "records.json").unlink()
        (directory / "records.json").write_text(json.dumps([{"map": "proposed_system"}]))
        self.assertEqual("proposed_system", list_results(self.root)["runs"][0]["map"])

    def _console_run(self, run_id, action="map.start", *, output=None, clean=False):
        import json
        directory = self.root / "data/logs/console" / run_id
        directory.mkdir(parents=True)
        record = {"run_id": run_id, "action_id": action, "status": "error" if not clean else "idle"}
        if output is not None:
            record["output_dir"] = str(output)
        (directory / "run.json").write_text(json.dumps(record))
        (directory / "cleanup.json").write_text(json.dumps({"clean": clean, "survivors": 0}))
        (directory / "cleanup_recheck.json").write_text(json.dumps({"clean": True, "survivors": 0}))
        (directory / "session.log").write_text("retained event log")
        return directory

    def test_results_discover_log_only_mapping_and_environment_jobs(self):
        mapping = self._console_run("run_mapping_no_save")
        job = self._console_run("run_offline_checks", "env.check", clean=True)
        runs = list_results(self.root)["runs"]
        self.assertEqual(2, len(runs))
        by_id = {run["id"]: run for run in runs}
        for directory in (mapping, job):
            row = by_id[directory.name]
            self.assertEqual(directory.relative_to(self.root).as_posix(), row["path"])
            self.assertEqual("console", row["dataset_kind"])
            self.assertEqual(["cleanup.json", "cleanup_recheck.json"], [f["name"] for f in row["files"][:2]])
            for file in row["files"]:
                self.assertEqual(file["path"], file_response(self.root, file["path"])["path"])
        self.assertIn("clean=false", by_id[mapping.name]["note"])
        self.assertIn("env.check", by_id[job.name]["note"])

    def test_console_output_and_authoritative_audit_merge_without_duplicate_rows(self):
        run_id = "run_mapping_saved"
        output = self.root / "data/runs" / run_id
        saved_map = output / "maps/test_console_map/test_console_map.yaml"
        saved_map.parent.mkdir(parents=True)
        saved_map.write_text("image: test_console_map.pgm\n")
        self._console_run(run_id, output=output)
        runs = list_results(self.root)["runs"]
        self.assertEqual(1, len(runs))
        row = runs[0]
        self.assertEqual(output.relative_to(self.root).as_posix(), row["path"])
        self.assertEqual(["cleanup.json", "cleanup_recheck.json"], [f["name"] for f in row["files"][:2]])
        links = {file["path"] for file in row["files"]}
        self.assertIn(saved_map.relative_to(self.root).as_posix(), links)
        self.assertIn(f"data/logs/console/{run_id}/cleanup.json", links)
        self.assertIn("map.start; error", row["note"])
        self.assertIn("Original cleanup clean=false", row["note"])
        self.assertEqual(len(links), len(row["files"]))

    def test_logs_merge_by_record_output_path_and_preserve_map_datasets(self):
        import json
        run_id = "20261027_nav_start"
        outputs = {}
        for name, map_name in ((run_id, "proposed_system"), ("run_" + run_id, "tb3_sandbox")):
            output = self.root / "data/runs" / name
            output.mkdir(parents=True)
            (output / "CHECKSHEET.md").write_text("navigation records")
            (output / "records.json").write_text(json.dumps([{"map": map_name, "survivor_count": 0}]))
            outputs[map_name] = output
        self._console_run(run_id, "nav.start", output=outputs["tb3_sandbox"], clean=True)
        runs = list_results(self.root)["runs"]
        self.assertEqual(2, len(runs))
        by_map = {run["map"]: run for run in runs}
        self.assertEqual({"proposed_system", "tb3_sandbox"}, set(by_map))
        self.assertTrue(all(run["dataset_kind"] == "console" for run in runs))
        self.assertTrue(any(file["path"].endswith("/cleanup.json") for file in by_map["tb3_sandbox"]["files"]))
        self.assertIn("nav.start", by_map["tb3_sandbox"]["note"])
        self.assertFalse(any(file["path"].startswith("data/logs/") for file in by_map["proposed_system"]["files"]))

    def test_console_log_discovery_and_metadata_respect_symlink_boundaries(self):
        import json
        run_id = "run_logs_escape"
        directory = self.root / "data/logs/console" / run_id
        directory.mkdir(parents=True)
        (directory / "cleanup.json").write_text('{"clean": false, "survivors": 0}')
        outside = self.root / "outside_console_record.json"
        outside.write_text(json.dumps({"run_id": run_id, "action_id": "private_external_action", "status": "private"}))
        (directory / "run.json").symlink_to(outside)
        outside_folder = self.root / "outside_folder"
        outside_folder.mkdir()
        (outside_folder / "cleanup.json").write_text("private external cleanup")
        (self.root / "data/logs/console/escape_folder").symlink_to(outside_folder, target_is_directory=True)
        runs = list_results(self.root)["runs"]
        self.assertEqual(1, len(runs))
        self.assertEqual(run_id, runs[0]["id"])
        self.assertNotIn("private_external_action", str(runs))
        self.assertNotIn("private external cleanup", str(runs))
        self.assertEqual(["cleanup.json"], [file["name"] for file in runs[0]["files"]])

    def test_ambiguous_map_run_ids_do_not_merge_logs_into_an_arbitrary_dataset(self):
        run_id = "ambiguous_run"
        for rel in ("data/runs/" + run_id, "data/runs/run_" + run_id, "data/benchmarks/" + run_id):
            output = self.root / rel
            output.mkdir(parents=True)
            (output / "CHECKSHEET.md").write_text("dataset")
        self._console_run(run_id, "map.start")
        runs = list_results(self.root)["runs"]
        self.assertEqual(4, len(runs))
        self.assertEqual(1, sum(run["path"].startswith("data/logs/console/") for run in runs))
        for run in runs:
            if not run["path"].startswith("data/logs/console/"):
                self.assertFalse(any(file["path"].startswith("data/logs/") for file in run["files"]))


if __name__ == "__main__":
    unittest.main()
