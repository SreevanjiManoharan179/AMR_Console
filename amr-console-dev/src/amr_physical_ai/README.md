# amr_physical_ai (ROS 2 package)

The robot side of AMR Console: launch files, worlds, maps, Nav2 and SLAM settings, RViz views,
the exploration node and methods, the typed-command pipeline and the scoring tools.
The console starts everything through `launch/tb3_mapping.launch.py`; you rarely need it directly.

```bash
# Mapping in the proposed_system office (from the repository root, after setup/build.sh)
ros2 launch amr_physical_ai tb3_mapping.launch.py world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro

# Automatic exploration with the "ours" method, ground truth recorded
ros2 launch amr_physical_ai tb3_mapping.launch.py world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro \
    explore:=ours explore_out:=data/runs/manual/exploration ground_truth:=true

# Navigation on a saved map
ros2 launch amr_physical_ai tb3_navigation.launch.py map:=src/amr_physical_ai/maps/proposed_system.yaml \
    world:=src/amr_physical_ai/worlds/proposed_system.sdf.xacro
```

Useful launch arguments: `headless:=true` (no Gazebo window), `use_rviz:=false`,
`slam_params_file:=<file>` (SLAM Toolbox settings; empty = installed defaults),
`params_file:=config/nav2_{mppi,dwb,rpp}.yaml`, `gazebo_view:=false` (keep Gazebo's own grid and camera).

| Folder | Contents |
|---|---|
| `amr_physical_ai/` | `exploration*.py`, `explore_node.py`, `command_pipeline.py`, `teleop_wasd.py` |
| `config/` | Nav2 settings per controller, named places per world |
| `worlds/`, `maps/` | Gazebo worlds (with layouts) and saved maps |
| `scripts/` | `office_world.py` (generate/check/compare worlds), `score_exploration.py`, `exploration_metrics.py`, process-safety helpers used by the console |
| `test/` | offline tests (`bash scripts/check.sh` from the repository root) |
