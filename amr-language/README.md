# AMR Language

The language side of the AMR Console's office assistant: it turns what a person says into a short,
checked plan for the robot, and holds the conversation around it. Version 0.8.1. Proprietary,
copyright Bharathwaj Manoharan.

It runs offline on the console computer. A small local language model (Qwen 3.5, 4B, 4-bit, through
a sandboxed Ollama runner) rewrites and plans requests that the built-in English grammar does not
cover; everything the robot says comes from fixed, reviewed sentences. It never drives a robot
itself: the AMR Console starts it as a separate process and owns movement, STOP and approvals.

## What it does

| | |
|---|---|
| Understands | deliver, collect, tell, ask, call, go; chains with "then"; "if yes / if no"; "if she is not there, ..." |
| Checks | every name and place against the office list and map; item limits; at most six steps and four items |
| Decides | act (after a read-back and a yes), ask (who, where, which, "please check this"), or refuse politely |
| Converses | greets a person it visits, takes corrections, looks for someone who does not answer, asks the last person whether they need anything else |
| Languages | English in the AMR Console. Tamil, Malayalam, Telugu and Tamil in English letters are kept for the Speech Lab; their wording has not been checked by native speakers |

The full list of situations is in the console project: `amr-console-dev/docs/SCENARIOS.md`.

## How it has been checked

| Check | Result | Limits |
|---|---|---|
| Automated tests (`python3 -m pytest -q tests`) | 484, all pass with the measurement records; without them (the public release) 476 pass and 8 are skipped | they use a stand-in model, not the real one |
| Office request check, English (console project, real model) | 30 of 30 on 10 Nov 2026 | fixed test sentences, typed, no robot |
| Search for a person, spoken replies, corrections, the model reading a free reply (0.7.0, 0.8.0) | tests pass | not yet run with the real model or a real microphone |
| Earlier measurements (October to 3 November) | in `docs/history/` and `records/` | superseded versions; kept as the record in the development repository, not in the public release |

## Try it without the console

```bash
python3 -m pytest -q tests
python3 scripts/local_runner.py serve                     # terminal 1: the model runner
python3 scripts/rehearse_job.py --requester Bharathwaj --language en \
    --text 'Give the file to Divya.' --names data/task4_names_example.yaml --settings data/office_model_B.json
```

## Layout

| Folder | Holds |
|---|---|
| `amr_language/` | the module: grammar, plan checks, conversation, fixed sentences, model client |
| `tests/` | automated tests |
| `data/` | model settings and example office lists |
| `scripts/` | tools. In daily use: `local_runner.py`, `rehearse_job.py`, `review_learning.py`, and `verify.py` (which needs the measurement records). The rest produced the measurements in `docs/history/` |
| `docs/` | current documents; start at [docs/README.md](docs/README.md) |
| `docs/history/` | dated measurement reports and install records of earlier versions (development repository only) |
| `records/` | written by the module and its checks: learning suggestions, map requests, rehearsal logs, measurement data (the measurements are in the development repository only) |
| `records/development_prompts/` | the task briefs this module was built from (development repository only) |
| `reference/` | example tag and place files, licence copy |
