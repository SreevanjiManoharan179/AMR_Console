# Delivery task grounding pilot v3b · 20 Oct 2026

Offline preparation for task execution in simulation; typed input, rule-based grammar. Template commands are not real language.

Seed 20261019; 243 labelled commands (237 distinct texts); 60/40 split = 145/98; alpha 0.10; 1000 repeated random splits.

Maximum-slot threshold q̂ = 0.513122226531. Simultaneous inclusion uses max(1 − p(true)); no guarantee for real input is claimed.

| Policy | Success | Any question | Clarification | Confirmation | Wrong destinations | All-slot coverage |
|---|---:|---:|---:|---:|---:|---:|
| Argmax, no clarification | 84.69% | 0.00% | 0.00% | 0.00% | 15.31% | 86.73% |
| Per-slot + confirmation | 100.00% | 88.78% | 28.57% | 86.73% | 0.00% | 98.98% |

Wrong confirmation proposals: 0.00%; mean questions/command: 1.153.

The answerer is an oracle that selects its labelled intent if offered and declines incorrect confirmations. Post-confirmation wrong destinations therefore measure this policy under that assumption. They are not a human-error rate.

| 1000 splits | Mean success | Mean questions | Mean wrong destinations | Mean all-slot coverage | Coverage p05 |
|---|---:|---:|---:|---:|---:|
| baseline | 82.98% | 0.00% | 16.21% | 86.31% | 81.63% |
| per_slot | 99.19% | 87.71% | 0.00% | 97.59% | 95.92% |

## Calibration caveat

For slot errors (0, 0.9), min = 0 despite one badly scored slot. A cutoff on that minimum does not bound every slot error. Production calibration now uses max(1 − p(true)), so a cutoff bounds every scored slot error.

Max-score cross-check: q̂ 0.513122226531; headline joint coverage 98.98%; repeated mean 97.59%.

v3 (min) → v3b (max), success / question rate / wrong destinations / joint coverage / coverage p05: 100.00%, 88.78%, 0.00%, 98.98%, 95.92% → 100.00%, 88.78%, 0.00%, 98.98%, 95.92%.

## Limits

- Template commands are not real language; templates and scorer rules were designed together.
- Calibration uses max over slot errors (weakest slot), the simultaneous inclusion rule; template splits do not establish exchangeability with real input.
- No statistical guarantee for real input, conditional classes, execution or physical delivery.
- Question rate includes mandatory confirmations. Clarification rate excludes confirmations.
- Success is offline grounding/appropriate rejection with a simulated truthful answerer, not navigation or measured human performance.
- Zero post-confirmation wrong destinations assumes the human declines every wrong proposal; wrong proposals are reported separately.
- Ambiguous/missing-seat texts can have multiple latent intents. Random command splits share template families; no held-out-template claim.
- Repeated splits overlap; they are sensitivity summaries, not 1000 independent language datasets.
- Name probabilities use softmax T=0.1 over the <=1-edit admissible names (canonical name length >=4 for fuzzy matching).
- Known invalid syntax and unknown targets have an explicit <none> label; they are included in all-command coverage.

Saved locally: dataset, split IDs, headline decisions and all repeated-split summaries are saved beside this report. Not checked: live navigation, actual acknowledgements or delivery, real language, the physical robot or other hardware.
