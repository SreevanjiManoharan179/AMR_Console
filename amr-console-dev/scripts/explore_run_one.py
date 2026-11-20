"""One-run autonomous exploration driver using the existing console worker, without an HTTP server/token."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import queue
import re
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/amr_console'))
import registry
import runtime
from engine import private_rviz_copy
from client import WorkerClient

parser = argparse.ArgumentParser()
parser.add_argument('--method', choices=['nearest', 'gain', 'ours'], required=True)
parser.add_argument('--world', choices=list(registry.EXPLORE_WORLDS), default=registry.EXPLORE_WORLDS[0])
args = parser.parse_args()
run_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_map_explore_' + uuid.uuid4().hex[:8]
env = runtime.choose_transport('mapping')
env['GZ_PARTITION'] = 'explore_' + run_id
action = registry.find_action(registry.actions(ROOT, runtime.environment(ROOT), ROOT / 'tools/amr_console/approvals.yaml'), 'map.explore')
assert not action['locked'], action
params = registry.validate(action, vars(args))
spec = registry.build(action, params, run_id, env, {'root': str(ROOT)})
spec['root'] = str(ROOT)
log = Path(spec['log_dir'])
out = Path(spec['explore']['out'])
log.mkdir(parents=True, exist_ok=False)
Path(spec['output_dir']).mkdir(parents=True, exist_ok=False)
private_rviz_copy(spec, ROOT, spec['rviz_config'])
events = queue.Queue()
client = WorkerClient(spec, events.put)
print(json.dumps({'run_id': run_id, 'command': spec['command'], 'env': env, 'out': str(out)}), flush=True)
client.start()
failure = None
summary = None
stopped = False
started = last_report = time.monotonic()
fatal = re.compile(r'process has died|Traceback \(most recent call last\)|Explorer failed:|Not ready after|Map handling failed:|Truth recording failed:|corrupted double|terminate called|Segmentation fault', re.I)
try:
    while not client.finished.is_set():
        while not events.empty():
            event = events.get()
            text = event.get('text', '')
            if event.get('type') in ('ready', 'alarm', 'finished'):
                print(json.dumps(event), flush=True)
            if not stopped and (event.get('type') == 'alarm' or fatal.search(text)):
                failure = text
        path = out / 'events.jsonl'
        if path.exists() and not failure:
            for line in path.read_text().splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('outcome') in ('ABORTED', 'TIMEOUT', 'REJECTED'):
                    failure = 'First unsuccessful goal: ' + json.dumps(event)
                    break
        path = out / 'summary.json'
        if path.exists():
            try:
                summary = json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        if time.monotonic() - started > 7200 and not failure:
            failure = 'Wall-clock watchdog exceeded 7200 s'
        if (failure or summary is not None) and not stopped:
            print(json.dumps({'stopping': failure or 'explorer summary complete', 'summary': summary}), flush=True)
            client.control('stop')
            stopped = True
        if time.monotonic() - last_report >= 30:
            path = out / 'progress.json'
            print(path.read_text() if path.exists() else 'Waiting for explorer records', flush=True)
            last_report = time.monotonic()
        time.sleep(.25)
finally:
    if not client.finished.is_set():
        client.stop()
cleanup = client.cleanup
if not cleanup or not cleanup.get('clean') or cleanup.get('forced') or cleanup.get('survivors') or not cleanup.get('audit', {}).get('clean'):
    failure = failure or 'Shutdown was not strictly clean; see cleanup.json'
if summary is None:
    path = out / 'summary.json'
    summary = json.loads(path.read_text()) if path.exists() else None
if not summary or summary.get('error') or not summary.get('final_map', {}).get('saved') or not summary.get('truth_samples'):
    failure = failure or 'Explorer summary/map/truth incomplete'
result = {'run_id': run_id, 'method': args.method, 'world': args.world, 'failure': failure,
          'summary': summary, 'cleanup': cleanup, 'exploration': str(out), 'log': str(log)}
(log / 's14_run_result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'result': str(log / 's14_run_result.json'), 'failure': failure}), flush=True)
sys.exit(1 if failure else 0)
