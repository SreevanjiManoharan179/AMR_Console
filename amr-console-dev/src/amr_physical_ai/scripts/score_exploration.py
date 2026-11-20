#!/usr/bin/env python3
"""Score autonomous exploration runs against the exact map (the exploration benchmark). Offline: no ROS.

One run:    python3 src/amr_physical_ai/scripts/score_exploration.py <run folder>/exploration [--out FILE]
Benchmark:  python3 src/amr_physical_ai/scripts/score_exploration.py --benchmark <run folders...> --out DIR

A run folder is what the explorer node (or the offline robot) writes: events.jsonl, maps/NNNN.npz,
truth.jsonl and summary.json. The exact map is src/amr_physical_ai/maps/<world>.yaml for the run's world
(--world overrides it). Run folders are only read; scores go to --out (a benchmark writes results.json
and RESULTS.md there). Definitions are those of the plan's benchmark table:

  explored ratio    exact free cells the SLAM map knows (scripts/exploration_metrics.py), per map message
  T90, T99          time from the explorer's start until the ratio first reaches 90% and 99%
  distance to 99%   metres of Gazebo truth path driven by then
  completion        the ratio of the last map before the explorer stopped
  coverage AUC      mean ratio over the first 6 minutes
  area per metre    explored floor (m2) per metre driven
  walls, faces, corners, floor IoU   the last map against the exact map, within 0.10 m
  final pose error  SLAM pose minus truth at the last truth sample
  safety            collisions (truth path closer than the robot radius to an exact obstacle), Nav2
                    recoveries (spin, backup, ... goals) and aborted goals
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import sys

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exploration_metrics as em  # noqa: E402
import office_world  # noqa: E402

MAPS = Path(__file__).resolve().parents[1] / 'maps'
ROBOT_RADIUS_M = .22            # nav2_mppi.yaml robot_radius
BASELINE = 'nearest'            # the baseline every method is compared with
METHOD_LABELS = {'ours': 'ours', 'gain': 'gain', 'nearest': 'nearest'}
# The benchmark design's targets. "all": every run; "vs nearest": the method's mean against nearest's mean.
TARGETS = [
    # key, label, rule, target, stretch
    ('completion', 'Completion', 'min_all', .99, None),
    ('completion_mean', 'Completion, mean', 'min_mean', None, .995),
    ('distance_to_99_m', 'Distance to 99%, vs nearest', 'less_than_b0', .22, .30),
    ('T99_s', 'T99, vs nearest', 'less_than_b0', .25, .30),
    ('coverage_auc_6min', 'Coverage over time, vs nearest', 'more_than_b0', .20, .33),
    ('wall_agreement', 'Wall agreement', 'min_all', .98, .995),
    ('faces_seen', 'Faces seen', 'min_all', .95, .98),
    ('corners_seen', 'Corners seen', 'min_all', .95, 1.),
    ('floor_iou', 'Floor IoU, vs nearest', 'not_below_b0', 0., 1e-9),
    ('final_pose_error_m', 'Final pose error', 'max_all', .10, .05),
    ('collisions', 'Collisions', 'max_all', 0, 0),
    ('recoveries', 'Nav2 recoveries per run', 'max_all', 1, 0),
]


def read_jsonl(path):
    try:
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    except OSError:
        return []


def exact_for(world):
    path = MAPS / f'{world}.yaml'
    if not path.is_file():
        raise FileNotFoundError(f'No exact map for world {world!r} ({path}); pass --world.')
    occupied, free, resolution, origin = office_world.read_map(path)
    return occupied, free, resolution, (origin[0], origin[1])


def snapshots(folder):
    """(stamp, path) of every saved map, in time order."""
    found = []
    for path in sorted((Path(folder) / 'maps').glob('*.npz')):
        with np.load(path) as data:
            found.append((float(data['stamp']), path))
    return sorted(found)


def load_snapshot(path):
    with np.load(path) as data:
        return (np.asarray(data['data']), float(data['resolution']), tuple(float(v) for v in data['origin']))


def path_until(samples):
    """Cumulative truth path length at each sample (samples sorted by time)."""
    out, total = [], 0.
    for i, sample in enumerate(samples):
        if i:
            total += math.dist((samples[i - 1]['x'], samples[i - 1]['y']), (sample['x'], sample['y']))
        out.append(total)
    return out


def distance_at(times, cumulative, t):
    """Path driven up to time t (the last truth sample at or before t)."""
    index = int(np.searchsorted(times, t, side='right')) - 1
    return cumulative[index] if index >= 0 else 0.


def clearance_field(e_occ, e_res):
    """Metres from each cell centre to the nearest exact obstacle surface."""
    return ndimage.distance_transform_edt(~e_occ) * e_res - e_res / 2


def episodes(flags):
    """Number of separate runs of True."""
    flags = np.asarray(flags, dtype=bool)
    return int(np.count_nonzero(flags[1:] & ~flags[:-1]) + (1 if flags.size and flags[0] else 0))


def score_run(folder, exact=None, world=None):
    folder = Path(folder)
    events = read_jsonl(folder / 'events.jsonl')
    try:
        summary = json.loads((folder / 'summary.json').read_text())
    except (OSError, ValueError):
        summary = {}
    start = next((e for e in events if e.get('event') == 'start'), None)
    if start is None:
        raise ValueError(f'{folder}: the explorer never started (no start event).')
    finish = next((e for e in reversed(events) if e.get('event') == 'finish'), None)
    world = world or summary.get('world')
    if exact is None:
        exact = exact_for(world)
    e_occ, e_free, e_res, e_origin = exact
    t0 = float(start['t'])
    maps = snapshots(folder)
    if not maps:
        raise ValueError(f'{folder}: no saved maps.')
    t_end = float(finish['t']) if finish else maps[-1][0]
    window = [(stamp, path) for stamp, path in maps if t0 < stamp <= t_end + 1e-9]
    before = [(stamp, path) for stamp, path in maps if stamp <= t0]
    first = before[-1] if before else maps[0]

    truth = sorted((s for s in read_jsonl(folder / 'truth.jsonl')
                    if s.get('x') is not None and t0 - 1e-9 <= s['t'] <= t_end + 1e-9), key=lambda s: s['t'])
    times = np.array([s['t'] for s in truth])
    cumulative = path_until(truth)
    has_truth = len(truth) >= 2

    def ratio(path):
        data, res, origin = load_snapshot(path)
        values = em.on_exact(data, res, origin, e_free.shape, e_res, e_origin)
        return em.explored_ratio(values, e_free)

    series = [(0., ratio(first[1]), 0.)]
    for stamp, path in window:
        series.append((stamp - t0, ratio(path), distance_at(times, cumulative, stamp) if has_truth else None))
    final_stamp, final_path = (window or [first])[-1]
    data, res, origin = load_snapshot(final_path)
    quality = em.score_map(data, res, origin, e_occ, e_free, e_res, e_origin)
    timing = {key: (round(value, 2) if isinstance(value, float) and key != 'coverage_auc_6min' else value)
              for key, value in em.score_series(series).items()}
    if not has_truth:
        timing['distance_to_90_m'] = timing['distance_to_99_m'] = None
    distance = cumulative[-1] if has_truth else None
    area = quality['explored_ratio'] * np.count_nonzero(e_free) * e_res * e_res

    errors = [math.dist((s['x'], s['y']), s['slam'][:2]) for s in truth if s.get('slam')]
    last_pose = next((s for s in reversed(truth) if s.get('slam')), None)
    field = clearance_field(e_occ, e_res)
    if has_truth:
        clear = office_world._sample(field, e_res, e_origin, np.array([s['x'] for s in truth]),
                                     np.array([s['y'] for s in truth]))
        close = clear < ROBOT_RADIUS_M
    else:
        clear, close = np.array([]), np.array([], dtype=bool)
    results = Counter(e['outcome'] for e in events if e.get('event') == 'result')
    recoveries = [e for e in events if e.get('event') == 'recovery' and t0 <= e['t'] <= t_end + 1e-9]
    decisions = [e for e in events if e.get('event') == 'decision' and e.get('kind') in ('frontier', 'home')]
    compute = sorted(e['compute_s'] for e in events if e.get('event') == 'decision' and 'compute_s' in e)
    corners = quality['corners']
    return {
        'folder': str(folder), 'method': start.get('method') or summary.get('method'), 'world': world,
        'end': finish.get('end') if finish else 'no finish event', 'elapsed_s': round(t_end - t0, 2),
        'settings_sha256': summary.get('settings_sha256'), 'sources': summary.get('sources'),
        **timing,
        'completion': quality['explored_ratio'],
        'distance_m': None if distance is None else round(distance, 2),
        'area_per_metre_m2': round(area / distance, 3) if distance else None,
        'wall_agreement': quality['wall_agreement'], 'faces_seen': quality['faces_seen'],
        'corners_seen': corners['ratio'], 'corners': corners, 'floor_iou': quality['floor_iou'],
        'final_pose_error_m': round(math.dist((last_pose['x'], last_pose['y']), last_pose['slam'][:2]), 3)
        if last_pose else None,
        'pose_error_rms_m': round(math.sqrt(sum(e * e for e in errors) / len(errors)), 3) if errors else None,
        'pose_error_max_m': round(max(errors), 3) if errors else None,
        'collisions': episodes(close) if has_truth else None,
        'min_clearance_m': round(float(clear.min()), 3) if clear.size else None,
        'recoveries': len(recoveries), 'recovery_kinds': dict(Counter(e.get('behavior') for e in recoveries)),
        'goals': len(decisions), 'results': dict(results), 'aborted_goals': results.get('ABORTED', 0),
        'decision_compute_s': {'median': compute[len(compute) // 2] if compute else None,
                               'max': compute[-1] if compute else None},
        'maps_scored': len(window) + 1, 'truth_samples': len(truth),
        'series': [[round(t, 2), round(r, 5), None if d is None else round(d, 3)] for t, r, d in series],
    }


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {'n': len(values), 'mean': statistics.fmean(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values)}


METRICS = ('completion', 'T90_s', 'T99_s', 'distance_to_99_m', 'coverage_auc_6min', 'area_per_metre_m2',
           'wall_agreement', 'faces_seen', 'corners_seen', 'floor_iou', 'final_pose_error_m', 'collisions',
           'recoveries', 'aborted_goals', 'goals', 'distance_m', 'elapsed_s')


def aggregate(scores):
    """Per-method statistics, ours and gain against nearest, and the plan's targets for ours."""
    by_method = {}
    for score in scores:
        by_method.setdefault(score['method'], []).append(score)
    methods = {}
    for method, runs in by_method.items():
        methods[method] = {'runs': len(runs), 'reached_99': sum(r['T99_s'] is not None for r in runs),
                           'ends': dict(Counter(r['end'] for r in runs)),
                           'settings_sha256': sorted({r['settings_sha256'] for r in runs if r['settings_sha256']}),
                           **{m: _stats([r[m] for r in runs]) for m in METRICS}}
    base = methods.get(BASELINE)
    versus = {}
    for method, stats in methods.items():
        if method == BASELINE or base is None:
            continue
        versus[method] = {}
        for metric, sense in (('distance_to_99_m', 'less'), ('T99_s', 'less'), ('coverage_auc_6min', 'more'),
                              ('floor_iou', 'diff')):
            ours, b0 = stats.get(metric), base.get(metric)
            # Only when every run of both methods has the value: a run that never reached 99% has no
            # T99, and averaging the others would compare different sets of runs.
            if (not ours or not b0 or not b0['mean'] or ours['n'] < stats['runs'] or b0['n'] < base['runs']):
                versus[method][metric] = None
            elif sense == 'less':
                versus[method][metric] = 1 - ours['mean'] / b0['mean']
            elif sense == 'more':
                versus[method][metric] = ours['mean'] / b0['mean'] - 1
            else:
                versus[method][metric] = ours['mean'] - b0['mean']
    return {'methods': methods, 'versus_nearest': versus, 'targets': targets(methods, versus)}


def targets(methods, versus, method='ours'):
    """Each target for one method: its value, and whether it meets the target and the stretch."""
    stats, rel = methods.get(method), versus.get(method, {})
    out = []
    for key, label, rule, target, stretch in TARGETS:
        if stats is None:
            break
        metric = 'completion' if key == 'completion_mean' else key
        s = stats.get(metric)
        if rule in ('less_than_b0', 'more_than_b0', 'not_below_b0'):
            value = rel.get(metric)             # None: no nearest runs, or not every run of both methods has it
            met = False if value is None else value >= target - 1e-12
            better = False if value is None else value >= stretch - 1e-12
        elif s is None or s['n'] < stats['runs']:
            value, met, better = (s['min'] if rule == 'min_all' else s['max']) if s else None, False, False
        else:
            value = {'min_all': s['min'], 'max_all': s['max'], 'min_mean': s['mean']}[rule]
            ok = (lambda v, lim: v >= lim - 1e-12) if rule.startswith('min') else (lambda v, lim: v <= lim + 1e-12)
            met = None if target is None else ok(value, target)
            better = None if stretch is None else ok(value, stretch)
        out.append({'key': key, 'label': label, 'rule': rule, 'value': value, 'target': target,
                    'stretch': stretch, 'met': met, 'stretch_met': better})
    return out


def _fmt(value, kind=''):
    if value is None:
        return '—'
    if kind == '%':
        return f'{100 * value:.1f}%'
    if kind == '%2':
        return f'{100 * value:.2f}%'
    if kind == 'pts':
        return f'{100 * value:+.2f} pts'
    if kind == 's':
        return f'{value:.0f} s'
    if kind == 'm':
        return f'{value:.2f} m'
    return f'{value:.3g}' if isinstance(value, float) else str(value)


def markdown(result, world):
    order = [m for m in ('ours', 'gain', 'nearest') if m in result['methods']]
    rows = [('Runs reaching 99%', lambda s: f"{s['reached_99']} of {s['runs']}"),
            ('Completion, min', lambda s: _fmt(s['completion'] and s['completion']['min'], '%2')),
            ('T90, mean', lambda s: _fmt(s['T90_s'] and s['T90_s']['mean'], 's')),
            ('T99, mean', lambda s: _fmt(s['T99_s'] and s['T99_s']['mean'], 's')),
            ('Distance to 99%, mean', lambda s: _fmt(s['distance_to_99_m'] and s['distance_to_99_m']['mean'], 'm')),
            ('Coverage AUC 6 min, mean', lambda s: _fmt(s['coverage_auc_6min'] and s['coverage_auc_6min']['mean'], '%')),
            ('Area per metre, mean', lambda s: _fmt(s['area_per_metre_m2'] and s['area_per_metre_m2']['mean'])),
            ('Wall agreement, min', lambda s: _fmt(s['wall_agreement'] and s['wall_agreement']['min'], '%')),
            ('Faces seen, min', lambda s: _fmt(s['faces_seen'] and s['faces_seen']['min'], '%')),
            ('Corners seen, min', lambda s: _fmt(s['corners_seen'] and s['corners_seen']['min'], '%')),
            ('Floor IoU, mean', lambda s: _fmt(s['floor_iou'] and s['floor_iou']['mean'], '%2')),
            ('Final pose error, max', lambda s: _fmt(s['final_pose_error_m'] and s['final_pose_error_m']['max'], 'm')),
            ('Collisions, total', lambda s: _fmt(s['collisions'] and round(s['collisions']['mean'] * s['collisions']['n']))),
            ('Recoveries, max per run', lambda s: _fmt(s['recoveries'] and s['recoveries']['max'])),
            ('Goals, mean', lambda s: _fmt(s['goals'] and s['goals']['mean']))]
    lines = [f'# Exploration benchmark — {world}', '',
             'TurtleBot3 Waffle simulation. Computed by `score_exploration.py` from the run '
             'folders listed in results.json; every claim stays Not checked until you mark it.', '',
             '| Metric | ' + ' | '.join(METHOD_LABELS.get(m, m) for m in order) + ' |',
             '| --- | ' + ' | '.join('---' for _ in order) + ' |']
    for label, cell in rows:
        lines.append(f'| {label} | ' + ' | '.join(cell(result['methods'][m]) for m in order) + ' |')
    lines += ['', '## Targets for ours', '',
              'All-run targets use the worst run; "vs nearest" targets compare the method means.', '',
              '| Target | Value | Target | Met | Stretch | Met |', '| --- | --- | --- | --- | --- | --- |']
    for t in result['targets']:
        kind = '%' if t['rule'] in ('less_than_b0', 'more_than_b0') or t['key'] in (
            'wall_agreement', 'faces_seen', 'corners_seen') else (
            'm' if t['key'] == 'final_pose_error_m' else '')
        if t['key'] in ('completion', 'completion_mean'):
            kind = '%2'
        if t['key'] == 'floor_iou':
            kind = 'pts'
        yes = {True: 'yes', False: 'no', None: '—'}
        lines.append(f"| {t['label']} | {_fmt(t['value'], kind)} | {_fmt(t['target'], kind)} | {yes[t['met']]} | "
                     f"{_fmt(t['stretch'], kind)} | {yes[t['stretch_met']]} |")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('runs', nargs='+', type=Path, help='exploration run folders')
    parser.add_argument('--world', help='exact map to score against (default: the run\'s world)')
    parser.add_argument('--benchmark', action='store_true', help='aggregate the runs by method')
    parser.add_argument('--out', type=Path, help='output file (one run) or folder (benchmark)')
    args = parser.parse_args(argv)
    scores = [score_run(run, world=args.world) for run in args.runs]
    if not args.benchmark:
        text = json.dumps(scores[0] if len(scores) == 1 else scores, indent=2) + '\n'
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        else:
            sys.stdout.write(text)
        return 0
    worlds = sorted({s['world'] for s in scores})
    if len(worlds) != 1:
        raise SystemExit(f'A benchmark scores one world; these runs are in {worlds}.')
    result = {'world': worlds[0], 'runs': [{k: v for k, v in s.items() if k != 'series'} for s in scores],
              **aggregate(scores)}
    if not args.out:
        sys.stdout.write(json.dumps(result, indent=2) + '\n')
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    (args.out / 'series.json').write_text(json.dumps({s['folder']: s['series'] for s in scores}) + '\n')
    (args.out / 'RESULTS.md').write_text(markdown(result, worlds[0]))
    print(f'Scored {len(scores)} runs in {worlds[0]}: {args.out / "RESULTS.md"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
