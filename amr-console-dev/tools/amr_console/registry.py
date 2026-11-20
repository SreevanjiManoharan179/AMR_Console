"""Inventory-backed actions, strict parameter validation and real command argv.

This module has no ROS imports and starts no processes. The worlds come from
config/worlds.yaml; the only other YAML read here is the small human-maintained
approval format and the saved-map fields needed to validate map coordinates.
Neither is interpreted as executable code.
"""
from __future__ import annotations

import ast
import math
from pathlib import Path
import re
import shlex

import yaml

from errors import ConsoleError


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROLLERS = ("mppi", "dwb", "rpp")
TEXT_PATTERN = r"^[a-z0-9_]{3,40}$"
_RUN_ID = re.compile(r"^[a-zA-Z0-9_\-]{3,120}$")
_WORLD_ID = re.compile(r"^[a-z0-9_]{3,40}$")


def load_worlds(path=REPO_ROOT / "config/worlds.yaml"):
    """Read config/worlds.yaml: {world_id: entry}, the default first. Fails loudly when malformed."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    worlds = data.get("worlds") if isinstance(data, dict) else None
    if not isinstance(worlds, dict) or not worlds:
        raise ValueError(f"{path}: 'worlds' must map world ids to entries")
    for world_id, entry in worlds.items():
        if not _WORLD_ID.fullmatch(str(world_id)) or not isinstance(entry, dict):
            raise ValueError(f"{path}: bad world id or entry {world_id!r}")
        if not isinstance(entry.get("map"), str) or entry.get("world") not in (None,) and not isinstance(entry["world"], str):
            raise ValueError(f"{path}: {world_id} needs a map path and a world path or null")
        spawn = entry.get("spawn", [-2.0, -0.5, 0.0])
        if (not isinstance(spawn, list) or len(spawn) != 3 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in spawn)):
            raise ValueError(f"{path}: {world_id} spawn must be [x, y, yaw]")
    default = data.get("default")
    order = ([default] if default in worlds else []) + [w for w in worlds if w != default]
    return {world_id: worlds[world_id] for world_id in order}


WORLD_CONFIG = load_worlds()
# Every world has a saved map with the same id; the default world comes first.
MAP_IDS = tuple(WORLD_CONFIG)
ALL_MAP_IDS = MAP_IDS
WORLDS = {w: e.get("world") for w, e in WORLD_CONFIG.items()}
MAP_FILES = {w: e["map"] for w, e in WORLD_CONFIG.items()}
SPAWNS = {w: list(e.get("spawn", [-2.0, -0.5, 0.0])) for w, e in WORLD_CONFIG.items()}
LABELS = {w: e.get("label", w) for w, e in WORLD_CONFIG.items()}
# RViz px per m for each world view, so the whole map fits.
VIEW_SCALES = {w: int(e.get("view_scale", 45)) for w, e in WORLD_CONFIG.items()}
# Kept for older callers: a saved map -> the world file it needs (worlds with their own file).
OPTIONAL_MAPS = {w: w for w, path in WORLDS.items() if path}
# Autonomous exploration (the exploration benchmark design): the explorer's goal rules, first the proposed one.
EXPLORE_METHODS = ("ours", "gain", "nearest")
EXPLORE_WORLDS = tuple(w for w, e in WORLD_CONFIG.items() if e.get("explore") and e.get("world"))
EXPLORE_FILES = ["src/amr_physical_ai/launch/tb3_mapping.launch.py",
                 "src/amr_physical_ai/rviz/tb3_navigation.rviz",
                 "src/amr_physical_ai/config/nav2_mppi.yaml",
                 "src/amr_physical_ai/amr_physical_ai/exploration.py",
                 "src/amr_physical_ai/amr_physical_ai/exploration_run.py",
                 "src/amr_physical_ai/amr_physical_ai/explore_node.py"]
# Delivery-command maps: each has its own named places, approval key and grounding calibration.
COMMAND_MAPS = {w: {"approval": e["approval"], "locations": e["places"], "grounding_results": e["grounding"],
               **({"tags": e["tags"]} if e.get("tags") else {})}
           for w, e in WORLD_CONFIG.items() if e.get("places") and e.get("grounding") and e.get("approval")}
_OPTIONAL_APPROVALS = {profile["approval"]: False for profile in COMMAND_MAPS.values()}


def map_files(map_id):
    """Repository files a saved map needs, including its world when it has its own."""
    stem = MAP_FILES[map_id][:-len(".yaml")] if MAP_FILES[map_id].endswith(".yaml") else MAP_FILES[map_id]
    files = [stem + ".yaml", stem + ".pgm"]
    world = WORLDS.get(map_id)
    return files + ([world] if world else [])


def read_approvals(path):
    """Read the human-maintained approval YAML, failing closed on errors.

    Each delivery map has one key (for example ``proposed_system_places``); every key
    is optional and false when absent. Unknown or repeated keys make the whole file invalid.
    """
    defaults = dict(_OPTIONAL_APPROVALS)
    try:
        content = Path(path).read_text(encoding="utf-8")
        if len(content) > 16384:
            raise ValueError("approval file exceeds 16 KB")
        found = {}
        for line in content.splitlines():
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            key, separator, raw = text.partition(":")
            if not separator or key not in defaults or key in found:
                raise ValueError("use each supported key once")
            raw = raw.strip()
            if raw not in {"true", "false"}:
                raise ValueError("approvals must be true or false")
            found[key] = raw == "true"
        return dict(defaults, **found)
    except (OSError, ValueError) as exc:
        return dict(defaults, error=f"Approvals could not be read safely: {exc}.")


def places_confirmed(root, map_id=None):
    """Read the explicit confirmation line; full YAML/map validation is in command_pipeline."""
    map_id = map_id or next(iter(COMMAND_MAPS), None)
    try:
        text = (Path(root) / COMMAND_MAPS[map_id]['locations']).read_text()
        values = re.findall(r'^approval_status:\s*([^\n#]*?)\s*(?:#.*)?$', text, re.MULTILINE)
        return values == ['confirmed']
    except (OSError, KeyError, ValueError):   # ValueError covers a file that is not UTF-8
        return False


def command_places(root):
    """Display names of each delivery map's places, read from the name lines only."""
    places = {}
    for map_id, profile in COMMAND_MAPS.items():
        try:
            text = (Path(root) / profile['locations']).read_text()
        except (OSError, ValueError):
            continue
        names = re.findall(r'^\s*-\s*name:\s*([a-z0-9_]+)\s*$', text, re.MULTILINE)
        if names:
            places[map_id] = [name.replace('_', ' ') for name in names]
    return places


def commands_ready(root, approval, map_id):
    return bool(approval.get(COMMAND_MAPS[map_id]['approval'])) and places_confirmed(root, map_id)


def _param(name, label, type_, default, **extra):
    return dict(name=name, label=label, type=type_, default=default, **extra)


def _action(id_, tab, label, description, *, params=None, session=None,
            requires_session=None, operation=False, teleop=False,
            command="", lock_reason="", confirm_phrase=None):
    return dict(id=id_, tab=tab, label=label, description=description,
                params=params or [], command=command,
                kind="operation" if operation else "command", session=session,
                requires_session=requires_session, teleop=teleop,
                locked=bool(lock_reason), lock_reason=lock_reason,
                confirm_phrase=confirm_phrase)


def actions(root, environment, approvals_path=None):
    """Return the complete registry; re-read approvals on every call."""
    root = Path(root).resolve()
    approval = read_approvals(approvals_path or root / "tools/amr_console/approvals.yaml")
    command_missing = []
    if not any(commands_ready(root, approval, map_id) for map_id in COMMAND_MAPS):
        for map_id, profile in COMMAND_MAPS.items():
            if not approval.get(profile["approval"]):
                command_missing.append(f"{profile['approval']}: true in tools/amr_console/approvals.yaml")
            if not places_confirmed(root, map_id):
                command_missing.append(f"approval_status: confirmed in {profile['locations']}")
        if not COMMAND_MAPS:
            command_missing.append("a world with named places and a grounding calibration in config/worlds.yaml")
        if approval.get("error"):
            command_missing.insert(0, approval["error"])
    # A world appears only when all its files are present.
    present = {m: [f for f in map_files(m) if not (root / f).is_file()] for m in MAP_IDS}
    maps = [m for m, missing in present.items() if not missing]
    locked = {m: "Missing: " + ", ".join(missing) + "." for m, missing in present.items() if missing}
    explore_worlds = [m for m in EXPLORE_WORLDS if m in maps]
    explore_locked = {m: locked[m] for m in EXPLORE_WORLDS if m in locked}
    default_world = maps[0] if maps else MAP_IDS[0]
    # Display names for world/map options (ids stay the values the API accepts).
    world_labels = {w: LABELS[w] for w in MAP_IDS}
    registry = [
        _action("env.build", "environment", "Build the workspace",
                "Runs the build script. Refused while any console session or terminal simulation is active.",
                command="bash scripts/build.sh"),
        _action("env.check", "environment", "Run offline checks",
                "Runs the offline tests and map checks. Refused while a terminal simulation is active.",
                command="bash scripts/check.sh"),
        _action("map.start", "map", "Start mapping",
                "Opens Gazebo and RViz with the TurtleBot3 Waffle and SLAM Toolbox in the chosen world. "
                "The drive pad is enabled only for this session.",
                params=[_param("world", "World", "select", default_world, options=maps,
                               locked_options=locked, option_labels=world_labels)],
                session="mapping", teleop=True,
                command="ros2 launch --noninteractive amr_physical_ai tb3_mapping.launch.py [world:=<world file>]"),
        _action("map.explore", "map", "Explore automatically",
                "Opens Gazebo and RViz with SLAM Toolbox and Nav2 (MPPI); the explorer chooses every goal "
                "until no reachable frontier is left. ours: the largest unseen areas first, then a sweep of the small "
                "gaps and corners, then home. gain and nearest are the benchmark baselines. Maps, Gazebo truth "
                "and every goal are recorded for scoring. Nav2 drives, so the drive pad stays off; Save the map works "
                "during and after the run.",
                params=[_param("world", "World", "select", explore_worlds[0] if explore_worlds else default_world,
                               options=explore_worlds, locked_options=explore_locked, option_labels=world_labels),
                        _param("method", "Method", "select", EXPLORE_METHODS[0], options=list(EXPLORE_METHODS))],
                session="mapping",
                command="ros2 launch --noninteractive amr_physical_ai tb3_mapping.launch.py world:=<world file> "
                        "explore:=<method> explore_out:=<new exploration folder> params_file:=<nav2_mppi.yaml> "
                        "ground_truth:=true",
                lock_reason="" if explore_worlds else "Needs a world with an exact map (explore: true in config/worlds.yaml)."),
        _action("map.save", "map", "Save the map",
                "Saves through SLAM Toolbox into a new run folder. Checks result=0 and both YAML and PGM files.",
                params=[_param("name", "Map name", "text", "map_", pattern=TEXT_PATTERN,
                               hint="lowercase, digits and underscores; 3–40 characters")],
                requires_session="mapping", operation=True,
                command="Call /slam_toolbox/save_map and check the new YAML and PGM files."),
        _action("nav.start", "navigate", "Start navigation",
                "Opens Gazebo and RViz with AMCL and Nav2 on a saved map and controller parameter file, "
                "in the world the map belongs to.",
                params=[_param("map", "Map", "select", default_world, options=maps, locked_options=locked,
                               option_labels=world_labels),
                        _param("controller", "Controller", "select", "mppi", options=list(CONTROLLERS))],
                session="navigation",
                command="ros2 launch --noninteractive amr_physical_ai tb3_navigation.launch.py map:=<saved map YAML> params_file:=<controller YAML> [world:=<world file>]"),
        _action("nav.goal", "navigate", "Send a goal",
                "Sends one NavigateToPose goal inside the loaded map's bounds. Bounds alone do not establish reachability.",
                params=[_param("x", "x (m)", "number", .8, step=.05),
                        _param("y", "y (m)", "number", 0., step=.05),
                        _param("yaw", "yaw (rad)", "number", 0., step=.1)],
                requires_session="navigation", operation=True,
                command="Send NavigateToPose to the running session with a quaternion computed from yaw."),
        _action("commands.typed", "commands", "Send a typed command",
                "Rule-based (no LLM). Grounds one typed request to an approved place on the running session's map, "
                "asks when unsure, navigates and records the Gazebo true final error.",
                params=[_param("text", "Request", "text", "go to the reception", max_length=300, required=True)],
                requires_session="navigation", operation=True,
                command="Rule-based: ground one request, navigate and save its record.",
                lock_reason=("Needs " + "; ".join(command_missing) + ".") if command_missing else ""),
    ]
    # Shown in the Commands tab for the running session's map (display only; the typed-command pipeline reads the YAML itself).
    command = find_action(registry, "commands.typed")
    command["places"] = command_places(root)
    command["places_ready"] = [m for m in COMMAND_MAPS if commands_ready(root, approval, m)]
    # Presence is a readiness requirement, not an instruction to install/build.
    required = {
        "env.build": ["scripts/build.sh"], "env.check": ["scripts/check.sh"],
        "map.start": ["src/amr_physical_ai/launch/tb3_mapping.launch.py", "src/amr_physical_ai/rviz/tb3_mapping.rviz"],
        "map.explore": list(EXPLORE_FILES),
        "nav.start": ["src/amr_physical_ai/launch/tb3_navigation.launch.py", "src/amr_physical_ai/rviz/tb3_navigation.rviz",
                      *[f"src/amr_physical_ai/config/nav2_{c}.yaml" for c in CONTROLLERS]],
        "commands.typed": ["tools/amr_console/typed_commands.py", "src/amr_physical_ai/amr_physical_ai/command_pipeline.py"],
    }
    required_helpers = ["src/amr_physical_ai/scripts/gazebo_stop.py"]
    for id_ in ("map.start", "map.explore", "nav.start"):
        required[id_].extend(required_helpers)
    for action in registry:
        missing = [str(path) for path in required.get(action["id"], []) if not (root / path).is_file()]
        if missing:
            action["locked"] = True
            action["lock_reason"] = "Required assets are missing: " + ", ".join(missing) + "."
        if environment.get("ros_ready") is False and action["id"] not in {"env.build", "env.check"}:
            action["locked"] = True
            ros_reason = environment.get("ros_reason") or "Source ROS 2 Jazzy and the workspace setup before starting the console."
            action["lock_reason"] = ((action["lock_reason"] + " ") if action["lock_reason"] else "") + ros_reason
    return registry


def find_action(registry, action_id):
    for action in registry:
        if action["id"] == action_id:
            return action
    raise ConsoleError(404, "unknown_action", f"No action called {action_id}.")


def validate(action, params, map_bounds=None):
    """Apply defaults and reject unknown keys, coercions and unsafe values."""
    if not isinstance(params, dict):
        raise ConsoleError(422, "invalid_params", "Parameters must be a JSON object.")
    definitions = {p["name"]: p for p in action.get("params", [])}
    unknown = set(params) - set(definitions)
    if unknown:
        raise ConsoleError(422, "unknown_param", "Unknown parameters: " + ", ".join(sorted(map(str, unknown))) + ".")
    values = {}
    for name, p in definitions.items():
        value = params.get(name, p.get("default"))
        type_ = p["type"]
        label = p["label"]
        if type_ == "bool":
            valid = type(value) is bool
        elif type_ == "int":
            valid = type(value) is int
        elif type_ == "number":
            valid = type(value) in (int, float) and math.isfinite(value)
        elif type_ == "text":
            valid = isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            if valid and len(value) > p.get("max_length", 4096):
                raise ConsoleError(422, "invalid_param", f"{label} must be at most {p.get('max_length', 4096)} characters.")
            if valid and p.get("required") and not value.strip():
                raise ConsoleError(422, "invalid_param", f"{label} must not be empty.")
        elif type_ == "select":
            if isinstance(value, str) and value in p.get("locked_options", {}):
                raise ConsoleError(409, "locked_option", p["locked_options"][value])
            valid = isinstance(value, str) and value in p["options"]
        elif type_ == "multi":
            valid = (isinstance(value, list) and bool(value) and
                     all(isinstance(v, str) and v in p["options"] for v in value) and
                     len(value) == len(set(value)))
            if valid:
                value = [option for option in p["options"] if option in value]
        else:
            raise ConsoleError(500, "registry_error", f"Unsupported parameter type {type_}.")
        if not valid:
            raise ConsoleError(422, "invalid_param", f"{label} must have the declared {type_} type and allowed value.")
        if type_ in {"int", "number"}:
            if ("min" in p and value < p["min"]) or ("max" in p and value > p["max"]):
                raise ConsoleError(422, "invalid_param", f"{label} must be between {p.get('min', '-∞')} and {p.get('max', '∞')}.")
        if type_ == "text" and p.get("pattern") and not re.fullmatch(p["pattern"], value):
            raise ConsoleError(422, "invalid_param", f"{label} must use {p.get('hint', 'the allowed format')}.")
        if p.get("disabled_reason") and value != p.get("default"):
            raise ConsoleError(409, "disabled_param", p["disabled_reason"])
        values[name] = value
    if action["id"] == "nav.goal" and map_bounds is not None:
        if not goal_in_bounds(values["x"], values["y"], map_bounds):
            raise ConsoleError(422, "goal_outside_map", "The goal must be inside the loaded map's bounds.")
    return values


def map_bounds(root, map_id):
    """Read finite metadata and a PGM header for an allowlisted saved map."""
    if map_id not in ALL_MAP_IDS:
        raise ConsoleError(422, "unknown_map", "Choose an allowlisted saved map.")
    path = Path(root).resolve() / MAP_FILES[map_id]
    try:
        fields = {}
        for line in path.read_text().splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        resolution = float(fields["resolution"])
        origin = ast.literal_eval(fields["origin"])
        if (not math.isfinite(resolution) or resolution <= 0 or
                not isinstance(origin, (list, tuple)) or len(origin) != 3 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in origin)):
            raise ValueError("invalid resolution or origin")
        image_name = fields["image"].strip("\"'")
        image = Path(image_name)
        if image.is_absolute() or ".." in image.parts:
            raise ValueError("unsafe map image path")
        image_path = (path.parent / image).resolve()
        if not image_path.is_relative_to(path.parent):
            raise ValueError("map image escapes its directory")
        with image_path.open("rb") as stream:
            tokens = []
            size = 0
            while len(tokens) < 4 and size < 65536:
                line = stream.readline(4096)
                if not line:
                    break
                size += len(line)
                tokens.extend(line.split(b"#", 1)[0].split())
        if len(tokens) < 4 or tokens[0] not in {b"P2", b"P5"}:
            raise ValueError("unsupported or incomplete PGM header")
        width, height, maximum = (int(v) for v in tokens[1:4])
        if width <= 0 or height <= 0 or not 1 <= maximum <= 65535:
            raise ValueError("invalid PGM dimensions")
        return {"map_id": map_id, "origin": list(origin), "resolution": resolution,
                "width": width, "height": height}
    except (OSError, ValueError, KeyError, SyntaxError) as exc:
        raise ConsoleError(409, "map_unavailable", f"Cannot validate the saved map: {exc}.") from exc


def goal_in_bounds(x, y, bounds):
    origin_x, origin_y, origin_yaw = bounds["origin"]
    dx, dy = x - origin_x, y - origin_y
    local_x = dx * math.cos(origin_yaw) + dy * math.sin(origin_yaw)
    local_y = -dx * math.sin(origin_yaw) + dy * math.cos(origin_yaw)
    return (0 <= local_x < bounds["width"] * bounds["resolution"] and
            0 <= local_y < bounds["height"] * bounds["resolution"])


def build(action, params, run_id, env, session=None):
    """Prepare an execution spec; the caller supplies its workspace root.

    ``session`` carries ``root`` and, for operations, the active session's
    ``run_id`` and ``kind``. This function creates no output directories.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ConsoleError(422, "invalid_run", "Use the backend-generated run ID.")
    session = session or {}
    root = Path(session.get("root", REPO_ROOT)).resolve()
    action_id = action["id"]
    operation = action.get("requires_session") is not None
    if operation:
        run_id = session.get("run_id", run_id)
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ConsoleError(422, "invalid_run", "The running session has no valid backend run ID.")
    log_dir = root / "data/logs/console" / run_id
    output_dir = root / "data/runs" / run_id
    for directory, allowed in ((log_dir, root / "data/logs/console"),
                               (output_dir, root / "data/runs")):
        if not directory.resolve().is_relative_to(allowed):
            raise ConsoleError(409, "output_path_unsafe", "An output folder symlink escapes the workspace; correct it before starting.")
    spec = {"action_id": action_id, "run_id": run_id,
            "kind": action.get("session") or action.get("requires_session") or "job",
            "preview_kind": "operation" if operation else "command", "argv": [],
            "sequence": [], "env": dict(env), "params": dict(params),
            "log_dir": str(log_dir), "output_dir": str(output_dir)}
    nav_prefix = ["ros2", "launch", "--noninteractive", "amr_physical_ai", "tb3_navigation.launch.py"]
    if action_id in {"env.build", "env.check"}:
        spec["argv"] = ["bash", "scripts/build.sh" if action_id == "env.build" else "scripts/check.sh"]
    elif action_id == "map.start":
        world = params.get("world", MAP_IDS[0])
        spec["argv"] = ["ros2", "launch", "--noninteractive", "amr_physical_ai", "tb3_mapping.launch.py"]
        spec["rviz_source"] = str(root / "src/amr_physical_ai/rviz/tb3_mapping.rviz")
        spec["world"] = world
        if WORLDS.get(world):
            spec["argv"].append(f"world:={WORLDS[world]}")
        # Frame the private RViz copy (view and grid) on the world's saved map.
        if all((root / path).is_file() for path in map_files(world)):
            spec["view_map"], spec["view_scale"] = world, VIEW_SCALES[world]
    elif action_id == "map.explore":
        world, method = params["world"], params["method"]
        exploration = output_dir / "exploration"
        if not exploration.resolve().is_relative_to(root / "data/runs"):
            raise ConsoleError(409, "output_path_unsafe", "The exploration folder escapes the console root; correct the symlink first.")
        spec["argv"] = ["ros2", "launch", "--noninteractive", "amr_physical_ai", "tb3_mapping.launch.py",
                        f"world:={WORLDS[world]}", f"explore:={method}",
                        f"explore_out:={exploration.relative_to(root)}",
                        "params_file:=src/amr_physical_ai/config/nav2_mppi.yaml", "ground_truth:=true"]
        spec["rviz_source"] = str(root / "src/amr_physical_ai/rviz/tb3_navigation.rviz")
        spec["world"] = world
        spec["view_map"], spec["view_scale"] = world, VIEW_SCALES[world]
        spec["explore"] = {"method": method, "world": world, "out": str(exploration)}
    elif action_id == "nav.start":
        map_id, controller = params["map"], params["controller"]
        spec["map_id"] = map_id
        spec["map_bounds"] = map_bounds(root, map_id)
        spec["rviz_source"] = str(root / "src/amr_physical_ai/rviz/tb3_navigation.rviz")
        spec["argv"] = nav_prefix + [f"map:={MAP_FILES[map_id]}",
                                      f"params_file:=src/amr_physical_ai/config/nav2_{controller}.yaml"]
        if WORLDS.get(map_id):
            spec["argv"].append(f"world:={WORLDS[map_id]}")
        spec["world"] = map_id
        spec["view_map"], spec["view_scale"] = map_id, VIEW_SCALES[map_id]
        profile = COMMAND_MAPS.get(map_id)
        spec['record_truth'] = bool(profile and commands_ready(root, read_approvals(root / 'tools/amr_console/approvals.yaml'),
                                                     map_id))
        if spec['record_truth']:
            spec['argv'].append('ground_truth:=true')
            spec['command_profile'] = dict(map_id=map_id, approval=profile['approval'],
                                      locations=profile['locations'], grounding_results=profile['grounding_results'])
    elif action_id == "map.save":
        name = params["name"]
        stem = output_dir / "maps" / name / name
        if not stem.resolve().is_relative_to(root / "data/runs"):
            raise ConsoleError(409, "output_path_unsafe", "The map output folder escapes the console root; correct the symlink first.")
        spec["map_stem"] = str(stem)
        spec["operation"] = (f"Call /slam_toolbox/save_map with name.data={stem.relative_to(root)}, "
                             f"then check result=0 and {name}.yaml plus {name}.pgm exist.")
    elif action_id == "nav.goal":
        spec["operation"] = (f"Send NavigateToPose x={params['x']} m, y={params['y']} m, yaw={params['yaw']} rad "
                             "to the running navigation session; STOP cancels the goal.")
    elif action_id == "commands.typed":
        spec["operation"] = ("RULE-BASED FALLBACK (no LLM): validate one request on this session's map; "
                             "use this session's transport and save text, JSON, validation, Nav2 outcome, "
                             "time and Gazebo truth in data/runs/" + run_id + "/commands/<request_id>.json.")
    else:
        raise ConsoleError(404, "unknown_action", f"No action called {action_id}.")
    if action_id in {"map.start", "map.explore", "nav.start"}:
        spec["rviz_config"] = str(log_dir / "rviz_config.rviz")
        spec["argv"].append(f"rviz_config:={Path(spec['rviz_config']).relative_to(root)}")
    if spec["argv"]:
        sequences = spec["sequence"] or [spec["argv"]]
        spec["command"] = "\nthen: ".join(shlex.join(argv) for argv in sequences)
    return spec
