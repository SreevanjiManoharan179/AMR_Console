"""Exploration benchmark: the three exploration methods on one world, a fresh launch per run, unattended.

    python3 scripts/explore_batch.py --runs 5 --label my_benchmark --no-rviz

Each run goes through the console's own worker (the same launch, ground-truth recording, STOP
and cleanup audit as "Explore automatically"), with three differences for a benchmark:
  * a goal that Nav2 aborts, times out or refuses does NOT stop the run: the explorer handles
    it and the scorer counts it;
  * one SLAM Toolbox settings file is passed to every run (default: the installed defaults)
    and its SHA-256 is recorded;
  * methods are interleaved by round (nearest, ours, gain, nearest, ...), so no method gets all
    the late runs.
An unexplained fault (process died, traceback, alarm, unclean shutdown) stops the whole batch;
known harmless shutdown aborts are judged and counted by the cleanup audit.

Writes data/benchmarks/<label>/manifest.json (updated after every run) and batch.log, then prints
the scoring command (src/amr_physical_ai/scripts/score_exploration.py --benchmark).
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import queue
import re
import shlex
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/amr_console'))

METHODS = ('nearest', 'ours', 'gain')
FATAL = re.compile(r'process has died|Traceback \(most recent call last\)|Explorer failed:|Not ready after|'
                   r'Map handling failed:|Truth recording failed:|corrupted double|terminate called|'
                   r'Segmentation fault', re.I)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def one_run(method, world, slam_params, headless, wall_limit_s, log, no_rviz=False):
    import registry
    import runtime
    from engine import private_rviz_copy
    from client import WorkerClient

    run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_map_explore_' + uuid.uuid4().hex[:8]
    env = runtime.choose_transport('mapping')
    env['GZ_PARTITION'] = 'bench_' + run_id
    action = registry.find_action(registry.actions(ROOT, runtime.environment(ROOT),
                                                   ROOT / 'tools/amr_console/approvals.yaml'), 'map.explore')
    if action['locked']:
        raise RuntimeError(f'map.explore is locked: {action}')
    params = registry.validate(action, {'method': method, 'world': world})
    spec = registry.build(action, params, run_id, env, {'root': str(ROOT)})
    spec['root'] = str(ROOT)
    spec['argv'] = list(spec['argv']) + [f'slam_params_file:={slam_params}']
    if headless:
        spec['argv'].append('headless:=true')
    if no_rviz:  # display only: RViz subscribes and draws, it never commands the robot
        spec['argv'].append('use_rviz:=false')
        spec['no_rviz'] = True  # tells the console worker there is no RViz view to save or close
    spec['command'] = shlex.join(spec['argv'])
    log_dir, out = Path(spec['log_dir']), Path(spec['explore']['out'])
    log_dir.mkdir(parents=True, exist_ok=False)
    Path(spec['output_dir']).mkdir(parents=True, exist_ok=False)
    private_rviz_copy(spec, ROOT, spec['rviz_config'])
    events = queue.Queue()
    client = WorkerClient(spec, events.put)
    log(json.dumps({'run_id': run_id, 'method': method, 'command': spec['command']}))
    client.start()
    failure, summary, stopped = None, None, False
    started = last_report = time.monotonic()
    ready_at = summary_at = stopped_at = None
    slam_sha_run = sha256(slam_params)
    try:
        while not client.finished.is_set():
            while not events.empty():
                event = events.get()
                text = event.get('text', '')
                if event.get('type') == 'ready' and ready_at is None:
                    ready_at = time.monotonic()
                alarm = event.get('type') == 'alarm' or event.get('level') == 'alarm'
                if event.get('type') in ('ready', 'alarm', 'finished') or alarm:
                    log(json.dumps(event)[:400])
                if not stopped and (alarm or FATAL.search(text)):
                    failure = text or json.dumps(event)[:400]
            if summary is None:
                summary = read_json(out / 'summary.json')
                if summary is not None:
                    summary_at = time.monotonic()
            if time.monotonic() - started > wall_limit_s and not failure and summary is None:
                failure = f'Wall-clock watchdog exceeded {wall_limit_s:.0f} s'
            if (failure or summary is not None) and not stopped:
                log(json.dumps({'stopping': failure or 'explorer summary complete'}))
                stopped, stopped_at = True, time.monotonic()
                try:
                    client.control('stop')
                except Exception as exc:  # the worker may already have exited after an alarm
                    log(f'  stop request not delivered ({exc!r}); waiting for the worker to finish')
            if stopped and time.monotonic() - stopped_at > 240:
                failure = failure or 'Worker did not finish within 240 s of STOP'
                log('  ' + failure)
                break
            if time.monotonic() - last_report >= 60:
                progress = read_json(out / 'progress.json')
                log(f'  {method} {run_id}: ' + (json.dumps(progress)[:300] if progress else 'waiting for explorer records'))
                last_report = time.monotonic()
            time.sleep(.25)
    finally:
        if not client.finished.is_set():
            try:
                client.stop()
            except Exception as exc:
                failure = failure or f'Forced worker stop failed: {exc!r}'
    # Exploration wall time: from readiness to the explorer's summary (shutdown time excluded).
    wall_s = (summary_at or time.monotonic()) - (ready_at or started)
    cleanup = client.cleanup or {}
    # cleanup['clean'] = the audit's continue_allowed (crash-free, or only the narrowly waived
    # Nav2 bond and bridge shutdown aborts) and nothing forced. A waived abort is counted below, never hidden.
    if not cleanup.get('clean') or cleanup.get('forced') or cleanup.get('survivors'):
        failure = failure or 'Shutdown was not clean (unwaived fault, forced step or survivors); see cleanup.json'
    summary = summary or read_json(out / 'summary.json')
    if not summary or summary.get('error') or not summary.get('final_map', {}).get('saved') \
            or not summary.get('truth_samples'):
        failure = failure or 'Explorer summary/map/truth incomplete'
    sim_s = (summary or {}).get('elapsed_s')
    result = {'run_id': run_id, 'method': method, 'world': world, 'failure': failure,
              'exploration': str(out), 'log': str(log_dir), 'end': (summary or {}).get('end'),
              'sim_s': sim_s, 'wall_s': round(wall_s, 1),
              'rtf': round(sim_s / wall_s, 3) if sim_s and wall_s > 0 and summary_at else None,
              'slam_params_sha256': slam_sha_run,
              'cleanup': {k: cleanup.get(k) for k in ('clean', 'forced', 'survivors', 'bond_abort_count')},
              'crash_free': bool(cleanup.get('audit', {}).get('clean')),
              'bond_abort': bool(cleanup.get('audit', {}).get('bond_abort')),
              'truth_bridge_abort': bool(cleanup.get('audit', {}).get('truth_bridge_abort'))}
    (log_dir / 'benchmark_run_result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    import registry
    parser.add_argument('--world', default=registry.EXPLORE_WORLDS[0], choices=list(registry.EXPLORE_WORLDS))
    parser.add_argument('--methods', default='nearest,ours,gain',
                        help='comma-separated, run in this order within each round')
    parser.add_argument('--runs', type=int, required=True, help='runs per method')
    parser.add_argument('--slam-params', type=Path,
                        default=Path('/opt/ros/jazzy/share/slam_toolbox/config/mapper_params_online_async.yaml'),
                        help='SLAM Toolbox settings file (default: the installed defaults)')
    parser.add_argument('--label', required=True, help='folder name under data/benchmarks/')
    parser.add_argument('--headless', action='store_true', help='Gazebo without its window (RViz stays)')
    parser.add_argument('--no-rviz', action='store_true',
                        help='no RViz window (display only; avoids the RViz crash on window close)')
    parser.add_argument('--wall-limit', type=float, default=5400., help='seconds of wall time per run')
    parser.add_argument('--continue-on-failure', action='store_true',
                        help='keep going after a failed run (default: stop the batch, per the project rules)')
    args = parser.parse_args(argv)
    methods = [m.strip() for m in args.methods.split(',') if m.strip()]
    bad = [m for m in methods if m not in METHODS]
    if bad or args.runs < 1:
        parser.error(f'unknown methods {bad} or runs < 1')
    slam_params = args.slam_params.resolve()
    if not slam_params.is_file():
        parser.error(f'{slam_params} does not exist')
    folder = ROOT / 'data/benchmarks' / args.label
    folder.mkdir(parents=True, exist_ok=False)
    logfile = (folder / 'batch.log').open('a')

    def log(text):
        line = f'{datetime.now():%H:%M:%S} {text}'
        print(line, flush=True)
        logfile.write(line + '\n')
        logfile.flush()

    sources = {p: sha256(ROOT / p) for p in (
        'src/amr_physical_ai/amr_physical_ai/exploration.py',
        'src/amr_physical_ai/amr_physical_ai/exploration_run.py',
        'src/amr_physical_ai/amr_physical_ai/explore_node.py',
        'src/amr_physical_ai/config/nav2_mppi.yaml',
        'src/amr_physical_ai/launch/tb3_mapping.launch.py',
        'scripts/explore_batch.py')}
    manifest = {'label': args.label, 'world': args.world, 'methods': methods, 'runs_per_method': args.runs,
                'slam_params_file': str(slam_params), 'slam_params_sha256': sha256(slam_params),
                'headless': args.headless, 'no_rviz': args.no_rviz, 'started': datetime.now().isoformat(timespec='seconds'),
                'source_sha256': sources, 'runs': [], 'stopped_early': None}

    def save():
        (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

    save()
    log(f'Batch {args.label}: {args.runs} x {methods} on {args.world}; SLAM settings {slam_params.name} '
        f'sha256 {manifest["slam_params_sha256"][:16]}')
    for round_ in range(1, args.runs + 1):
        for method in methods:
            try:
                result = one_run(method, args.world, slam_params, args.headless, args.wall_limit, log,
                                 no_rviz=args.no_rviz)
            except Exception as exc:  # setup error before or around the launch
                result = {'method': method, 'failure': f'driver error: {exc!r}'}
            result['round'] = round_
            if result.get('slam_params_sha256') and result['slam_params_sha256'] != manifest['slam_params_sha256']:
                result['failure'] = result.get('failure') or 'SLAM settings file changed during the batch'
            manifest['runs'].append(result)
            save()
            log(f'== round {round_} {method}: ' + ('OK' if not result.get('failure') else 'FAILED: ' + str(result['failure'])[:300])
                + f" | end: {result.get('end')} | rtf {result.get('rtf')}")
            if result.get('failure') and not args.continue_on_failure:
                manifest['stopped_early'] = f'round {round_} {method}: {result["failure"]}'
                manifest['finished'] = datetime.now().isoformat(timespec='seconds')
                save()
                log('STOPPED: a run failed. Nothing after it was run. See batch.log and the run\'s cleanup.json.')
                return 1
    manifest['finished'] = datetime.now().isoformat(timespec='seconds')
    save()
    ok = [r['exploration'] for r in manifest['runs'] if not r.get('failure')]
    log(f'Batch complete: {len(ok)} of {len(manifest["runs"])} runs finished cleanly.')
    log('Score with: python3 src/amr_physical_ai/scripts/score_exploration.py --benchmark --out '
        f'{folder.relative_to(ROOT)}/score ' + ' '.join(str(Path(p).relative_to(ROOT)) for p in ok))
    return 0


if __name__ == '__main__':
    sys.exit(main())
