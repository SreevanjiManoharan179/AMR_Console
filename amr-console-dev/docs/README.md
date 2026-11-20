# Documents

Start with the first three. The rest are for the people who run or change the software.

| Document | For | What it covers |
|---|---|---|
| [../README.md](../README.md) | everyone | what the console does, how to build and start it |
| [SCENARIOS.md](SCENARIOS.md) | clients, users | every office situation the robot handles, and what is left for later |
| [OFFICE_TASKS.md](OFFICE_TASKS.md) | users, owner | the Office tasks tab: asking, replying, switches, approvals |
| [SPEECH_LAB.md](SPEECH_LAB.md) | owner, testers | the Speech Lab test bench and the checking tools |
| [ARCHITECTURE.md](ARCHITECTURE.md) | engineers | processes, safety interlocks, data flow |
| [API.md](API.md) | engineers | the console's HTTP interface |
| [WORLDS.md](WORLDS.md) | engineers | adding a Gazebo world and its map |
| [SPEECH_INSTALL_REQUEST.md](SPEECH_INSTALL_REQUEST.md) | owner | the approved speech install: what, where from, hashes |
| `records/` | engineers | dated notes kept for the record, such as the 27 Oct page redesign; kept in the development repository, not in the public release |

## Where things live

| Folder | Holds | In git |
|---|---|---|
| `config/` | settings (`office_tasks.yaml`), the office's people and places, robot limits, test definitions (`office_request_cases.json`, `speech_test_sentences.tsv`) | yes |
| `docs/` | the documents above | yes |
| `scripts/` | build, checks and test tools | yes |
| `setup/` | start scripts, the app-menu launchers for AMR Console and AMR Speech Lab, and their icons (`setup/icons/`) | yes |
| `src/` | ROS 2 package (robot, worlds, maps, navigation) | yes |
| `tools/amr_console/` | the console (server, page, workers, tests) | yes |
| `tools/speech_lab/` | the Speech Lab test bench | yes |
| `build/`, `install/`, `log/` | made by the build; safe to delete and rebuild | no |
| `data/logs/` | run-time logs: console sessions, language and speech services | no |
| `data/runs/` | robot run records (drives, commands) | no |
| `data/evaluation/` | test results and recordings: `office_requests/`, `speech_lab/`, `mic_check/`, `speech_owner/`, `speech_fleurs/`, `speech_synthetic_en/` | no |

Nothing under `data/` is ever committed: logs and recordings stay on this computer.

## Names used

| Name | Means |
|---|---|
| Office request check | `scripts/office_request_check.py`: sends a fixed list of test requests to the real language model and compares each result with the expected one. (Called "office battery" until 10 Nov 2026; it has nothing to do with the robot's battery.) |
| Language service | the separate `amr-language` project, started by the console as its own process |
| Without the robot | the conversation runs and moves are only written down; nothing drives |
