# Shared-load check — frozen v0.4.3

Generated 2026-11-05T06:41:13.313319+05:30. Raw archive: `records/shared_load_2026-11-03_p2_ready`.

[checked] Measurement script only. Language behavior remains v0.4.3. Both console folders are read-only; no console/ROS/Gazebo session was started, stopped or signalled by this script. Only owned B/C workers are managed. No installs or task dispatch.

## Owner operation

This archive contains the November 3 campaign. Use a fresh output folder for a new campaign. Use `--report records/shared_load_2026-11-03_p2_ready` to regenerate the automated tables from the saved measurements.

Confirmation prompts accept only an empty Enter; navigation text stays in Firefox’s AMR Console Commands > Request field. To retry an interrupted pre-B P1 while preserving P0, use `--continue-p1 <old-archive> --output <new-archive>`; raw P0 files are copied with hashes, the old archive stays intact, and no old P1 samples count. If B startup failed before a runner was created after a successful P1, `--continue-p2 <old-archive> --output <new-archive>` preserves both P0 and P1. P0: open the dev console without a session. P1: start proposed_system with ground truth on, enter the run ID, send cafeteria then home. P2: B warms outside timing; repeat both goals while 20 requests run, pressing Enter when each leg starts (ten requests per leg). P3: click console STOP yourself about one second into the long Tamil request, then confirm cleanup. P4 is optional: start a new session, repeat both goals with B plus isolated CPU C. Type observed driving RTF if automatic read-only stats are unavailable. The script prints detailed prompts.

The 20 fixed starters are 01/02/03/06/10 for en/ta/ml/te, including short and long requests. Names and numeric capability inputs are frozen SCRIPTED fixtures, solely for full-planner load; they do not authorize physical payloads. No output plan is dispatched. P4 uses the frozen cross-system mode (one C translation plus B greedy), so its draw policy differs from P2 adaptive B. C, when selected, runs only in `.runtime/indictrans2-C/venv/bin/python -I`, on CPU.

## Phases

| Phase | Status | Shared language trials | p50 / p95 ms (shared) | Peak GPU MiB | Peak host used RAM KiB | RTF p50 / source |
|---|---|---:|---|---:|---:|---|
| P0 | measured | 0/20 | not measured / not measured | 15.0 | 7072224 | not measured / not_measured |
| P1 | measured | 0/20 | not measured / not measured | 15.0 | 10740380 | not measured / not_measured |
| P2 | measured | 13/20 | 1247.1719 / 6053.7306 | 3787.0 | 11873180 | 1.0 / owner_typed_during_navigation |
| P3 | not measured | 0/20 | not measured / not measured | 3787.0 | 11572020 | not measured / not_measured |
| P4 | not measured | 0/20 | not measured / not measured | not measured | not measured | not measured / not_measured |

[checked] Unrun phases are **not measured**, never zero-performance results. Compute-query and pmon graphics memory are separate observations; do not add their overlapping PID allocations. Sampled peaks may miss short spikes. Target GPU/RAM/CPU cadence is 200 ms; actual gaps/overruns, API errors and CPU metrics are in summary.json. Graphics process/RTF clients are slower, separately timestamped. Navigation overlap uses enclosing ground-truth samples, an approximation rather than exact Nav2 event timing.

## Proposed pass lines

| Item | Measured result / pass |
|---|---|
| B_fully_GPU | True |
| gpu_below_7_5_GB | True |
| rtf_drop_at_most_10_percent | not measured |
| navigation_time | True |
| final_error_change | True |
| STOP | not measured |

P2 allocation: 1340 /api/ps samples, 0 missing B entries, 0 API errors. Full GPU requires positive size == size_vram in every sample. Peak threshold: GPU MiB; strict 7.5 decimal GB = 7152.56 MiB used for the proposed line; 7.5 GiB = 7680 MiB result is also retained in summary.json.

P2 RTF / P1 RTF = not measured. Same recorded console settings/commit: True. P2 has one screenshot-backed RTF reading of 100.00% while driving with B loaded, before the first batch was released. P1 has no RTF reading for comparison. P2 used a new session with the same console commit, map, controller and ground-truth setting.

P2 requests: 20 attempted of 20, 0 not attempted, 20 measured, 0 unmeasured, 7 idle/partially-overlapping trials excluded from shared results. Within 5 s: 11/13; within 20 s: 13/13. Zero denominators mean not measured. All-trial counts/latency are preserved separately in summary.json.

Task 4b standalone matched-ID p50/p95: 1162.9769 / 11405.0624 ms; P2 ratios: 1.0724 / 0.5308. Source `records/task4b/B_t07_complete_2026-11-03/requests.jsonl`, hash `a8df1befc74da47549d1f276a749682fc36bb092879ee408c3037023e4d8fe33`. This is a historical comparison across planner versions (task 4b versus v0.4.3), not a controlled causal estimate.

| Goal | P1/P2 seconds | Time ratio | P1/P2 final error m | Change m | Success / comparable start & map |
|---|---|---|---|---|---|
| cafeteria | 58.778 / 58.9756 | 1.0034 | 0.1072 / 0.1127 | 0.0055 | True / True, True |
| home | 62.6106 / 62.3754 | 0.9962 | 0.1094 / 0.0628 | -0.0466 | True / True, True |

## STOP evidence

The owner's STOP ended console run `20261103_191414_nav_start_12d72b06` cleanly. Both console audits report zero survivors, no forced cleanup and no bond abort. The run exited with code 0, and the recheck reports the simulation lock released. The audit records and hashes are in `P3/console_cleanup_review.json`.

The interactive measurement session ended before persisting `P3/phase.json`. The available record does not establish B was still generating when STOP was clicked or that the next planner request worked. P3's strict result remains **not measured**. The long resource log includes time spent waiting at the prompt and is not a timed STOP trace.

## GPU process memory

| Phase | PID | Process | GPU peak MiB | RAM peak KiB | CPU peak (one core = 100%) | Sources |
|---|---:|---|---:|---:|---:|---|
| P0 | 3090 | Xorg | 4 | 209080 | 4.9978 | pmon_compute_and_graphics |
| P1 | 15353 | map_server | not measured | 31704 | 14.9935 |  |
| P1 | 15356 | amcl | not measured | 32196 | 19.9913 |  |
| P1 | 15358 | controller_serv | not measured | 48940 | 54.9601 |  |
| P1 | 15360 | planner_server | not measured | 38272 | 24.9878 |  |
| P1 | 15363 | bt_navigator | not measured | 50588 | 29.9832 |  |
| P1 | 15369 | rviz2 | not measured | 196640 | 29.9911 |  |
| P1 | 26993 | gz-transport-to | not measured | 13228 | 0 |  |
| P1 | 3090 | Xorg | 4 | 215920 | 44.9771 | pmon_compute_and_graphics |
| P2 | 64303 | map_server | not measured | 30764 | 14.9944 |  |
| P2 | 64318 | amcl | not measured | 31160 | 19.9911 |  |
| P2 | 64320 | controller_serv | not measured | 46200 | 49.9589 |  |
| P2 | 64322 | planner_server | not measured | 36500 | 24.9803 |  |
| P2 | 64327 | bt_navigator | not measured | 48888 | 24.9882 |  |
| P2 | 64333 | rviz2 | not measured | 194200 | 29.9811 |  |
| P2 | 65212 | python3 | not measured | 12352 | 0 |  |
| P2 | 65215 | ollama | not measured | 37068 | 14.994 |  |
| P2 | 65441 | llama-server | 3764.0 | 1588228 | 709.4856 | compute_query, pmon_compute_and_graphics |
| P2 | 65624 | gz-transport-to | not measured | 13116 | 4.9972 |  |
| P2 | 3090 | Xorg | 4 | 248456 | 29.9902 | pmon_compute_and_graphics |
| P3 | 3090 | Xorg | 4 | 242748 | 79.9661 | pmon_compute_and_graphics |
| P3 | 64303 | map_server | not measured | 32332 | 34.9794 |  |
| P3 | 64318 | amcl | not measured | 31688 | 34.9794 |  |
| P3 | 64320 | controller_serv | not measured | 45532 | 44.9735 |  |
| P3 | 64322 | planner_server | not measured | 37620 | 29.985 |  |
| P3 | 64327 | bt_navigator | not measured | 49316 | 39.9764 |  |
| P3 | 64333 | rviz2 | not measured | 195096 | 24.986 |  |
| P3 | 65215 | ollama | not measured | 37572 | 9.991 |  |
| P3 | 65441 | llama-server | 3764.0 | 1586904 | 659.6128 | compute_query, pmon_compute_and_graphics |
| P3 | 76398 | gz-transport-se | not measured | 13032 | not measured |  |
| P3 | 76482 | gz-transport-se | not measured | 13104 | not measured |  |
| P3 | 122530 | llama-server | not measured | 52008 | not measured |  |
| P3 | 122544 | llama-server | not measured | 175560 | not measured |  |
| P3 | 122562 | ollama | 18.0 | 358940 | 69.9401 | compute_query |

## Recommendation

Use B for a supervised simulation pilot, with proposal generation while the robot is idle. In this run, B stayed on the GPU and both navigation goals met the time and final-error lines. The 13 fully overlapping requests give a useful latency sample, with 11 within 5 seconds and all 13 within 20 seconds. The P1 RTF baseline and timed STOP recovery remain unmeasured. Keep normal console navigation and STOP on their existing paths during the pilot.

The language module already accepts text and produces a grounded proposal. This campaign exercised console navigation through its existing typed rule parser while B independently processed test requests. A live console adapter and microphone input are still pending. The next simulation milestone is a confirmed typed navigation proposal through the adapter, followed by local push-to-talk transcription into the same path. See the handoff for the pilot boundaries and acceptance checks.
