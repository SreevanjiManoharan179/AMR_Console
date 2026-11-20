"""Decision policies compared in the pilot (baselines, offline subset).

Each policy maps a command's label probabilities to one decision:
  act y      send the robot to location y (moves the robot)
  reject     say the place is not known (no motion)
  ask C      ask a closed question over the locations in C (no motion yet)
  ask_open   ask an open question ("where should I go?") (no motion yet)
  rephrase   ask the user to say it again (no motion)

greedy              always acts on the most probable label
fixed threshold     acts if the top probability >= tau, otherwise asks openly;
                    tau is set on the calibration set (see choose_tau)
conformal + ask     acts on a single-location set, rejects a {<none>} set,
                    asks over the set when it holds several labels,
                    asks to rephrase when the set is empty
speculative navigation (not built): move along the shared path while asking;
designed but not built here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from . import NONE_LABEL
from .conformal import prediction_set


@dataclass(frozen=True)
class Decision:
    kind: str                          # act | reject | ask | ask_open | rephrase
    target: str | None = None          # for act
    options: tuple[str, ...] = ()      # for ask
    question: str = ""

    @property
    def moves(self) -> bool:
        return self.kind == "act"

    @property
    def asks(self) -> bool:
        return self.kind in ("ask", "ask_open", "rephrase")


def _top(p: Mapping[str, float], labels: Sequence[str]) -> str:
    # ties broken by label order (locations before <none>), stated in the results
    return max(labels, key=lambda y: (p[y], -labels.index(y)))


def question_text(options: Sequence[str]) -> str:
    places = [o for o in options if o != NONE_LABEL]
    names = [f"the {o}" for o in places]
    if len(names) == 1:
        body = f"Did you mean {names[0]}"
    else:
        body = "Which one: " + ", ".join(names[:-1]) + f" or {names[-1]}"
    if NONE_LABEL in options:
        body += ", or somewhere else"
    return body + "?"


def greedy_policy(p: Mapping[str, float], labels: Sequence[str]) -> Decision:
    y = _top(p, labels)
    return Decision("reject") if y == NONE_LABEL else Decision("act", target=y)


def threshold_policy(p: Mapping[str, float], labels: Sequence[str], tau: float) -> Decision:
    y = _top(p, labels)
    if p[y] >= tau:
        return Decision("reject") if y == NONE_LABEL else Decision("act", target=y)
    return Decision("ask_open", question="Where would you like me to go?")


def conformal_policy(p: Mapping[str, float], labels: Sequence[str], q_hat: float) -> Decision:
    c = prediction_set(p, q_hat, labels)
    if not c:
        return Decision("rephrase", question="Sorry, could you say that again?")
    if c == [NONE_LABEL]:
        return Decision("reject")
    if len(c) == 1:
        return Decision("act", target=c[0])
    return Decision("ask", options=tuple(c), question=question_text(c))


def outcome(decision: Decision, intent: str) -> dict[str, bool]:
    """Score one decision against the speaker's intent.

    success      the robot ends up doing what was meant: acts on the intent; rejects
                 or rephrases when nothing known was meant; or asks a question whose
                 answer leads there (a closed question only if the intent is offered)
    wrong_move   the robot moves to a location that was not meant
    covered      the intent is in the decision's label set (for coverage)
    """
    k = decision.kind
    if k == "act":
        ok = decision.target == intent
        return {"success": ok, "wrong_move": not ok, "asked": False,
                "covered": ok}
    if k == "reject":
        ok = intent == NONE_LABEL
        return {"success": ok, "wrong_move": False, "asked": False, "covered": ok}
    if k == "ask":
        ok = intent in decision.options
        return {"success": ok, "wrong_move": False, "asked": True, "covered": ok}
    if k == "ask_open":
        return {"success": True, "wrong_move": False, "asked": True, "covered": True}
    if k == "rephrase":
        ok = intent == NONE_LABEL
        return {"success": ok, "wrong_move": False, "asked": True, "covered": False}
    raise ValueError(k)


def choose_tau(probs: Sequence[Mapping[str, float]], intents: Sequence[str],
               labels: Sequence[str], target_success: float) -> float:
    """Smallest tau whose calibration success reaches the target (fewest questions).

    Raising tau only turns acts into open questions, and an open question always
    succeeds, so success rises with tau and the smallest qualifying tau asks least.
    """
    candidates = sorted({p[_top(p, labels)] for p in probs})
    candidates.append(1.0 + 1e-9)
    n = len(intents)
    for tau in candidates:
        ok = sum(outcome(threshold_policy(p, labels, tau), y)["success"] for p, y in zip(probs, intents))
        if ok / n >= target_success:
            return tau
    return candidates[-1]
