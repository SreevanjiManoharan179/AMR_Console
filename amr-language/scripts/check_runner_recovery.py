#!/usr/bin/env python3
"""Exercise automatic restart after an idle owned-server exit, without aborting inference."""
import json
import os
import signal
from pathlib import Path

from runner_control import ROOT,Runner
from bench_utils import ResourceMonitor


def main():
    directory=ROOT/'records/task3/recovery_control';directory.mkdir(exist_ok=False)
    rows=[]
    for contestant in ('A','B'):
        model=json.loads((ROOT/f'data/office_model_{contestant}.json').read_text())['model']
        with Runner(directory/contestant) as runner:
            monitor=ResourceMonitor(directory/contestant/'resources.jsonl').start()
            try:
                runner.load(model)
                if not runner.ensure_live(): raise RuntimeError('Initial liveness did not pass')
                runner.event('injected_idle_server_exit',note='No generation in flight; control test only')
                os.killpg(runner.process.pid,signal.SIGTERM)
                runner.process.wait(timeout=10)
                recovered=runner.ensure_live()
                row={'contestant':contestant,'recovered':recovered,'automatic_restarts':runner.restarts,
                     'allocation_after':runner.allocation(),'resources':monitor.stop()}
                if not recovered or runner.restarts!=1: raise RuntimeError('Automatic recovery failed')
                rows.append(row)
            finally:
                if monitor.thread and monitor.thread.is_alive(): monitor.stop()
        print('Automatic idle-server recovery verified:',contestant,flush=True)
    (directory/'results.json').write_text(json.dumps({'complete':True,'trials':rows,
        'scope':'Control fault only; excluded from model-quality restart counts'},indent=2)+'\n')


if __name__=='__main__': main()
