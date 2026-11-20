"""Run the grounding calibration pilot end to end and write the results.

Usage (from the folder that contains grounding_calibration/):
  python3 -m grounding_calibration.run_pilot --registry <named_locations.yaml> --out <results dir>

Outputs in --out: results.md, results.json, dataset.json, split.json,
decisions_headline.csv. Deterministic for a given registry and seed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
from dataclasses import asdict
from datetime import datetime, timezone

from . import NONE_LABEL
from .conformal import calibrate, coverage_bounds
from .dataset import class_counts, fingerprint, generate, split
from .evaluate import describe_distribution, summarise
from .policies import greedy_policy, threshold_policy, conformal_policy, choose_tau
from .registry import describe, load_registry
from .token_scorer import TokenScorer

POLICIES = ("greedy", "fixed threshold", "conformal + ask")



def _repo_relative(path):
    """The registry path relative to the repository root when it is inside it (no machine paths in results)."""
    full = os.path.abspath(path)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    return os.path.relpath(full, root) if full.startswith(root + os.sep) else os.path.basename(full)

def run_split(cal, test, probs, labels, alpha):
    q_hat = calibrate([probs[c.id] for c in cal], [c.intent for c in cal], alpha)
    tau = choose_tau([probs[c.id] for c in cal], [c.intent for c in cal], labels, 1 - alpha)
    decisions = {
        "greedy": [greedy_policy(probs[c.id], labels) for c in test],
        "fixed threshold": [threshold_policy(probs[c.id], labels, tau) for c in test],
        "conformal + ask": [conformal_policy(probs[c.id], labels, q_hat) for c in test],
    }
    return q_hat, tau, decisions, {k: summarise(v, test) for k, v in decisions.items()}


def fmt(x: float) -> str:
    return f"{100 * x:.1f} %"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha", type=float, default=0.10)
    ap.add_argument("--temperature", type=float, default=0.10)
    ap.add_argument("--cal-fraction", type=float, default=0.60)
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--repeats", type=int, default=1000)
    ap.add_argument("--label", default="", help="free text recorded in results, e.g. 'real registry, laptop'")
    a = ap.parse_args(argv)

    registry = load_registry(a.registry)
    scorer = TokenScorer(registry, temperature=a.temperature)
    labels = scorer.labels
    commands = generate(registry, seed=a.seed)
    probs = {c.id: scorer.probabilities(c.text) for c in commands}
    cal, test = split(commands, a.cal_fraction, seed=a.seed)
    q_hat, tau, decisions, headline = run_split(cal, test, probs, labels, a.alpha)

    rep = {p: {"success": [], "help_rate": [], "wrong_move": [], "coverage": [], "mean_set_size": []}
           for p in POLICIES}
    q_hats, taus = [], []
    for r in range(a.repeats):
        c_r, t_r = split(commands, a.cal_fraction, seed=a.seed + 1 + r)
        q_r, tau_r, _, s_r = run_split(c_r, t_r, probs, labels, a.alpha)
        q_hats.append(q_r)
        taus.append(tau_r)
        for p in POLICIES:
            for m in rep[p]:
                rep[p][m].append(s_r[p][m])
    repeated = {p: {m: describe_distribution(v) for m, v in ms.items()} for p, ms in rep.items()}
    lo, hi = coverage_bounds(len(cal), a.alpha)

    os.makedirs(a.out, exist_ok=True)
    meta = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": a.label, "registry_path": _repo_relative(a.registry),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "alpha": a.alpha, "temperature": a.temperature, "cal_fraction": a.cal_fraction,
        "seed": a.seed, "repeats": a.repeats, "scorer": scorer.name,
        "dataset_fingerprint": fingerprint(commands), "labels": labels,
    }
    result = {
        "meta": meta,
        "registry": [asdict(e) for e in registry],
        "counts": {"all": class_counts(commands), "calibration": class_counts(cal), "test": class_counts(test)},
        "headline": {"q_hat": q_hat, "tau": tau, "summary": headline},
        "repeated_splits": {"q_hat": describe_distribution(q_hats), "tau": describe_distribution(taus),
                            "policies": repeated},
        "expected_coverage_bounds": [lo, hi],
    }
    with open(os.path.join(a.out, "results.json"), "w") as f:
        json.dump(result, f, indent=2)
    with open(os.path.join(a.out, "dataset.json"), "w") as f:
        json.dump([asdict(c) for c in commands], f, indent=2)
    with open(os.path.join(a.out, "split.json"), "w") as f:
        json.dump({"seed": a.seed, "calibration": [c.id for c in cal], "test": [c.id for c in test]}, f, indent=2)
    with open(os.path.join(a.out, "decisions_headline.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "class", "text", "intent", "top_label", "top_p"] +
                   [f"{p}: decision" for p in POLICIES] + ["conformal question"])
        for i, c in enumerate(test):
            p = probs[c.id]
            top = max(labels, key=lambda y: (p[y], -labels.index(y)))
            row = [c.id, c.cls, c.text, c.intent, top, f"{p[top]:.4f}"]
            for pol in POLICIES:
                d = decisions[pol][i]
                row.append(d.kind + (f" {d.target}" if d.target else "") +
                           (f" {list(d.options)}" if d.options else ""))
            row.append(decisions["conformal + ask"][i].question)
            w.writerow(row)

    # ---- results.md -------------------------------------------------------
    L = []
    L.append("# Grounding calibration pilot results: calibrated ambiguity handling, offline\n")
    L.append(f"Created {meta['created_utc']} · {a.label or 'no label'} · dataset fingerprint "
             f"`{meta['dataset_fingerprint']}` · scorer {scorer.name}, T = {a.temperature} · "
             f"alpha = {a.alpha} · seed {a.seed}\n")
    L.append("**What this is.** The first calibrated result for grounding calibration on held-out, template-generated "
             "commands, computed offline with a rule-based scorer. It shows the mechanism and its "
             "calibration. It is not the complete method: speculative navigation is designed, not built; "
             "there are no real users and no live robot in these numbers.\n")
    L.append("## Registry read\n\n```\n" + describe(registry) + "\n```\n")
    L.append("## Commands\n")
    L.append("| Class | All | Calibration | Test |\n|---|---|---|---|")
    for cls in sorted(result["counts"]["all"]):
        L.append(f"| {cls} | {result['counts']['all'][cls]} | {result['counts']['calibration'].get(cls, 0)} | "
                 f"{result['counts']['test'].get(cls, 0)} |")
    L.append(f"| **total** | {len(commands)} | {len(cal)} | {len(test)} |\n")
    L.append(f"## Headline split (seed {a.seed})\n")
    L.append(f"Conformal threshold q_hat = {q_hat:.4f} (a label enters the set when its probability "
             f">= {1 - q_hat:.4f}). fixed threshold tau = {tau:.4f}, the smallest that reaches "
             f"{fmt(1 - a.alpha)} success on the calibration set.\n")
    L.append("| Policy | Success | Questions asked | Wrong moves | Coverage | Mean set size |\n|---|---|---|---|---|---|")
    for p in POLICIES:
        s = headline[p]
        L.append(f"| {p} | {fmt(s['success'])} | {fmt(s['help_rate'])} | {fmt(s['wrong_move'])} | "
                 f"{fmt(s['coverage'])} | {s['mean_set_size']:.2f} |")
    L.append("")
    L.append("Per class, conformal + ask (test split):\n")
    L.append("| Class | n | Success | Questions | Wrong moves | Coverage |\n|---|---|---|---|---|---|")
    for cls, s in headline["conformal + ask"]["per_class"].items():
        L.append(f"| {cls} | {s['n']} | {fmt(s['success'])} | {fmt(s['help_rate'])} | "
                 f"{fmt(s['wrong_move'])} | {fmt(s['coverage'])} |")
    L.append("")
    L.append(f"## Over {a.repeats} random calibration/test splits\n")
    L.append("| Policy | Success mean (5th–95th pct) | Questions mean | Wrong moves mean | Coverage mean (5th–95th pct) |\n|---|---|---|---|---|")
    for p in POLICIES:
        r = repeated[p]
        L.append(f"| {p} | {fmt(r['success']['mean'])} ({fmt(r['success']['p05'])}–{fmt(r['success']['p95'])}) | "
                 f"{fmt(r['help_rate']['mean'])} | {fmt(r['wrong_move']['mean'])} | "
                 f"{fmt(r['coverage']['mean'])} ({fmt(r['coverage']['p05'])}–{fmt(r['coverage']['p95'])}) |")
    L.append("")
    L.append(f"Expected conformal coverage from theory: between {fmt(lo)} and {fmt(hi)} for "
             f"{len(cal)} calibration commands (the upper bound assumes no tied scores; this "
             f"scorer produces ties, so coverage can sit above it).\n")
    L.append("## Example conformal + ask decisions (headline test split)\n")
    shown = set()
    for i, c in enumerate(test):
        d = decisions["conformal + ask"][i]
        key = (c.cls, d.kind)
        if key in shown:
            continue
        shown.add(key)
        what = d.question or (f"go to {d.target}" if d.kind == "act" else "reply: I don't know that place")
        L.append(f"- *{c.text}* ({c.cls}, meant: {c.intent}) → **{d.kind}**: {what}")
    L.append("")
    L.append("## Limits, stated plainly\n")
    L.append("- Commands are generated from templates, not spoken by real users; the scorer's stop-word "
             "list was written with the templates in view, so these commands are easier than real speech.")
    L.append("- The token scorer is rule-based and deterministic. With it, a tuned fixed threshold can match "
             "the conformal policy on success and question rate; the difference to report is the conformal policy's "
             "calibrated guarantee and its closed question over the candidate set, not a higher success rate.")
    L.append("- Coverage is guaranteed on average over random splits and only for commands like the "
             "calibration commands. It is not guaranteed per class.")
    L.append("- Ties in the top probability are broken by label order (locations before <none>).")
    L.append("- Speculative navigation is not built; no robot moved for these numbers.")
    with open(os.path.join(a.out, "results.md"), "w") as f:
        f.write("\n".join(L) + "\n")

    print(f"registry entries: {len(registry)}; commands: {len(commands)} (cal {len(cal)}, test {len(test)})")
    print(f"q_hat {q_hat:.4f}; tau {tau:.4f}")
    for p in POLICIES:
        s = headline[p]
        print(f"{p:<20} success {fmt(s['success'])}  questions {fmt(s['help_rate'])}  "
              f"wrong moves {fmt(s['wrong_move'])}  coverage {fmt(s['coverage'])}")
    print(f"conformal + ask coverage over {a.repeats} splits: mean {fmt(repeated['conformal + ask']['coverage']['mean'])}, "
          f"5th pct {fmt(repeated['conformal + ask']['coverage']['p05'])}")
    print(f"written: {os.path.abspath(a.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
