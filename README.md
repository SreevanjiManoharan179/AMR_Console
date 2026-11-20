# AMR Console

An office assistant for an autonomous mobile robot, in simulation: people ask in plain English,
the robot says what it understood, waits for a yes, and carries small items and messages between
them. The same browser console maps a building, explores it automatically, navigates with Nav2
and records every run against the simulator's ground truth.

> **Status:** case study completed in November 2026. Simulation only, with the TurtleBot3 Waffle
> reference robot in Gazebo Harmonic on ROS 2 Jazzy. English in the console; Tamil, Malayalam,
> Telugu and Tanglish only in the Speech Lab test bench. A real-robot connection is planned, not
> built.

## Two projects, one product

| Folder | What it is | Start here |
|---|---|---|
| [`amr-console-dev/`](amr-console-dev/) | **The product.** The browser console (mapping, exploration, navigation, Office tasks), the ROS 2 package, the delivery-task layer and the Speech Lab test bench | [README](amr-console-dev/README.md), then [SCENARIOS](amr-console-dev/docs/SCENARIOS.md) |
| [`amr-language/`](amr-language/) | The language module the console starts as its own process: understands requests, checks them against the office's people and places, holds the conversation | [README](amr-language/README.md) |
| `amr-speech/` (made on your computer) | Speech-to-text: its Python environment and downloaded models. No source code | [speech install](amr-console-dev/docs/SPEECH_INSTALL_REQUEST.md) |

The console looks for the other two next to itself (`amr-console-dev/config/office_tasks.yaml`:
`../amr-language`, `../amr-speech`), so keep the three folders side by side, as in this
repository.

## What has been measured

All in simulation. The full tables, with their limits, are in the project READMEs.

| Check | Result | Where |
|---|---|---|
| Automatic exploration of the 18 × 12 m office, 5 runs per method | method `ours`: 98.9 % of the floor mapped (median), back at the dock 5 of 5, no collisions | [console README](amr-console-dev/README.md#exploration-benchmark-in-simulation) |
| Office request check, English, with the real language model | 30 of 30 fixed test requests understood as expected (10 Nov 2026, typed, no robot) | [language README](amr-language/README.md#how-it-has-been-checked) |
| Offline test suites (no simulator, stand-in model) | console: 701 tests pass; language module: 476 pass, 8 skipped here (below) | [CI](.github/workflows/ci.yml) |

The measurement records behind these results (run logs, maps over time, model outputs) and the
dated development reports are not in this repository; the author can share them on request.
Eight language-module tests check those records, so here they are skipped. Everything that
drives the robot is checked live in the simulator, not by these suites.

## Quick start

Ubuntu 24.04 with ROS 2 Jazzy and Gazebo Harmonic; [docs/INSTALL.md](docs/INSTALL.md) has the
full list and every optional part. Building and running the software needs the copyright
holder's permission (see [Licence](#licence)).

```bash
git clone https://github.com/SreevanjiManoharan179/AMR_Console.git
cd AMR_Console/amr-console-dev
bash setup/check_system.sh        # what is installed, what is missing (installs nothing)
bash setup/build.sh               # colcon build of the robot package
bash scripts/check.sh             # offline tests (no simulator)
bash setup/run_console.sh         # starts the console and opens it in your browser
```

Mapping, exploration and navigation need nothing more. The Office tasks tab also needs the local
model runner and model, and speaking instead of typing needs the speech install
([docs/INSTALL.md](docs/INSTALL.md)). The owner's approvals of the simulated office's named
places are included (`amr-console-dev/tools/amr_console/approvals.yaml`); a new world starts
unapproved, and the console never edits approvals.

## Documents

| Document | What it covers |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | everything to install, in order, and how to check each part |
| [amr-console-dev/docs/README.md](amr-console-dev/docs/README.md) | the console's documents: scenarios, office tasks, architecture, API, worlds, Speech Lab |
| [amr-language/docs/README.md](amr-language/docs/README.md) | the language module's documents: the contract with the console, design, rehearsal rules |
| [CHANGELOG.md](CHANGELOG.md) | what changed in this repository |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how changes are checked; how this repository is updated from development |
| [SECURITY.md](SECURITY.md) | how to report a security problem |

## Licence

Copyright (c) 2026 Bharathwaj Manoharan. All rights reserved: the source is published so that it
can be read and reviewed. No licence to use, copy, modify or distribute it is granted without the
copyright holder's prior written permission; see [LICENSE](LICENSE). Third-party software used at
run time, and the few files derived from it, are listed in [NOTICE](NOTICE) and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). No model weights are included.

To cite this work, see [CITATION.cff](CITATION.cff).
