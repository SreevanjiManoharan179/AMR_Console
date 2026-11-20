"""Split conformal prediction over location labels (KnowNo-style).

Nonconformity of a labelled command: s = 1 - p(intended label).
Threshold: q_hat = the ceil((n + 1)(1 - alpha))-th smallest calibration score.
Prediction set: every label y with 1 - p(y) <= q_hat.

Guarantee (marginal, over random calibration/test draws): if calibration and
test commands are exchangeable, P(intended label in set) >= 1 - alpha.
It is not a per-class guarantee, and it does not hold for commands unlike the
calibration set (for example real speech after template calibration).
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

EPS = 1e-12   # equal scores must fall on the same side of the threshold


def calibrate(probabilities: Sequence[Mapping[str, float]], intents: Sequence[str],
              alpha: float) -> float:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    if len(probabilities) != len(intents) or not intents:
        raise ValueError("need one intent per calibration command, and at least one")
    scores = sorted(1.0 - p[y] for p, y in zip(probabilities, intents))
    n = len(scores)
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:              # too few calibration points for this alpha: include everything
        return 1.0
    return scores[k - 1]


def prediction_set(p: Mapping[str, float], q_hat: float, labels: Sequence[str]) -> list[str]:
    return [y for y in labels if 1.0 - p[y] <= q_hat + EPS]


def coverage_bounds(n_cal: int, alpha: float) -> tuple[float, float]:
    """Expected coverage lies in [1 - alpha, 1 - alpha + 1/(n + 1)] (upper bound needs no ties)."""
    return 1 - alpha, min(1.0, 1 - alpha + 1 / (n_cal + 1))
