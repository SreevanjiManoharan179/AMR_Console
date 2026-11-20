"""Token scorer: a rule-based token-match scorer over registry entries.

For a command, every entry gets a match score r in [0, 1]: the best, over the
entry's name and aliases, of the fraction of that variant's content tokens
found in the command (exact match, or a close spelling for tokens of four or
more letters). A "<none>" label gets the fraction of the command's content
tokens that the best-matching entries do not explain (a command with no
content tokens gets 1). Scores become probabilities with a softmax at a fixed
temperature T.

Why a <none> label: the conformal set must be able to say "this is not one of
our places" (unknown commands). Why the unexplained-token rule: "go to the
station" is fully explained by three stations (ambiguous, not unknown), while
"go to the kitchen" is explained by nothing (unknown).

Known limitation, stated in every result: the stop-word list was written with
the command templates in view, so template commands are easier for this
scorer than real speech would be.

Change v2 (18 Oct 2026, the owner's decision after the first real-registry run):
"right" was a single stop word so that the filler "right away" would drop out.
That also erased the approved alias "right station" (east station) down to
"station", which then matched every station and made "go to the station" act
on east. v2 drops only the two-word phrase "right away" and keeps "right" as a
content word. The first run (pilot of 18 Oct 2026) stays on record;
the fix was found by inspecting that run's errors on the same commands.
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Sequence

from . import NONE_LABEL
from .registry import Entry

STOP_WORDS = frozenset("""
a an the to go goto head move take drive navigate travel walk get bring me us you i
please can could would will kindly need want like be at in on of for towards toward
into now quickly away straight then there here over back again just robot hey
ok okay sir let lets let's and one any some nearest closest that this
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")
MIN_FUZZY_LEN = 4      # shorter tokens must match exactly ("lab" is not "lap")
FUZZY_FLOOR = 0.80     # SequenceMatcher ratio needed for a spelling match


def _singular(token: str) -> str:
    # light plural folding: "stations" -> "station"; leaves "glass", "bus" alone
    if len(token) >= 5 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


# Two-word fillers dropped as a unit, so their words stay meaningful elsewhere
# ("right station" is an approved alias). v2: only "right away".
STOP_PHRASES = frozenset({("right", "away")})


def content_tokens(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    kept, i = [], 0
    while i < len(tokens):
        if tuple(tokens[i:i + 2]) in STOP_PHRASES:
            i += 2
            continue
        kept.append(tokens[i])
        i += 1
    return [_singular(t) for t in kept if t not in STOP_WORDS]


def token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) < MIN_FUZZY_LEN:
        return 0.0
    r = SequenceMatcher(None, a, b).ratio()
    return r if r >= FUZZY_FLOOR else 0.0


def variant_match(variant: Sequence[str], command: Sequence[str]) -> tuple[float, set[int]]:
    """Fraction of the variant's tokens found in the command, and which command tokens matched."""
    if not variant:
        return 0.0, set()
    total, used = 0.0, set()
    for vt in variant:
        best, best_i = 0.0, None
        for i, ct in enumerate(command):
            s = token_similarity(vt, ct)
            if s > best:
                best, best_i = s, i
        total += best
        if best_i is not None:
            used.add(best_i)
    return total / len(variant), used


class TokenScorer:
    """Rule-based scorer over the registry; deterministic, no model, no network."""

    name = "token match v2 (rule-based; \"right away\" phrase fix)"

    def __init__(self, registry: Sequence[Entry], temperature: float = 0.1):
        if temperature <= 0:
            raise ValueError("temperature must be positive")
        self.registry = list(registry)
        self.temperature = temperature
        self.labels = [e.name for e in self.registry] + [NONE_LABEL]
        self._variants = {e.name: [content_tokens(v) for v in e.variants] for e in self.registry}

    def raw_scores(self, command: str) -> dict[str, float]:
        ct = content_tokens(command)
        r: dict[str, float] = {}
        used_by: dict[str, set[int]] = {}
        for e in self.registry:
            best, best_used = 0.0, set()
            for v in self._variants[e.name]:
                s, used = variant_match(v, ct)
                if s > best:
                    best, best_used = s, used
            r[e.name] = best
            used_by[e.name] = best_used
        top = max(r.values()) if r else 0.0
        if not ct:
            r[NONE_LABEL] = 1.0
        elif top <= 0.0:
            r[NONE_LABEL] = 1.0
        else:
            explained: set[int] = set()
            for name, s in r.items():
                if s == top:
                    explained |= used_by[name]
            r[NONE_LABEL] = (len(ct) - len(explained)) / len(ct)
        return r

    def probabilities(self, command: str) -> dict[str, float]:
        r = self.raw_scores(command)
        m = max(r.values())
        z = {k: math.exp((v - m) / self.temperature) for k, v in r.items()}
        s = sum(z.values())
        return {k: z[k] / s for k in self.labels}


def make_scorer_b(registry: Sequence[Entry]):
    """Sentence-embedding scorer (a local model) behind a guarded import.

    Not used in the 15-18 Oct pilot: it was left out because the model was not
    installed by the cut-off. Returns None when the package is missing.
    """
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return None
    raise NotImplementedError("the sentence-embedding scorer is out of scope for this pilot")
