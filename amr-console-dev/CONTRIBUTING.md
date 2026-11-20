# Contributing

1. Keep ROS code and assets in `src/amr_physical_ai`, console code in `tools/amr_console`,
   task code in `tools/amr_task`, configuration in `config/`, setup in `setup/`.
2. Before every commit: `bash scripts/check.sh` (offline, about a minute). For launch,
   world or Nav2/SLAM changes also run `bash setup/build.sh` and one console session.
3. A new world is a new entry in `config/worlds.yaml`; see [docs/WORLDS.md](docs/WORLDS.md).
   Regenerated worlds and maps must match their layout (`office_world.py check`).
4. A change to a places or tag file invalidates its calibration on purpose: rerun the
   calibration and commit the new files with the change.
5. Run output stays in `data/` and is never committed. Benchmarks that are reported somewhere
   are copied out with their `manifest.json` and the commit they ran on.
6. State what was tested and what was not in each pull request. Results are always
   "in simulation" unless they were measured on a robot.
7. No credentials, tokens or machine-specific paths in the repository.
