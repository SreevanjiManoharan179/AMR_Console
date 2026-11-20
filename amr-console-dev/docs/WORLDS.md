# Worlds

A world is a Gazebo world plus its saved map. Everything the console offers per world comes
from one file, `config/worlds.yaml`.

## Coordinates

The map frame's (0, 0) is the robot's spawn point (the dock). So for every world:

```
world (Gazebo) = map + spawn (x, y)
```

Both shipped worlds spawn the robot at world (−2.0, −0.5). The console uses this rule for the
RViz view, the RViz and Gazebo grids (both laid over the saved map, 1 m lines on whole map
metres), and for converting the simulator's true pose into map coordinates.

## Shipped worlds

| id | World | Map | Exploration scoring | Typed commands |
|---|---|---|---|---|
| `proposed_system` | `src/amr_physical_ai/worlds/proposed_system.sdf.xacro`, generated from `proposed_system_layout.yaml` | exact (generated from the same layout) | yes | yes |
| `tb3_sandbox` | TurtleBot3 reference world from `nav2_minimal_tb3_sim` | built with SLAM | no | no |

## Adding a world

1. **World file.** Put the Gazebo world (`.sdf` or `.sdf.xacro`) in `src/amr_physical_ai/worlds/`.
   Its world must be named `default` (the ground-truth reader expects it). For an office made
   of walls, rooms and furniture you can instead write a layout YAML and generate both the world
   and an exact map:

   ```bash
   python3 src/amr_physical_ai/scripts/office_world.py generate --layout src/amr_physical_ai/worlds/<id>_layout.yaml
   ```

2. **Map.** Save `src/amr_physical_ai/maps/<id>.yaml` and `<id>.pgm` (same id as the world
   file's stem). For a world without a layout, map it once in the console (Mapping → Start
   mapping, then Save the map) and copy the saved pair here.

3. **Entry in `config/worlds.yaml`:**

   ```yaml
   my_warehouse:
     label: My warehouse
     world: src/amr_physical_ai/worlds/my_warehouse.sdf.xacro
     map: src/amr_physical_ai/maps/my_warehouse.yaml
     spawn: [-2.0, -0.5, 0.0]
     view_scale: 30          # RViz pixels per metre; smaller for bigger maps
     explore: false          # true only when the map is exact (generated), for scoring
   ```

4. **Typed commands (optional).** Add a places file
   (`src/amr_physical_ai/config/named_locations_<id>.yaml`, see the shipped one), run the
   grounding calibration for it, and add `places`, `grounding` and `approval` to the entry:

   ```bash
   cd tools/grounding_calibration
   python3 -m grounding_calibration.run_pilot --registry ../../src/amr_physical_ai/config/named_locations_<id>.yaml \
       --out ../../config/calibration/<id>/grounding --label "<id> calibration"
   ```

   The owner then confirms the places (`approval_status: confirmed` in the places file) and
   enables them (`<id>_places: true` in `tools/amr_console/approvals.yaml`).

5. **Build and check.** `bash setup/build.sh` (the package installs the maps), then
   `bash scripts/check.sh`, then start mapping in the new world from the console.
