# Third-party software

None of this is included in the repository; each part is installed separately and is used under
its own licence. The licences below were read from each project's licence file, package
metadata or model card (on 20 Nov 2026, or when the model was pinned); check the project for its
current terms before you redistribute anything.

The one exception is the three Nav2 parameter files listed in [NOTICE](NOTICE), derived from
`nav2_bringup` (Apache-2.0; full text in [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)).

## Robot software (installed with ROS 2 Jazzy)

| Software | Used for | Licence |
|---|---|---|
| [ROS 2 Jazzy](https://docs.ros.org/en/jazzy/) | middleware, launch, RViz | Apache-2.0 (core packages) |
| [Nav2](https://github.com/ros-navigation/navigation2): `nav2_bringup` | navigation launch and parameters | Apache-2.0 |
| Nav2: `nav2_mppi_controller` | MPPI controller | MIT |
| Nav2: DWB (`dwb_core`) | DWB controller | BSD-3-Clause |
| Nav2: `nav2_regulated_pure_pursuit_controller` | RPP controller | Apache-2.0 |
| Nav2: `nav2_amcl` | localisation on a saved map | LGPL-2.1-or-later |
| [SLAM Toolbox](https://github.com/SteveMacenski/slam_toolbox) | mapping | LGPL |
| [Gazebo Harmonic](https://gazebosim.org/) (`gz sim` 8) | simulator | Apache-2.0 |
| [ros_gz](https://github.com/gazebosim/ros_gz) (`ros_gz_sim`, `ros_gz_bridge`) | ROS–Gazebo bridge | Apache-2.0 |
| [nav2_minimal_tb3_sim](https://github.com/ros-navigation/nav2_minimal_turtlebot_simulation) | TurtleBot3 Waffle simulation model | Apache-2.0 |

## Language and speech (optional; Office tasks and Speech Lab)

| Software or model | Used for | Licence |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) v0.40.12 | local model runner, started in a sandbox | MIT |
| [Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B), `qwen3.5:4b-q4_K_M` | the language model | Apache-2.0 |
| [bubblewrap](https://github.com/containers/bubblewrap) | the model runner's sandbox (system package) | LGPL |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [CTranslate2](https://github.com/OpenNMT/CTranslate2) | English speech-to-text | MIT |
| [Systran/faster-whisper-base.en](https://huggingface.co/Systran/faster-whisper-base.en) and `small.en` | English speech models | MIT |
| [ai4bharat/indic-conformer-600m-multilingual](https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual) | Tamil, Malayalam and Telugu speech model | MIT; gated: accept its conditions on Hugging Face first |
| [IndicTrans2](https://github.com/AI4Bharat/IndicTrans2) and [ai4bharat/indictrans2-indic-en-dist-200M](https://huggingface.co/ai4bharat/indictrans2-indic-en-dist-200M) | translation, in one language-module comparison only | MIT |
| [FLEURS](https://huggingface.co/datasets/google/fleurs) (`google/fleurs`) | speech test clips that `scripts/speech_fleurs.py` downloads | CC-BY-4.0 |

The exact versions and hashes are pinned in `amr-console-dev/config/speech_models.json`,
`amr-console-dev/config/speech_requirements_*.lock` and `amr-language/data/`.

## Python packages for the tests

| Package | Licence |
|---|---|
| pytest | MIT |
| PyYAML | MIT |
| NumPy | BSD-3-Clause (with bundled parts under 0BSD, MIT, Zlib, CC0-1.0) |
| SciPy | BSD-3-Clause |
| pandas | BSD-3-Clause |
| Matplotlib | Matplotlib licence (PSF-based) |
| Pillow | MIT-CMU |
| packaging | Apache-2.0 or BSD-2-Clause |
