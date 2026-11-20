# Grounding calibration pilot results: calibrated ambiguity handling, offline

Created 2026-10-27T01:00:39+00:00 · proposed_system places · dataset fingerprint `447e2b424142` · scorer token match v2 (rule-based; "right away" phrase fix), T = 0.1 · alpha = 0.1 · seed 20261015

**What this is.** The first calibrated result for grounding calibration on held-out, template-generated commands, computed offline with a rule-based scorer. It shows the mechanism and its calibration. It is not the complete method: speculative navigation is designed, not built; there are no real users and no live robot in these numbers.

## Registry read

```
home                 key=home             pose=(0, 0)         aliases: base, home base, dock, charging dock
reception            key=reception        pose=(-0.6, 2.8)    aliases: front desk, lobby
conference hall      key=conference_hall  pose=(4, 2)         aliases: conference room, meeting room, boardroom
demo area            key=demo_area        pose=(8.6, 2.6)     aliases: showroom, demo zone, display area
cafeteria            key=cafeteria        pose=(12.5, 2.5)    aliases: canteen, kitchen, pantry, cafe
open office          key=open_office      pose=(4.2, -1.8)    aliases: open plan office, workstations
team office          key=team_office      pose=(10, -3)       aliases: team space, engineering office
manager cabin        key=manager_cabin    pose=(13.4, -2.6)   aliases: manager, director cabin
storage              key=storage          pose=(-1.2, -4.6)   aliases: storeroom, supply closet
```

## Commands

| Class | All | Calibration | Test |
|---|---|---|---|
| ambiguous | 12 | 6 | 6 |
| unknown | 18 | 8 | 10 |
| valid_exact | 36 | 21 | 15 |
| valid_extra | 12 | 8 | 4 |
| valid_partial | 12 | 9 | 3 |
| valid_typo | 12 | 9 | 3 |
| **total** | 102 | 61 | 41 |

## Headline split (seed 20261015)

Conformal threshold q_hat = 0.5131 (a label enters the set when its probability >= 0.4869). fixed threshold tau = 0.3282, the smallest that reaches 90.0 % success on the calibration set.

| Policy | Success | Questions asked | Wrong moves | Coverage | Mean set size |
|---|---|---|---|---|---|
| greedy | 85.4 % | 0.0 % | 14.6 % | 85.4 % | 1.00 |
| fixed threshold | 85.4 % | 0.0 % | 14.6 % | 85.4 % | 1.00 |
| conformal + ask | 100.0 % | 22.0 % | 0.0 % | 92.7 % | 1.07 |

Per class, conformal + ask (test split):

| Class | n | Success | Questions | Wrong moves | Coverage |
|---|---|---|---|---|---|
| ambiguous | 6 | 100.0 % | 100.0 % | 0.0 % | 100.0 % |
| unknown | 10 | 100.0 % | 30.0 % | 0.0 % | 70.0 % |
| valid_exact | 15 | 100.0 % | 0.0 % | 0.0 % | 100.0 % |
| valid_extra | 4 | 100.0 % | 0.0 % | 0.0 % | 100.0 % |
| valid_partial | 3 | 100.0 % | 0.0 % | 0.0 % | 100.0 % |
| valid_typo | 3 | 100.0 % | 0.0 % | 0.0 % | 100.0 % |

## Over 1000 random calibration/test splits

| Policy | Success mean (5th–95th pct) | Questions mean | Wrong moves mean | Coverage mean (5th–95th pct) |
|---|---|---|---|---|
| greedy | 90.3 % (85.4 %–95.1 %) | 0.0 % | 9.7 % | 90.3 % (85.4 %–95.1 %) |
| fixed threshold | 91.5 % (85.4 %–97.6 %) | 1.2 % | 8.5 % | 91.5 % (85.4 %–97.6 %) |
| conformal + ask | 99.8 % (100.0 %–100.0 %) | 15.7 % | 0.0 % | 95.9 % (92.7 %–100.0 %) |

Expected conformal coverage from theory: between 90.0 % and 91.6 % for 61 calibration commands (the upper bound assumes no tied scores; this scorer produces ties, so coverage can sit above it).

## Example conformal + ask decisions (headline test split)

- *go over to the open ofice* (valid_typo, meant: open office) → **act**: go to open office
- *go to the library* (unknown, meant: <none>) → **reject**: reply: I don't know that place
- *go over to home* (valid_exact, meant: home) → **act**: go to home
- *go to the main office* (unknown, meant: <none>) → **rephrase**: Sorry, could you say that again?
- *please drive to a office* (ambiguous, meant: open office) → **ask**: Which one: the open office or the team office?
- *okay, take me to open plan office* (valid_extra, meant: open office) → **act**: go to open office
- *go to open* (valid_partial, meant: open office) → **act**: go to open office

## Limits, stated plainly

- Commands are generated from templates, not spoken by real users; the scorer's stop-word list was written with the templates in view, so these commands are easier than real speech.
- The token scorer is rule-based and deterministic. With it, a tuned fixed threshold can match the conformal policy on success and question rate; the difference to report is the conformal policy's calibrated guarantee and its closed question over the candidate set, not a higher success rate.
- Coverage is guaranteed on average over random splits and only for commands like the calibration commands. It is not guaranteed per class.
- Ties in the top probability are broken by label order (locations before <none>).
- Speculative navigation is not built; no robot moved for these numbers.
