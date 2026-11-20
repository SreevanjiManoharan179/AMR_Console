# Installing

Three parts, each optional after the first. The scripts named here print what is missing and
never install anything themselves. Building and running the software needs the copyright
holder's permission; see [LICENSE](../LICENSE).

| Part | Needed for | Installed into |
|---|---|---|
| 1. Robot software | mapping, exploration, navigation, typed commands, delivery tasks | the system (ROS 2 packages) and `amr-console-dev/build/` |
| 2. Language model | the Office tasks tab and the Speech Lab | `amr-language/.runtime/` (reserve about 10 GB) |
| 3. Speech-to-text | speaking instead of typing | `amr-speech/` (English 0.75 GB; with Tamil, Malayalam and Telugu about 9 GB in all; reserve 12 GB) |

Keep `amr-console-dev/`, `amr-language/` and `amr-speech/` side by side, as in this repository.

## 1. Robot software

You need Ubuntu 24.04 with a desktop (X11 recommended), [ROS 2 Jazzy](https://docs.ros.org/en/jazzy/Installation.html)
and Gazebo Harmonic (`gz sim` 8, installed with the ROS packages below), and Python 3.12.

```bash
git clone https://github.com/SreevanjiManoharan179/AMR_Console.git
cd AMR_Console/amr-console-dev
bash setup/check_system.sh     # one line per requirement; prints the apt command for anything missing
```

Install what it lists (ROS packages such as `nav2_bringup`, `nav2_mppi_controller`,
`slam_toolbox`, `nav2_minimal_tb3_sim`, `ros_gz_sim`, `ros_gz_bridge`, `rmw_cyclonedds_cpp`,
`rviz2` and `xacro`, and the Python packages), then:

```bash
bash setup/build.sh            # colcon build of the robot package
bash scripts/check.sh          # offline tests, about a minute; no simulator
bash setup/run_console.sh      # starts the console and opens its link in your browser
bash setup/install_launcher.sh # optional: AMR Console and AMR Speech Lab in the app menu
```

Use one checkout per terminal, and don't source another workspace that also contains the
`amr_physical_ai` package. Only one console runs at a time (it serves `127.0.0.1:8765`).

## 2. Language model (Office tasks)

The language module runs a local model runner, Ollama v0.40.12, from its own `.runtime/`
folder, inside a `bubblewrap` sandbox, on `127.0.0.1` with cloud features off. The sandbox hides
your home folder from the runner, so the repository must be inside your home folder (not the
home folder itself). The console uses
the model `qwen3.5:4b-q4_K_M` (profile `office_B`). The download needs internet access once;
afterwards everything runs offline. From the `amr-language/` folder:

```bash
mkdir -p .runtime/downloads .runtime/ollama .runtime/models .runtime/tmp .runtime/cache
curl --fail --location --proto '=https' --tlsv1.2 --max-filesize 2000000000 \
  --output .runtime/downloads/ollama-linux-amd64-v0.40.12.tar.zst \
  https://github.com/ollama/ollama/releases/download/v0.40.12/ollama-linux-amd64.tar.zst
sha256sum --check data/ollama-v0.40.12.sha256
tar --zstd --extract --file .runtime/downloads/ollama-linux-amd64-v0.40.12.tar.zst \
  --directory .runtime/ollama --no-same-owner
python3 scripts/local_runner.py check-isolation
python3 scripts/local_runner.py serve          # stays in the foreground
```

In a second terminal, in the same folder:

```bash
python3 scripts/local_runner.py pull qwen3.5:4b-q4_K_M
python3 scripts/local_runner.py list
```

Then stop the runner with Ctrl+C: the console starts it itself when you press **Start language
service** on the Office tasks tab. The reviewed install, with sizes, sources and removal, is
[amr-language/docs/INSTALL_REQUEST.md](../amr-language/docs/INSTALL_REQUEST.md).

If `check-isolation` fails, the sandbox cannot start on this computer: Ubuntu 24.04 blocks
unprivileged user namespaces unless an AppArmor rule allows them. Either add such a rule, or set
`model_runner: uncontained` in `amr-console-dev/config/office_tasks.yaml`; the runner then still
listens on `127.0.0.1` only, without the extra file-system sandbox. Both are described in
[The model runner and Ubuntu 24.04](../amr-console-dev/docs/OFFICE_TASKS.md#the-model-runner-and-ubuntu-2404).

## 3. Speech-to-text (optional)

English first, then the Indian languages if you want them; the Indian-language model is gated,
so accept its conditions on Hugging Face first. The commands, sizes, hashes and checks are in
[amr-console-dev/docs/SPEECH_INSTALL_REQUEST.md](../amr-console-dev/docs/SPEECH_INSTALL_REQUEST.md);
they create `amr-speech/venv` and download the pinned models into `amr-speech/`. Then restart the
language service on the Office tasks tab, or try it in the Speech Lab
([SPEECH_LAB.md](../amr-console-dev/docs/SPEECH_LAB.md)).

## Checking the installation

| Check | Command |
|---|---|
| Everything for part 1 is present | `bash amr-console-dev/setup/check_system.sh` |
| The five offline console suites | `bash amr-console-dev/scripts/check.sh` |
| The language module | `cd amr-language && python3 -m pytest -q tests` |
| The model runner and model | `python3 scripts/check_system.py --probe-model` in `amr-language/` (writes `records/checks/system.json` there) |
| A live run | start the console, **Mapping → Start mapping**, drive a little, **STOP** |

What the robot does in each office situation is in
[amr-console-dev/docs/SCENARIOS.md](../amr-console-dev/docs/SCENARIOS.md).
