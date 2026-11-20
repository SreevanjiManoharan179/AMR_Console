"""Deterministic offline template pilot; no ROS, network or LLM."""
from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import random

import numpy as np

from .clarify import Grounder, NONE, calibrate, calibration_score, prediction_set
from .tags import Tags, ROOT

SEED = 20261019


def dataset(tags):
    rows = []
    def add(text, category, expected, truth):
        rows.append(dict(id=f't{len(rows):03}', text=text, category=category, destination=expected, truth=truth))
    for name, room in tags.rooms.items():
        display = name.replace('_', ' ')
        for template in ('go to the {}', 'please navigate to {}', 'could you head to {} now', 'kindly move to {} for me'):
            add(template.format(display), 'clean_room', 'room:'+name, {'room': name})
        for alias in room['aliases']:
            add('take this to the '+alias if alias != 'manager' else 'go to the '+alias,
                'paraphrase_template', 'room:'+name, {'room': name})
        # Deterministic dropped-letter spelling, including some deliberately difficult short names.
        typo = display[:max(1, len(display)//2)] + display[max(1, len(display)//2)+1:]
        add('go to the '+typo, 'typo_room', 'room:'+name, {'room': name})
    prefixes = ('', 'please ', 'kindly ', 'can you ', 'could you ', 'would you ', 'i want you to ')
    suffixes = ('', ' please', ' now', ' for me', ' thanks', ' thank you')
    for i, (prefix, suffix) in enumerate((p, s) for p in prefixes for s in suffixes):
        intended = ('open_office', 'team_office')[i % 2]
        add(prefix+'go to the office'+suffix, 'ambiguous_room', 'room:'+intended, {'room': intended})
    for name, person in tags.people.items():
        seat = person['seat']
        room = tags.seats[seat]['room']
        for template in ('deliver this to {}', 'take this to {}', 'please bring it to {}', 'could you give the parcel to {}'):
            add(template.format(name), 'clean_person', 'seat:'+seat, {'person': name})
        typo = name[:2]+name[3:]
        add('bring it to '+typo, 'typo_person', 'seat:'+seat, {'person': name})
        add(f'deliver to {name} in the {room.replace("_", " ")}', 'person_room',
            'seat:'+seat, {'person': name, 'room': room})
        add(f'deliver to {name} in the conference hall', 'conflict', None,
            {'person': name, 'room': 'conference_hall'})
    for i in range(8):
        name = ('Ravi', 'Rani')[i % 2]
        add(prefixes[i % len(prefixes)]+'deliver to Rai'+suffixes[i//len(prefixes)],
            'ambiguous_person', 'seat:'+tags.people[name]['seat'], {'person': name})
    for key, seat in tags.seats.items():
        room, r, c = seat['room'], seat['row'], seat['col']
        for template in ('deliver to row {r} seat {c} in the {room}',
                         'go to the {room}, give to row {r} person {c}'):
            add(template.format(r=r, c=c, room=room.replace('_', ' ')), 'clean_seat',
                'seat:'+key, {'room': room, 'seat': key})
        add(f'deliver to row {r} in the {room.replace("_", " ")}', 'missing_seat',
            'seat:'+key, {'room': room, 'seat': key})
    for key, obj in tags.objects.items():
        for verb in ('deliver this', 'take this', 'bring it'):
            add(verb+' to the '+key.replace('_', ' '), 'object', 'object:'+key, {'object': key})
    for prefix in prefixes[:4]:
        add(prefix+'take this to the manager', 'role', 'seat:'+tags.people['Divya']['seat'], {'person': 'Divya'})
    for name in ('Karthik', 'Nikhil', 'Zoe', 'Farhan', 'Lena', 'Suresh', 'Tara', 'Vikram'):
        for verb in ('take this', 'bring it'):
            add(verb+' to '+name, 'out_of_roster', None, {'person': NONE})
    for room in ('library', 'garage', 'lab', 'airport', 'gym', 'garden'):
        add('go to the '+room, 'unknown_room', None, {'room': NONE})
    for text in ('give this to the person near the printer', 'go to the office then home',
                 'do not go to reception', 'if ready deliver to Priya', 'go to Priya and Ravi',
                 'tell me where the manager is'):
        add(text, 'unsupported', None, {'validity': NONE})
    # Duplicate missing-seat text represents distinct latent intents; report that fact.
    return rows


def greedy_destination(grounder, frame, slots):
    if frame.error:
        return None
    top = {s: max(p, key=p.get) for s, p in slots.items()}
    if NONE in top.values():
        return None
    for kind in ('person', 'object', 'seat', 'room'):
        if kind in top:
            return grounder.tags.destination(kind, top[kind]).key
    return None


def evaluate(grounder, frame, row, slots, q_hat):
    expected, truth = row['destination'], row['truth']
    covered = all(y in prediction_set(slots[s], q_hat) for s, y in truth.items())
    baseline = greedy_destination(grounder, frame, slots)
    b = dict(success=float(baseline == expected), question_rate=0., clarification_rate=0.,
             confirmation_rate=0., wrong_destinations=float(baseline is not None and baseline != expected),
             joint_coverage=float(all(max(slots[s], key=slots[s].get) == y for s, y in truth.items())))
    choices, questions, clarifications, confirmations, proposed_wrong = {}, 0, 0, 0, False
    success, moved = False, None
    for _ in range(8):
        d = grounder.decide(frame, q_hat, choices)
        if d.kind == 'reject':
            success = expected is None
            break
        questions += 1
        if d.kind == 'confirm':
            confirmations += 1
            proposed_wrong = d.destination.key != expected
            # Simulated truthful human: declines a wrong proposal. This is an
            # oracle assumption, not measured human comprehension/reliability.
            if not proposed_wrong:
                moved, success = d.destination.key, True
            break
        clarifications += 1
        if d.slot == 'conflict' and expected is None:
            success = True  # oracle cancels contradictory task
            break
        answer = truth.get(d.slot)
        if answer not in d.options:
            break
        choices[d.slot] = answer
    cp = dict(success=float(success), question_rate=float(questions > 0),
              clarification_rate=float(clarifications > 0), confirmation_rate=float(confirmations > 0),
              wrong_destinations=float(moved is not None and moved != expected), joint_coverage=float(covered),
              wrong_confirmation_proposals=float(proposed_wrong), mean_questions=float(questions))
    return b, cp


def summary(values):
    return {k: sum(v[k] for v in values)/len(values) for k in values[0]}


def distribution(values):
    return dict(mean=float(np.mean(values)), p05=float(np.percentile(values, 5)),
                p50=float(np.percentile(values, 50)), p95=float(np.percentile(values, 95)),
                minimum=float(min(values)), maximum=float(max(values)))


def run(tags, seed=SEED, repeats=1000):
    g = Grounder(tags)
    rows = dataset(tags)
    frames = [g.grammar.parse(r['text']) for r in rows]
    probabilities = [g.score(f) for f in frames]
    for i, row in enumerate(rows):
        if not set(row['truth']) <= set(probabilities[i]):
            raise ValueError(f"Unscored truth slot in {row['id']}: {row['text']}")
    ncal = int(len(rows)*.6)
    def split(split_seed):
        indices = list(range(len(rows)))
        random.Random(split_seed).shuffle(indices)
        return indices[:ncal], indices[ncal:]
    @lru_cache(None)
    def metrics(index, q):
        return evaluate(g, frames[index], rows[index], probabilities[index], q)
    def score_split(split_seed):
        cal, test = split(split_seed)
        calibration = [(probabilities[i], rows[i]['truth']) for i in cal]
        q = calibrate(calibration, .1, 'max')
        qmax = calibrate(calibration, .1, 'max')
        scores = [metrics(i, q) for i in test]
        max_coverage = sum(all(y in prediction_set(probabilities[i][s], qmax)
                              for s, y in rows[i]['truth'].items()) for i in test)/len(test)
        return {'seed': split_seed, 'q_hat': q, 'max_score_q_hat_diagnostic': qmax,
                'max_score_joint_coverage_diagnostic': max_coverage,
                'baseline': summary([s[0] for s in scores]),
                'per_slot': summary([s[1] for s in scores])}, cal, test
    headline, cal, test = score_split(seed)
    repeated = [score_split(seed+1+i)[0] for i in range(repeats)]
    result = {'meta': {'seed': seed, 'alpha': .1, 'temperature': .1, 'calibration_fraction': .6,
                       'repeats': repeats, 'aggregation': 'max', 'robot': 'TurtleBot3 Waffle simulation',
                       'input': 'typed input, rule-based grammar', 'scorer': g.room_scorer.name,
                       'tag_semantics_sha256': tags.semantic_digest(), 'input_fingerprints': tags.fingerprints(),
                       'dataset_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()},
              'counts': {'total': len(rows), 'unique_texts': len({r['text'] for r in rows}),
                         'calibration': len(cal), 'test': len(test), 'by_category': dict(Counter(r['category'] for r in rows))},
              'headline': headline,
              'repeated_splits': {policy: {key: distribution([r[policy][key] for r in repeated])
                                         for key in headline[policy]} for policy in ('baseline', 'per_slot')},
              'max_score_diagnostic': distribution([r['max_score_joint_coverage_diagnostic'] for r in repeated]),
              'limitations': [
                  'Template commands are not real language; templates and scorer rules were designed together.',
                  'Calibration uses max over slot errors (weakest slot), the simultaneous inclusion rule; template splits do not establish exchangeability with real input.',
                  'No statistical guarantee for real input, conditional classes, execution or physical delivery.',
                  'Question rate includes mandatory confirmations. Clarification rate excludes confirmations.',
                  'Success is offline grounding/appropriate rejection with a simulated truthful answerer, not navigation or measured human performance.',
                  'Zero post-confirmation wrong destinations assumes the human declines every wrong proposal; wrong proposals are reported separately.',
                  'Ambiguous/missing-seat texts can have multiple latent intents. Random command splits share template families; no held-out-template claim.',
                  'Repeated splits overlap; they are sensitivity summaries, not 1000 independent language datasets.',
                  'Name probabilities use softmax T=0.1 over the <=1-edit admissible names (canonical name length >=4 for fuzzy matching).',
                  'Known invalid syntax and unknown targets have an explicit <none> label; they are included in all-command coverage.']}
    # The earlier calibration (v3) is kept beside the current one so the report can compare them.
    previous = json.loads((ROOT/'config/calibration/proposed_system/tasks/previous_v3_results.json').read_text())
    result['comparison_v3'] = comparison_line(previous, result)
    decisions = []
    for i in test:
        b, cp = metrics(i, headline['q_hat'])
        d = g.decide(frames[i], headline['q_hat'])
        decisions.append(dict(id=rows[i]['id'], initial_kind=d.kind, question=d.question,
                              probabilities=probabilities[i], baseline=b, per_slot=cp))
    return result, rows, {'calibration': [rows[i]['id'] for i in cal], 'test': [rows[i]['id'] for i in test]}, decisions, repeated


def comparison_line(previous, current):
    def values(result):
        h = result['headline']['per_slot']
        return ', '.join(f'{100*h[k]:.2f}%' for k in ('success', 'question_rate', 'wrong_destinations', 'joint_coverage')) + f", {100*result['repeated_splits']['per_slot']['joint_coverage']['p05']:.2f}%"
    return 'v3 (min) → v3b (max), success / question rate / wrong destinations / joint coverage / coverage p05: ' + values(previous) + ' → ' + values(current) + '.'


def markdown(result):
    c, h = result['counts'], result['headline']
    lines = ['# Delivery task grounding pilot v3b · 20 Oct 2026', '',
             'Offline preparation for task execution in simulation; typed input, rule-based grammar. Template commands are not real language.', '',
             f"Seed {result['meta']['seed']}; {c['total']} labelled commands ({c['unique_texts']} distinct texts); "
             f"60/40 split = {c['calibration']}/{c['test']}; alpha 0.10; {result['meta']['repeats']} repeated random splits.", '',
             f"Maximum-slot threshold q̂ = {h['q_hat']:.12g}. Simultaneous inclusion uses max(1 − p(true)); no guarantee for real input is claimed.", '',
             '| Policy | Success | Any question | Clarification | Confirmation | Wrong destinations | All-slot coverage |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for key, label in [('baseline', 'Argmax, no clarification'), ('per_slot', 'Per-slot + confirmation')]:
        values = h[key]
        lines.append('| '+label+' | '+' | '.join(f'{100*values[k]:.2f}%' for k in
                     ('success', 'question_rate', 'clarification_rate', 'confirmation_rate', 'wrong_destinations', 'joint_coverage'))+' |')
    lines += ['', f"Wrong confirmation proposals: {100*h['per_slot']['wrong_confirmation_proposals']:.2f}%; "
              f"mean questions/command: {h['per_slot']['mean_questions']:.3f}.", '',
              'The answerer is an oracle that selects its labelled intent if offered and declines incorrect confirmations. '
              'Post-confirmation wrong destinations therefore measure this policy under that assumption. They are not a human-error rate.', '',
              f"| {result['meta']['repeats']} splits | Mean success | Mean questions | Mean wrong destinations | Mean all-slot coverage | Coverage p05 |",
              '|---|---:|---:|---:|---:|---:|']
    for key in ('baseline', 'per_slot'):
        r = result['repeated_splits'][key]
        lines.append('| '+key+' | '+' | '.join(f"{100*r[k]['mean']:.2f}%" for k in
                     ('success', 'question_rate', 'wrong_destinations', 'joint_coverage'))+f" | {100*r['joint_coverage']['p05']:.2f}% |")
    lines += ['', '## Calibration caveat', '',
              'For slot errors (0, 0.9), min = 0 despite one badly scored slot. A cutoff on that minimum does not bound every slot error. '
              'Production calibration now uses max(1 − p(true)), so a cutoff bounds every scored slot error.', '',
              f"Max-score cross-check: q̂ {h['max_score_q_hat_diagnostic']:.12g}; headline joint coverage "
              f"{100*h['max_score_joint_coverage_diagnostic']:.2f}%; repeated mean "
              f"{100*result['max_score_diagnostic']['mean']:.2f}%.", '',
              result.get('comparison_v3', ''), '', '## Limits', '', *['- '+v for v in result['limitations']], '',
              'Saved locally: dataset, split IDs, headline decisions and all repeated-split summaries are saved beside this report. '
              'Not checked: live navigation, actual acknowledgements or delivery, real language, the physical robot or other hardware.', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'config/calibration/proposed_system/tasks')
    parser.add_argument('--repeats', type=int, default=1000)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error('repeats must be positive')
    result, rows, split, decisions, repeated = run(Tags(), repeats=args.repeats)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in [('results', result), ('dataset', rows), ('split', split),
                        ('decisions_headline', decisions), ('repeated_splits', repeated)]:
        with (args.out/(name+'.json')).open('x') as f:
            json.dump(value, f, indent=2, allow_nan=False)
            f.write('\n')
    (args.out/'results.md').write_text(markdown(result))
    print(json.dumps({'counts': result['counts'], 'headline': result['headline']}, indent=2))


if __name__ == '__main__':
    main()
