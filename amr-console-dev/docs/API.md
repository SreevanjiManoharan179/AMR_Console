# AMR Console HTTP API

The web page (`tools/amr_console/web/index.html`) is one self-contained file. It never decides
what runs: it renders the backend's action list, shows the exact command the backend will run,
and calls the endpoints below. Every safety rule is enforced in the backend.

## Running and authentication

```bash
bash setup/run_console.sh                      # or: python3 tools/amr_console/server.py
```

The server binds to `127.0.0.1:8765` only and prints one line, `AMR Console: http://127.0.0.1:8765/?token=<32 hex>`,
with a new random token on every start (`setup/run_console.sh` passes it to the browser without
printing it). `GET /` serves the page. Every `/api/*` call needs header `X-Console-Token: <token>`
(the event stream takes it as `?token=`). A wrong or missing token → `403 {"error": "bad_token", "reason": ...}`.

A client that stops sending in the middle of a request is disconnected after 20 s.

Errors are `{"error": "<code>", "reason": "<sentence>"}` with 4xx/5xx status: 409 for interlocks and
stale previews, 422 for invalid parameters, 404 for unknown actions or paths.

## Endpoints

| Method and path | Request | Response |
|---|---|---|
| `GET /api/status` | | `state` (`idle`, `starting`, `running`, `stopping`, `error`), current `session` (action, run id, kind, ROS domain, Gazebo partition, teleop, `operation_running`: true while a map save, goal or typed request is in progress; a `state` event is sent whenever it changes), `environment` (ROS distro, middleware, Gazebo version, commit, window arranging and recording support) |
| `GET /api/actions` | | the action registry (below), with lock reasons; re-reads `approvals.yaml` every time |
| `POST /api/preview` | `{action_id, params}` | `{kind, command` or `operation, env, spec_id, blocked_reason}`; `command` is exactly what `/api/run` executes |
| `POST /api/run` | `{action_id, params, spec_id, confirm_text}` | `{run_id}`; 409 `preview_stale` if anything changed since the preview (files, transport, state) or the spec is older than 10 min |
| `POST /api/stop` | | `{survivors, forced, bond_abort, report}` after the ordered shutdown and audit |
| `GET /api/events` | `?token=` | server-sent events, one JSON object per `data:` line: `log` (`level`, `text`), `state` (the status object), `alarm`; keep-alive every 15 s |
| `POST /api/continue` | | reserved for paused batch runs; 409 when nothing is waiting |
| `POST /api/teleop` | `{linear_x, angular_z}` | only in a running mapping session; clamped to 0.26 m/s and 1.82 rad/s; 300 ms deadman publishes zero |
| `POST /api/windows/arrange` | `{layout: "gazebo-left-rviz-right"}` | `{supported, reason}`; needs X11 and `wmctrl` or `xdotool` |
| `GET /api/results` | | run folders under `data/runs/`, `data/benchmarks/` and `data/logs/console/`, newest first, with their text files |
| `GET /api/file?path=` | repo-relative path | `{path, text}`; only `data/runs/`, `data/benchmarks/`, `data/logs/console/`; `.md .csv .json .jsonl .yaml .txt .log`; at most 1 MB; no `..` or symlink escapes |
| `POST /api/open` | `{path}` | opens a file or folder from the same roots with `xdg-open` (outside the session's process group) |
| `GET /api/office` | | office tasks snapshot: `service` (`off`, `starting`, `ready`, `stopping`, `error`, model, runner), `mode` (`robot`, `rehearsal`), `context` (map, `places` [{key, label}], `requesters` [{id, name, kind, language, place}]), `blocks` ({robot: [reasons], rehearsal: [reasons]}), last 12 `jobs`, `awaiting` (the open question: `input_id`, `job_id`, `expect`, `audience`, `place_label`, `wait_s`), `position_label`, `moving`, last 60 `timeline` entries, `busy`, `speech` (`not_installed`, `disabled`, `off`, `starting`, `ready`, `error`, per-language state); also sent as an `office` event on every change |
| `POST /api/office/service` | `{action: "start"\|"stop", mode}` | the snapshot; `robot` mode is refused (409 `office_approval`) until every owner approval is in place |
| `POST /api/office/request` | `{text, requester, language, place}` | `{job_id}`; text 1–1000 characters without control characters; `language` `auto`, `en`, `ta`, `ml`, `te`; refused with the `blocks` reasons |
| `POST /api/office/reply` | `{input_id, text}` | `{ok}`; only the currently open question (409 `office_reply_stale` otherwise) |
| `POST /api/office/cancel` | | `{ok}`; cancels the current task and the queue, stops an office drive; the session keeps running |
| `POST /api/office/speech?language=&hint=` | body: 16 kHz mono 16-bit WAV, `Content-Type: audio/wav`, at most 1 MB and 15 s | `{text, language, model, seconds, latency_ms, peak_db, rms_db, silent, clipped, offset, distorted}`; the words only, never submitted; `silent` (loudest sound under −50 dB, as from a muted microphone) skips the model; `distorted` (over 1 % clipped or a centre offset over 0.05, as from an empty jack at full boost) keeps the words but the page warns they may be wrong; `language` `auto` uses `hint` (the speaker's own language) then the operator's; 415 wrong type, 413 too large, 422 not 16 kHz mono 16-bit or too short/long, 409 when speech is not installed or still loading |

## Actions

| id | Tab | Runs as | Parameters |
|---|---|---|---|
| `env.build` | environment | job: `bash scripts/build.sh` | |
| `env.check` | environment | job: `bash scripts/check.sh` | |
| `map.start` | map | mapping session (drive pad on) | `world` |
| `map.explore` | map | mapping session (drive pad off) | `world` (worlds with `explore: true`), `method` (`ours`, `gain`, `nearest`) |
| `map.save` | map | operation in a mapping session | `name` (`[a-z0-9_]{3,40}`); saves `data/runs/<run_id>/maps/<name>/<name>.{yaml,pgm}`, refuses to overwrite |
| `nav.start` | navigate | navigation session | `map`, `controller` (`mppi`, `dwb`, `rpp`) |
| `nav.goal` | navigate | operation in a navigation session | `x`, `y`, `yaw`; must lie inside the loaded map |
| `commands.typed` | commands | operation in a navigation session | `text` (1–300 characters, not blank, no control characters); locked until a world's places are approved (below) and the session records ground truth |

Select parameters carry `options` (the ids the API accepts), `locked_options` ({id: reason}) and, for worlds and maps, `option_labels` ({id: display name}, for example `proposed_system` → "Demo office (18 × 12 m)"). Show the label, send the id.

## Interlocks

1. A session or job starts only when the console is `idle`; an operation runs only inside a running
   session of its kind, keeps its run id, and only one at a time.
2. `env.build` and `env.check` are refused while a session runs, while `log/simulation.lock` is
   held, or while any simulation process is running on the machine.
3. Before every session, a full process preflight: leftover Gazebo, bridge, Nav2, AMCL, map server,
   RViz or robot_state_publisher processes → 409 with the list. After every session, a survivor
   audit; survivors → `state: "error"` and an alarm, and nothing new starts until STOP is clean.
4. STOP signals only processes the console started (and their tracked descendants): SIGINT to the
   group, 10 s, SIGTERM, 5 s, SIGKILL; any forced step sets `forced: true` and an alarm.
5. Each session gets its own ROS domain (80–99, skipping busy ones) and `GZ_PARTITION=amr_console_<kind>_<domain>`.

## Approvals

Typed commands for a world need two edits by the deployment owner; the console never makes either:

```yaml
# tools/amr_console/approvals.yaml — one key per world with places (config/worlds.yaml)
proposed_system_places: true
```

and `approval_status: confirmed` in that world's places file. Unknown or repeated keys, or values
other than `true`/`false`, make the file invalid and every typed command stays locked.
