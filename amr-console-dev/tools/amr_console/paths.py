"""Allowlisted record access and separately labelled result datasets."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import stat

from errors import ConsoleError


TEXT_SUFFIXES = {".md", ".csv", ".json", ".jsonl", ".yaml", ".txt", ".log"}
MAX_TEXT_BYTES = 1024 * 1024
# Run output (data/ is created on demand and never committed).
RECORD_ROOTS = ("data/runs", "data/benchmarks", "data/logs/console")


def resolve_allowed(root, path, *, read=False):
    """Resolve a repo-relative path inside the data folders, fail closed."""
    root = Path(root).resolve()
    if not isinstance(path, str) or not path or len(path) > 4096 or "\\" in path or any(ord(c) < 32 for c in path):
        raise ConsoleError(404, "bad_path", "Use a repo-relative path under data/runs/, data/benchmarks/ or data/logs/console/.")
    supplied = PurePosixPath(path)
    if supplied.is_absolute() or ".." in supplied.parts:
        raise ConsoleError(404, "bad_path", "Paths may not be absolute or contain '..'.")
    allowed = tuple(root / directory for directory in RECORD_ROOTS)
    lexical = root.joinpath(*supplied.parts)
    if not any(lexical.is_relative_to(directory) for directory in allowed):
        raise ConsoleError(404, "bad_path", "Only data/runs/, data/benchmarks/ and data/logs/console/ records may be opened.")
    try:
        real = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConsoleError(404, "path_not_found", "That record path does not exist or cannot be resolved.") from exc
    # Compare with the literal directories under the canonical workspace root,
    # so even an allowlisted root symlink pointing outside the repo is rejected.
    if not any(real.is_relative_to(directory) for directory in allowed):
        raise ConsoleError(404, "bad_path", "The resolved path escapes the allowed record folders.")
    if not (real.is_file() or real.is_dir()):
        raise ConsoleError(404, "bad_path", "Only ordinary record files or directories may be opened.")
    if read:
        if not real.is_file():
            raise ConsoleError(422, "not_file", "Choose a text file; folders can be opened with Open folder.")
        if real.suffix.lower() not in TEXT_SUFFIXES:
            raise ConsoleError(422, "unsupported_file", "Images and video can be opened from the run folder; only record text is served.")
        if real.stat().st_size > MAX_TEXT_BYTES:
            raise ConsoleError(422, "file_too_large", "This file exceeds 1 MB; open its run folder to read it externally.")
    return real


def file_response(root, path):
    target = resolve_allowed(root, path, read=True)
    try:
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ConsoleError(404, "bad_path", "The record is not an ordinary file.")
            data = stream.read(MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise ConsoleError(404, "path_not_found", "The record changed or cannot be read; refresh Results.") from exc
    if len(data) > MAX_TEXT_BYTES:
        raise ConsoleError(422, "file_too_large", "This file exceeds 1 MB; open its run folder to read it externally.")
    return {"path": target.relative_to(Path(root).resolve()).as_posix(),
            "text": data.decode("utf-8", errors="replace")}


def _read_json(root, path):
    try:
        relative = path.relative_to(Path(root).resolve()).as_posix()
        return json.loads(file_response(root, relative)["text"])
    except (ConsoleError, OSError, ValueError):
        pass
    return None


def _files(root, directory):
    """Readable text only; images/video remain accessible through Open folder."""
    found = []
    for path in directory.rglob("*"):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            safe = resolve_allowed(root, path.relative_to(root).as_posix(), read=True)
        except (ConsoleError, ValueError, OSError):
            continue
        found.append({"name": str(path.relative_to(directory)),
                      "path": safe.relative_to(root).as_posix()})
    return _sort_files(found)


def _sort_files(files):
    priorities = {"cleanup.json": 0, "cleanup_recheck.json": 1, "CHECKSHEET.md": 2,
                  "run.json": 3, "records.json": 4, "records.csv": 5, "summary.json": 6}
    files.sort(key=lambda file: (priorities.get(Path(file["name"]).name, 20),
                                 file["name"], file["path"]))
    return files


def _merge_files(output_files, console_files):
    """One link per resolved file, with distinct names for duplicate basenames."""
    result = list(output_files)
    known_paths = {file["path"] for file in result}
    names = {file["name"] for file in result}
    for file in console_files:
        if file["path"] in known_paths:
            continue
        item = dict(file)
        if item["name"] in names:
            item["name"] = "console/" + item["name"]
        result.append(item)
        known_paths.add(item["path"])
        names.add(item["name"])
    return _sort_files(result)


def _survivor_count(row):
    if not isinstance(row, dict):
        return 0
    value = row.get("survivor_count")
    if type(value) is int and value >= 0:
        return value
    survivors = row.get("survivors")
    return len(survivors) if isinstance(survivors, list) else 0


def _console_note(run_record, cleanup):
    parts = []
    if isinstance(run_record, dict):
        parts.append(f"Console {run_record.get('action_id', 'session')}; {run_record.get('status', 'recorded')}.")
    if isinstance(cleanup, dict):
        clean = cleanup.get("clean")
        survivors = cleanup.get("survivors")
        if isinstance(survivors, list):
            survivors = len(survivors)
        if type(clean) is bool and type(survivors) is int:
            parts.append(f"Original cleanup clean={'true' if clean else 'false'}, survivors={survivors}.")
    return " ".join(parts) or "Authoritative console logs and cleanup records."


def _output_target(root, directory, run_record, rows):
    """Match only this run's safe output folder, never an arbitrary record path."""
    candidates = []
    if isinstance(run_record, dict) and run_record.get("run_id") == directory.name:
        output = run_record.get("output_dir")
        if isinstance(output, str):
            path = Path(output)
            try:
                relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
                safe = resolve_allowed(root, relative)
                if (safe.is_relative_to(root / "data/runs") and
                        safe.name in {directory.name, "run_" + directory.name}):
                    candidates.append(safe)
            except (ConsoleError, ValueError, OSError):
                pass
    for candidate in candidates:
        if candidate in rows:
            return rows[candidate]
    matches = [path for path in rows if path.is_relative_to(root / "data/runs")
               and path.name in {directory.name, "run_" + directory.name}]
    # A repeated ID on distinct map roots must not attach an audit to a map by
    # iteration order; the authoritative output path disambiguates that case.
    if len(matches) == 1:
        return rows[matches[0]]
    return None


def list_results(root):
    """Discover existing run folders, without pooling their map datasets."""
    root = Path(root).resolve()
    candidates = []
    for subtree, kind in (("data/runs", "console"), ("data/benchmarks", "benchmark")):
        parent = root / subtree
        if parent.is_dir():
            for directory in parent.iterdir():
                if directory.is_dir():
                    candidates.append((directory, kind, None))
    runs = []
    by_directory = {}
    for directory, kind, map_name in candidates:
        try:
            safe = resolve_allowed(root, directory.relative_to(root).as_posix())
            files = _files(root, safe)
            modified = safe.stat().st_mtime
        except (ConsoleError, ValueError, OSError):
            continue
        if not files or safe in by_directory:
            continue
        records = _read_json(root, safe / "records.json")
        run_record = _read_json(root, safe / "run.json")
        if isinstance(records, dict):
            records = records.get("records", records.get("trials"))
        rows = records if isinstance(records, list) else []
        maps = sorted({str(row["map"]) for row in rows if isinstance(row, dict) and row.get("map")})
        if maps:
            map_name = ", ".join(maps)
        if kind == "benchmark":
            label = f"Exploration benchmark — {directory.name}"
            note = "Batch of exploration runs (manifest.json, batch.log, scores)."
        else:
            label = f"Console session — {directory.name}"
            note = "Console mapping/navigation records."
            if isinstance(run_record, dict):
                note = f"Console {run_record.get('action_id', 'session')}; {run_record.get('status', 'recorded')}."
        row = {"id": directory.name, "label": label, "note": note,
               "path": safe.relative_to(root).as_posix(), "files": files,
               "dataset_kind": kind, "map": map_name, "_modified": modified}
        runs.append(row)
        by_directory[safe] = row
    # Console run/cleanup files are authoritative and live outside the output
    # trees. Mapping without a save and environment jobs may have no docs run.
    # Merge their links into an existing output row, or create a log-only row.
    console_root = root / "data/logs/console"
    if console_root.is_dir():
        for directory in console_root.iterdir():
            if not directory.is_dir():
                continue
            try:
                safe = resolve_allowed(root, directory.relative_to(root).as_posix())
                files = _files(root, safe)
                modified = safe.stat().st_mtime
            except (ConsoleError, ValueError, OSError):
                continue
            if not files:
                continue
            run_record = _read_json(root, safe / "run.json")
            cleanup = _read_json(root, safe / "cleanup.json")
            row = _output_target(root, directory, run_record, by_directory)
            console_note = _console_note(run_record, cleanup)
            if row is not None:
                row["files"] = _merge_files(row["files"], files)
                row["_modified"] = max(row["_modified"], modified)
                if row["dataset_kind"] == "console":
                    row["note"] = console_note
                else:
                    row["note"] += " " + console_note
                continue
            if safe in by_directory:
                continue
            row = {"id": directory.name, "label": f"Console session — {directory.name}",
                   "note": console_note, "path": safe.relative_to(root).as_posix(),
                   "files": files, "dataset_kind": "console", "map": None,
                   "_modified": modified}
            runs.append(row)
            by_directory[safe] = row
    runs.sort(key=lambda r: (r["_modified"], r["id"]), reverse=True)
    for run in runs:
        run.pop("_modified")
    return {"runs": runs}
