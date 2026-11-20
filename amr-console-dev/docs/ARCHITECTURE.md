# Architecture

```
 Browser (tools/amr_console/web/index.html)
    │  HTTP + server-sent events on 127.0.0.1, one-time token in the page link
    ▼
 Console server (server.py) ── engine.py: state, interlocks, prepared commands, records
    │                           registry.py: actions, parameter checks, exact argv (from config/worlds.yaml)
    │                           paths.py: read-only access to data/ records
    ▼
 Session worker (worker.py, its own process)
    │  starts one ROS 2 launch on a private ROS domain (80–99) and Gazebo partition,
    │  watches readiness, runs operations (save map, send goal, typed command),
    │  and on STOP shuts down in order and audits that nothing it started survives
    ▼
 ROS 2 / Gazebo (src/amr_physical_ai/launch/tb3_mapping.launch.py)
    Gazebo world + TurtleBot3 Waffle, ros_gz bridges, robot_state_publisher,
    SLAM Toolbox (mapping) or AMCL + map server (navigation), Nav2 (MPPI/DWB/RPP),
    explorer node (exploration), ground-truth bridge (/amr/ground_truth), RViz
```

## Main parts

| Part | Where | Notes |
|---|---|---|
| Worlds | `config/worlds.yaml`, `src/amr_physical_ai/worlds`, `maps` | map (0, 0) = spawn; see [WORLDS.md](WORLDS.md) |
| Exploration | `amr_physical_ai/exploration.py` (frontier methods, pure Python), `exploration_run.py` (run loop: goals, time limit, stall rules, return home), `explore_node.py` (ROS node) | `exploration_sim.py` is a fast grid simulator for developing methods without Gazebo |
| Scoring | `scripts/exploration_metrics.py`, `score_exploration.py`, `office_world.py compare` | against the exact map of a generated world |
| Typed commands | `amr_physical_ai/command_pipeline.py` (rule-based parser, place validation, free-space check), `tools/amr_console/typed_commands.py` (session glue, ground-truth record) | grounding calibration from `tools/grounding_calibration` (conformal prediction over place names) |
| Delivery tasks | `tools/amr_task` | tags (rooms, seats, people), grounding and clarification, delivery state machine, spatial memory, terminal runner |
| Process safety | `src/amr_physical_ai/scripts/`: `process_ownership.py`, `transport_isolation.py`, `shutdown_audit.py`, `shutdown_records.py`, `gazebo_stop.py`, `rviz_windows.py`, `window_capture.py`, `nav2_shutdown.py` | which processes the console owns, private ROS domains and Gazebo partitions, ordered shutdown and its audit, window handling |

## Safety rules the code enforces

- The console only stops processes it started (ownership by process identity), and refuses to
  start while a foreign simulation is running on its transport.
- A command preview is bound to a fingerprint of the files it uses; if anything changes before
  you press Run, the run is refused and the preview is rebuilt.
- Typed commands move the robot only for an approved place on the running session's map, with
  ground truth recorded; questions and refusals never send a goal.
- Records are served read-only from `data/` with symlink-escape checks; nothing else is readable.
- The console never edits `approvals.yaml` or a places file.

## Data

See the table in the [README](../README.md#using-the-console). Nothing under `data/` is committed.
