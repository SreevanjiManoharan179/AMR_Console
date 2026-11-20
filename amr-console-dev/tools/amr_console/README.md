# AMR Console backend

Start it with `bash setup/run_console.sh` from the repository root. The HTTP API, actions,
interlocks and approvals are described in [docs/API.md](../../docs/API.md); the overall design
in [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).

| File | Role |
|---|---|
| `server.py` | HTTP server on 127.0.0.1, token check, event stream |
| `engine.py` | console state, interlocks, prepared commands, run records, RViz view and grid |
| `registry.py` | the action list, parameter validation and exact command lines, from `config/worlds.yaml` |
| `worker.py`, `client.py`, `supervision.py` | one worker process per session: launch, readiness, operations, ordered shutdown, survivor audit |
| `ros_adapter.py` | rclpy side of a session: teleop, goals, map saving |
| `typed_commands.py` | typed commands: grounding, questions, approved goals, ground-truth record |
| `paths.py` | read-only access to records under `data/` |
| `windows.py`, `gazebo_shutdown.py`, `runtime.py`, `events.py`, `errors.py` | window handling, Gazebo stop, environment and transport, events, errors |
| `approvals.yaml` | the deployment owner's approvals; the console never edits it |
| `web/index.html` | the page |
| `tests/` | offline tests: `python3 run_tests.py` |
