"""Metrics for the grounding calibration pilot."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev
from typing import Iterable, Sequence

from .dataset import Command
from .policies import Decision, outcome


def set_size(d: Decision) -> int:
    if d.kind == "ask":
        return len(d.options)
    if d.kind in ("act", "reject"):
        return 1
    return 0


def summarise(decisions: Sequence[Decision], commands: Sequence[Command]) -> dict:
    rows = [outcome(d, c.intent) | {"cls": c.cls, "size": set_size(d)}
            for d, c in zip(decisions, commands)]
    n = len(rows)
    out = {
        "n": n,
        "success": sum(r["success"] for r in rows) / n,
        "help_rate": sum(r["asked"] for r in rows) / n,
        "wrong_move": sum(r["wrong_move"] for r in rows) / n,
        "coverage": sum(r["covered"] for r in rows) / n,
        "mean_set_size": mean(r["size"] for r in rows),
    }
    per = defaultdict(list)
    for r in rows:
        per[r["cls"]].append(r)
    out["per_class"] = {
        cls: {
            "n": len(rs),
            "success": sum(r["success"] for r in rs) / len(rs),
            "help_rate": sum(r["asked"] for r in rs) / len(rs),
            "wrong_move": sum(r["wrong_move"] for r in rs) / len(rs),
            "coverage": sum(r["covered"] for r in rs) / len(rs),
        }
        for cls, rs in sorted(per.items())
    }
    return out


def describe_distribution(values: Iterable[float]) -> dict:
    v = sorted(values)
    n = len(v)

    def pct(q: float) -> float:
        return v[min(n - 1, max(0, int(round(q * (n - 1)))))]

    return {"mean": mean(v), "sd": pstdev(v), "p05": pct(0.05), "p50": pct(0.5), "p95": pct(0.95),
            "min": v[0], "max": v[-1], "runs": n}
