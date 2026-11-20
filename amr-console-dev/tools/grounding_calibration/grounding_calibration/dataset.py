"""Template-generated, labelled commands for the grounding calibration pilot (seeded, reproducible).

Classes (intent = the location the speaker means; "<none>" = not a known place):
  valid_exact    a location's name or alias with a verb ("go to the east station")
  valid_typo     one interior letter dropped from a name token of 5+ letters
  valid_partial  only the token that is unique to one location ("go to north")
  valid_extra    filler words around the name ("I need you at the east station now")
  ambiguous      only a token shared by several locations ("go to the station");
                 the intent is one specific member, spread evenly across members
  unknown        a place that is not in the registry ("go to the kitchen"), plus
                 hard negatives that reuse a shared token ("go to the west station")

These are template commands, not recordings of real users. They test the
mechanism and the calibration, not robustness to real language.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Sequence

from . import NONE_LABEL
from .registry import Entry
from .token_scorer import content_tokens

VERBS = ["go to", "head to", "move to", "take me to", "navigate to", "drive to",
         "please go to", "can you go to", "go over to", "get to"]
ARTICLES = ["the ", ""]
EXTRA = ["I need you at {art}{name} now", "could you please head over to {art}{name}",
         "go to {art}{name} right away", "robot, go to {art}{name}",
         "okay, take me to {art}{name}", "please drive to {art}{name} now"]
AMBIGUOUS = ["go to the {w}", "go to a {w}", "head to any {w}", "take me to the nearest {w}",
             "go to one of the {w}s", "please drive to a {w}"]
UNKNOWN_PLACES = ["kitchen", "lab", "office", "front door", "charging dock", "cafeteria",
                  "meeting room", "library", "parking lot", "elevator", "lobby", "store room",
                  "conference hall", "workshop"]
HARD_NEG_MODIFIERS = ["west", "upper", "old", "main"]
HOME_FORMS = ["go home", "go back home", "return home", "head home now"]

TARGET_COUNTS = {"valid_exact": 36, "valid_typo": 12, "valid_partial": 12,
                 "valid_extra": 12, "ambiguous": 18, "unknown": 18}


@dataclass(frozen=True)
class Command:
    id: str
    text: str
    cls: str
    intent: str                       # a location name, or "<none>"
    acceptable: tuple[str, ...] = field(default=())   # every location the words fit


def _shared_tokens(registry: Sequence[Entry]) -> dict[str, list[str]]:
    owners: dict[str, set[str]] = {}
    for e in registry:
        for v in e.variants:
            for t in content_tokens(v):
                owners.setdefault(t, set()).add(e.name)
    return {t: sorted(n) for t, n in owners.items() if len(n) >= 2}


def _unique_tokens(registry: Sequence[Entry]) -> dict[str, str]:
    owners: dict[str, set[str]] = {}
    for e in registry:
        for v in e.variants:
            for t in content_tokens(v):
                owners.setdefault(t, set()).add(e.name)
    out = {}
    for e in registry:
        if len(content_tokens(e.name)) < 2:
            continue
        for t in content_tokens(e.name):
            if owners[t] == {e.name}:
                out[t] = e.name
    return out


def _drop_letter(word: str, rng: random.Random) -> str:
    i = rng.randrange(1, len(word) - 1)
    return word[:i] + word[i + 1:]


def generate(registry: Sequence[Entry], seed: int = 20261015,
             counts: dict[str, int] | None = None) -> list[Command]:
    counts = dict(TARGET_COUNTS if counts is None else counts)
    rng = random.Random(seed)
    registry_tokens = {t for e in registry for v in e.variants for t in content_tokens(v)}
    pools: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {k: [] for k in counts}

    for e in registry:
        for v in e.variants:
            for verb in VERBS:
                for art in ARTICLES:
                    pools["valid_exact"].append((f"{verb} {art}{v}", e.name, (e.name,)))
            for tpl in EXTRA:
                for art in ARTICLES:
                    pools["valid_extra"].append((tpl.format(art=art, name=v), e.name, (e.name,)))
            toks = v.split()
            for j, t in enumerate(toks):
                if len(t) >= 5:
                    for verb in VERBS:
                        typo = list(toks)
                        typo[j] = _drop_letter(t, rng)
                        if typo[j] not in registry_tokens:
                            pools["valid_typo"].append((f"{verb} the {' '.join(typo)}", e.name, (e.name,)))
        if e.name == "home":
            for f_ in HOME_FORMS:
                pools["valid_exact"].append((f_, e.name, (e.name,)))
    for t, owner in _unique_tokens(registry).items():
        for verb in VERBS:
            pools["valid_partial"].append((f"{verb} {t}", owner, (owner,)))
    shared = _shared_tokens(registry)
    for w, members in shared.items():
        for tpl in AMBIGUOUS:
            text = tpl.format(w=w)
            for m in members:   # same words, each member meant once: spread evenly
                pools["ambiguous"].append((text, m, tuple(members)))
    for place in UNKNOWN_PLACES:
        if not (set(content_tokens(place)) & registry_tokens):
            for verb in VERBS[:4]:
                pools["unknown"].append((f"{verb} the {place}", NONE_LABEL, ()))
    hard_neg = []
    for w in shared:
        for mod in HARD_NEG_MODIFIERS:
            if mod not in registry_tokens:
                hard_neg.append((f"go to the {mod} {w}", NONE_LABEL, ()))

    out: list[Command] = []
    for cls, n in counts.items():
        pool = list(dict.fromkeys(pools[cls]))     # de-duplicate, keep order
        if cls == "unknown":
            take_hard = min(len(hard_neg), max(1, n // 4))
            chosen = rng.sample(hard_neg, take_hard) if take_hard else []
            chosen += rng.sample(pool, min(len(pool), n - take_hard))
        else:
            chosen = rng.sample(pool, min(len(pool), n))
        for k, (text, intent, acceptable) in enumerate(chosen):
            out.append(Command(id=f"{cls}-{k:03d}", text=text, cls=cls,
                               intent=intent, acceptable=acceptable))
    return out


def split(commands: Sequence[Command], cal_fraction: float = 0.6,
          seed: int = 20261015) -> tuple[list[Command], list[Command]]:
    """One random split of the pooled commands (all classes mixed).

    Calibrating on a random sample of every class keeps calibration and test
    exchangeable, which is what the conformal guarantee needs. (The original
    design said to calibrate on valid commands only; that would break
    exchangeability for ambiguous and unknown commands, so it is not done.)
    """
    idx = list(range(len(commands)))
    random.Random(seed).shuffle(idx)
    k = int(round(cal_fraction * len(commands)))
    return [commands[i] for i in idx[:k]], [commands[i] for i in idx[k:]]


def fingerprint(commands: Sequence[Command]) -> str:
    blob = json.dumps([asdict(c) for c in commands], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def class_counts(commands: Sequence[Command]) -> dict[str, int]:
    return dict(sorted(Counter(c.cls for c in commands).items()))
