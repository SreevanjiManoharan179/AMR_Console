# grounding_calibration — calibrated ambiguity handling for typed commands, offline pilot

Pure Python 3 + PyYAML; no network, no model, no ROS, nothing moves a robot. Typed commands use its output (`config/calibration/<world>/grounding/results.json`).

## What it does

Given the approved named locations (the registry that typed commands use), it generates about 108 labelled template commands, scores every location for every command with the **token scorer** (rule-based token match), calibrates a **split-conformal** threshold at alpha = 0.10, and compares three policies on held-out commands:

| Policy | Rule |
|---|---|
| greedy | always act on the top label |
| fixed threshold | act if the top probability ≥ tau, else ask an open question; tau = the cheapest threshold reaching 90 % success on calibration |
| conformal + ask (the method typed commands use, minus motion) | one location in the set → act; {<none>} → reject; several → ask a closed question over them; empty → ask to rephrase |

Metrics: success, questions asked, wrong moves (moving to a place that was not meant), coverage (intended label in the set), mean set size; per class; and the same over 1000 random calibration/test splits.

A fourth policy, speculative navigation (move along the shared path while asking), is designed but **not built**.

## Files

```
grounding_calibration/registry.py   load named_locations.yaml (tolerant loader; `python3 -m grounding_calibration.registry <yaml>` prints what it read)
grounding_calibration/token_scorer.py   token scorer (token match, plural folding, <none> label, softmax T = 0.1)
grounding_calibration/dataset.py    seeded template commands in six classes; random pooled split
grounding_calibration/conformal.py  split conformal: q_hat = ceil((n+1)(1-alpha))-th smallest of 1 - p(intent)
grounding_calibration/policies.py   greedy, fixed threshold, conformal + ask, the closed-question text, outcome scoring, tau selection
grounding_calibration/evaluate.py   metrics
grounding_calibration/run_pilot.py  end-to-end run; writes results.md/.json, dataset.json, split.json, decisions_headline.csv
tests/test_grounding_calibration.py 21 offline tests on fixtures (never the real files)
fixtures/standin_named_locations.yaml  the four places of the first traversal map, no aliases — tests only
fixtures/real_shape_named_locations.yaml  a copy of the real registry (schema + aliases) — tests only
```

## Run

```
cd <folder that holds grounding_calibration/>
python3 -m pytest -q                       # expect: 21 passed
python3 -m grounding_calibration.registry <path>/named_locations.yaml
python3 -m grounding_calibration.run_pilot --registry <path>/named_locations.yaml --out <results dir> --label "<where and what>"
```

## Token scorer v2 (18 Oct 2026, the owner's decision)

The first real-registry run (18 Oct 2026, commit c6a8ed2) showed that "right" was a stop word, put there so the filler "right away" drops out. It also cut the approved alias "right station" (east station) down to "station", which then matched every station: all 7 of the conformal policy's wrong moves came from it, and "go to the station" acted on east instead of asking. v2 drops only the two-word phrase "right away" and keeps "right" as a content word. The fix was found by looking at the first run's errors on the same 108 commands, so v2's numbers are not a clean held-out result; both runs stay on record.

## One design correction against the original design

The original design (15 Oct 2026) said: calibrate on 60 % of the *valid* commands and test on the rest plus all ambiguous and unknown commands. That breaks the exchangeability the conformal guarantee needs (calibration would contain no ambiguous or unknown commands, so the threshold would not account for them). This package calibrates on a random 60 % of *all* commands. Ambiguous commands carry the specific location the speaker meant, spread evenly over the stations, as in KnowNo.

## Results on the stand-in registry (NOT evidence)

`results_standin/` holds a run on the four place names of the first traversal map without aliases, in the cloud sandbox. It shows the code works and what to expect. The numbers that count come from the run on the real registry on the laptop.

Headline split, 43 test commands: greedy success 79.1 %, wrong moves 20.9 %; fixed threshold success 83.7 %, questions 4.7 %, wrong moves 16.3 %; **conformal + ask success 100.0 %, questions 25.6 %, wrong moves 0.0 %, coverage 95.3 %**. Over 1000 splits: conformal + ask coverage mean 96.3 % (5th percentile 93.0 %), against a 90 % target. The conformal threshold was identical in all 1000 splits; the fixed threshold's tuned value flipped between two regimes (0.33 or 0.97) depending on which commands landed in calibration.

## Limits

Template commands, not real users; the stop-word list was written with the templates in view; the token scorer is deterministic and rule-based; coverage is marginal (on average over splits), not per class, and holds only for commands like the calibration commands; no robot moved; speculative navigation not built.
