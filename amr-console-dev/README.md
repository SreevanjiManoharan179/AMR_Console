# AMR Console

A browser console for building and testing an autonomous mobile robot (AMR) application in
simulation: map a building, explore it automatically, navigate, and carry out typed delivery
requests, with every run recorded and checked against simulator ground truth.

It is meant for teams who have (or plan to buy) an AMR and need the software around it:
a ready simulated office to try ideas in, exploration methods they can compare on a live map,
and a delivery-task layer that asks instead of guessing when a request is unclear.

> **Status:** simulation only, with the TurtleBot3 Waffle reference robot in Gazebo.
> The Commands tab is rule-based; the Office tasks tab uses a local language model (Qwen 3.5, 4B)
> that runs on this computer, offline. Real-robot connection is planned, not built.

## What it does

| Area | What you get |
|---|---|
| **Worlds** | `proposed_system`, an 18 × 12 m office (lobby, conference hall, demo area, cafeteria, open office, team office, manager cabin, storage) with an exact map, and the TurtleBot3 sandbox. Add your own in `config/worlds.yaml`. |
| **Mapping** | SLAM Toolbox with a drive pad in the browser; save the map from the console. |
| **Automatic exploration** | Three frontier-exploration methods on the live SLAM map: `ours` (largest unseen areas first, then a sweep of gaps and corners, then home), `gain` and `nearest` (baselines). Each run records the map over time, every goal and the simulator's true pose. |
| **Scoring** | `score_exploration.py` scores runs against the exact map: coverage over time, T90/T99, walls, corners, floor IoU, SLAM pose error, collisions, recoveries, return to the dock. |
| **Navigation** | Nav2 with MPPI, DWB or RPP on a saved map; send goals from the browser. |
| **Typed commands** | In the console, type "go to the office" or "go to the cafeteria". A calibrated scorer decides when to act, when to ask ("Which one: the open office or the team office?") and when to refuse an unknown place. Nothing moves on a question. |
| **Delivery tasks** | The task runner (`python3 -m tools.amr_task.run_live`, attached to a console navigation session) handles "take this to Priya" or "bring it to the manager": places, seats and people come from a tag file; it confirms before moving, waits for "taken" or "not here", and returns to the dock. |
| **Office tasks** | The Office tasks tab: an office assistant that takes requests in plain English, typed or spoken. It delivers and collects small items (up to four in one trip), passes on messages, asks a question and brings back the answer, calls a person, and goes to people or places. Requests can be chained with "then", "if yes / if no" and "if she is not there". It says what it understood and waits for a yes, looks for a person who does not answer, asks the last person whether they need anything else, and says politely what it cannot do. Works with the robot in Gazebo or as a conversation without it. See [docs/SCENARIOS.md](docs/SCENARIOS.md) and [docs/OFFICE_TASKS.md](docs/OFFICE_TASKS.md). |
| **Spatial memory** | Remembers delivery outcomes per person or place ("Last time (10 min ago) Priya: not here. Try again?"). |
| **Records** | Every session writes its command, logs, cleanup audit and results under `data/`. The console's Results tab opens them. |
| **Views** | RViz and Gazebo open with the same 1 m grid laid over the world's map. |

### Exploration benchmark (in simulation)

`proposed_system`, 5 runs per method, 30 min budget, SLAM Toolbox defaults. Median [min–max].

| | ours | gain | nearest |
|---|---|---|---|
| Floor mapped at 10 min | 81 % [77–85] | 82 % [81–86] | 57 % [45–67] |
| Final completion | 98.9 % [95.5–99.3] | 99.3 % [95.1–99.3] | 82.6 % [44.8–95.2] |
| Floor IoU vs. exact map | 94.2 % [90.8–95.4] | 94.6 % [88.9–95.5] | 79.5 % [42.7–90.7] |
| Worst SLAM pose error in a run | 0.13 m | 1.47 m | 2.75 m |
| Returned to the dock | 5 of 5 | 4 of 5 | 3 of 5 |
| Collisions | 0 | 0 | 0 |

`ours` and `gain` map about the same amount; `ours` was the most consistent (no run with SLAM
drift, every run back at the dock). The simulated LiDAR reaches 20 m; a real TurtleBot3
LDS reaches about 3.5 m, so expect lower numbers on hardware.

## Requirements

- Ubuntu 24.04 with a desktop (X11 recommended)
- ROS 2 Jazzy and Gazebo Harmonic (`gz sim` 8)
- ROS packages: Nav2 (`nav2_bringup`, `nav2_mppi_controller`), `slam_toolbox`, `nav2_minimal_tb3_sim`,
  `ros_gz_sim`, `ros_gz_bridge`, `rmw_cyclonedds_cpp`, `rviz2`, `xacro`
- Python 3.12 with PyYAML, NumPy, SciPy, Pillow (Ubuntu packages); pytest for the offline checks

`setup/check_system.sh` checks all of this and prints the exact `apt` command for anything
missing. It never installs anything itself.

## Quick start

```bash
git clone https://github.com/SreevanjiManoharan179/AMR_Console.git   # console and language module
cd AMR_Console/amr-console-dev
bash setup/check_system.sh        # what is installed, what is missing
bash setup/build.sh               # colcon build of the robot package
bash scripts/check.sh             # offline tests (no simulator)
bash setup/run_console.sh         # starts the console and opens it in your browser
bash setup/install_launcher.sh    # optional: "AMR Console" and "AMR Speech Lab" in the app menu
```

The Office tasks tab also needs the language module beside this folder (`../amr-language`, in the
same repository) with its local model runner, and speaking needs the speech install; see
[docs/OFFICE_TASKS.md](docs/OFFICE_TASKS.md).

Use one checkout per terminal: don't source another workspace that also contains the
`amr_physical_ai` package.

Only one console runs at a time: it drives the simulator and serves the page on 127.0.0.1:8765;
a second one says the port is in use and stops. The **AMR Speech Lab** (`bash
setup/run_speech_lab.sh`, or its app-menu entry) is a separate test bench for speech and language;
close the console before opening it, because both start the same language model.

## Using the console

1. **Mapping → Start mapping**, choose a world. Drive with the pad; **Save the map** when done.
2. **Mapping → Explore automatically** in `proposed_system`, choose a method. The robot explores
   until nothing reachable is left (or 30 min), then returns to the dock.
3. **Navigation → Start navigation** on a saved map, then **Send a goal**.
4. **Commands → Send a typed command** during a navigation session in `proposed_system`.
   Delivery commands need two approvals by the owner of the deployment: `approval_status:
   confirmed` in the places file and `proposed_system_places: true` in
   `tools/amr_console/approvals.yaml`. The console never edits either file.
5. **Office tasks → Start language service**, then type or speak a request in English. Without
   the robot it is a conversation only; with the robot it needs a navigation session on the office
   map and the owner approvals listed in [docs/OFFICE_TASKS.md](docs/OFFICE_TASKS.md). What the
   robot does in each situation: [docs/SCENARIOS.md](docs/SCENARIOS.md).
6. **STOP** ends whatever the console started, in order (robot, explorer, SLAM, RViz, Gazebo),
   and writes a cleanup audit. It never touches processes it did not start.

Run output goes to `data/` (not committed):

| Folder | Contents |
|---|---|
| `data/runs/<run_id>/` | maps you saved, exploration records, command records |
| `data/logs/console/<run_id>/` | the exact command, launch log, cleanup audit |
| `data/logs/task/` | delivery-task sessions |
| `data/logs/office/<stamp>/` | office tasks: conversation timeline, worker logs, speech transcripts |
| `data/runs/<run_id>/office/` | one record per office drive (goal, map check, true pose before and after) |
| `data/memory/` | spatial memory, one file per world |
| `data/benchmarks/` | exploration benchmarks |
| `data/evaluation/` | test results and recordings: office request check, Speech Lab, microphone and speech checks |

## Benchmarking exploration

```bash
python3 scripts/explore_batch.py --runs 5 --label my_benchmark --no-rviz
# then run the scoring command it prints
```

## Adding a world

See [docs/WORLDS.md](docs/WORLDS.md): a Gazebo world, its saved map, and one entry in
`config/worlds.yaml`. The grids, the console's world lists and the exploration scoring follow.

## Repository layout

```
config/                 worlds.yaml, tags (places/seats/people), calibration data
setup/                  check_system.sh, build.sh, run_console.sh, run_speech_lab.sh, install_launcher.sh
                        icons/: the two app icons (SVG, and PNG at 256 and 512 px for slides and documents)
scripts/                build.sh, check.sh, exploration benchmark runners, office request check, speech checks
src/amr_physical_ai/    ROS 2 package: launch, worlds, maps, Nav2/SLAM settings, RViz,
                        exploration, delivery-command pipeline, scoring tools, tests
tools/amr_console/      console backend (HTTP API, session worker, process supervision) + web page;
                        office tasks: language worker, speech worker, approved map context
tools/amr_task/         delivery tasks: tag grounding, clarification, runner, spatial memory
tools/grounding_calibration/
                        calibration that decides act / ask / refuse for typed commands (conformal prediction)
tools/speech_lab/       the Speech Lab: speech and language test bench, separate from the console
docs/                   start at docs/README.md: scenarios, office tasks, architecture, API
```

## Licence

Proprietary. See [LICENSE](LICENSE).
