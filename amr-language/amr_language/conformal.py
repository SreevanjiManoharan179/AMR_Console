"""Split conformal, matching the reference maximum-slot nonconformity."""
import math

EPS = 1e-12


def probabilities_valid(probabilities):
    if (not isinstance(probabilities, dict) or not probabilities
            or any(not isinstance(k, str) or type(p) not in (int, float)
                   or not math.isfinite(p) or not 0 <= p <= 1 for k, p in probabilities.items())
            or not math.isclose(sum(probabilities.values()), 1., abs_tol=EPS)):
        raise ValueError('Invalid probability distribution')


def prediction_set(probabilities, q_hat):
    probabilities_valid(probabilities)
    if type(q_hat) not in (float, int) or not math.isfinite(q_hat) or not 0 <= q_hat <= 1:
        raise ValueError('Invalid conformal threshold')
    return [key for key, p in probabilities.items() if 1-p <= q_hat+EPS]


def calibration_score(slots, truth):
    if not truth or not set(truth) <= set(slots):
        raise ValueError('Need truth for every scored slot')
    for probabilities in slots.values():
        probabilities_valid(probabilities)
    return max(1-slots[slot].get(label, 0.) for slot, label in truth.items())


def calibrate(rows, alpha=.1):
    if not rows or not 0 < alpha < 1:
        raise ValueError('Need calibration examples and valid alpha')
    scores = sorted(calibration_score(slots, truth) for slots, truth in rows)
    rank = math.ceil((len(scores)+1)*(1-alpha))
    return 1. if rank > len(scores) else scores[rank-1]
