#!/usr/bin/env python3
"""Run tests without a pipe and retain exact evidence inside the module."""
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='records/checks')
    args=parser.parse_args()
    checks=(ROOT/args.output).resolve()
    if not checks.is_relative_to(ROOT/'records'): raise ValueError('Verification output must stay under module records')
    checks.mkdir(parents=True,exist_ok=True)
    original_checks=ROOT/'records/checks'
    result=subprocess.run([sys.executable,'-m','pytest','-q'],cwd=ROOT,capture_output=True,text=True,check=False)
    (checks/'pytest_final.txt').write_text(result.stdout+result.stderr)
    print(result.stdout,end='')
    if result.stderr:
        print(result.stderr,file=sys.stderr,end='')
    reference={}
    for line in (original_checks/'reference.sha256').read_text().splitlines():
        digest,relative=line.split('  ',1)
        reference[relative]=hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()==digest
    baseline=json.loads((original_checks/'console_sources_before.json').read_text())
    # The 27 Oct console copy this check compares against moved to ~/AMR_Console_archive on 6 Nov 2026.
    places=(ROOT.parent/'amr-console',ROOT.parent.parent/'AMR_Console_archive'/'amr-console')
    console=next((p for p in places if p.is_dir()),places[0])
    changed=[relative for relative,digest in baseline.items()
             if not (console/relative).is_file() or hashlib.sha256((console/relative).read_bytes()).hexdigest()!=digest]
    sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
             for directory in ('amr_language','scripts','tests','data') for p in (ROOT/directory).rglob('*')
             if p.is_file() and '__pycache__' not in p.parts}
    (checks/'module_files.sha256.json').write_text(json.dumps(sources,indent=2,sort_keys=True)+'\n')
    benchmark=ROOT/'records/benchmarks/synthetic_standalone/summary.json'
    benchmark_status=('completed: synthetic standalone only' if benchmark.exists() else
                      'in progress: synthetic standalone only' if benchmark.parent.exists() else 'not run')
    summary={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
             'test_command':[sys.executable,'-m','pytest','-q'],'test_exit_code':result.returncode,
             'reference_unchanged':reference,'console_sources_checked':len(baseline),
             'console_sources_changed':changed,'module_manifest':str((checks/'module_files.sha256.json').relative_to(ROOT)),
             'model_installation':'approved A and B verified' if (ROOT/'records/office_checks/model_B_inventory.json').exists() else 'see inventories',
             'model_benchmark':benchmark_status,'shared_load_benchmark':'not run',
             'real_command_calibration':'not collected','speech_benchmark':'not run',
             'office_v3_checks':{key:('completed: STARTER only, labels pending' if (ROOT/f'records/office_checks/{key}_v3_final/summary.json').exists() else 'not complete') for key in ('A','B')},
             'office_labels':'pending by explicit owner decision; no confirmed freeze',
             'task3_checks':{key:('completed: liveness-gated; starter labels pending' if (ROOT/f'records/task3/final_{key}/summary.json').exists() else 'not complete') for key in ('A','B')},
             'task4_checks':{key:('completed: liveness-gated, scripted map facts; labels pending' if (ROOT/f'records/task4/{key}/summary.json').exists() else 'not complete') for key in ('A_glossary_off','A_glossary_on','B_glossary_off','B_glossary_on')},
             'C':('completed: fixed-planner CPU/GPU and B-loaded/unloaded C campaign; authored labels pending' if (ROOT/'records/task4_C_2026-11-03/completion.json').exists() else 'installed: pinned inventory/source review and offline CPU/GPU greeting smoke passed; full office benchmark pending' if (ROOT/'records/task4/C_install/completion.json').exists() else 'not loaded or measured; waiting for owner installation-done message before inventory/source inspection'),
             'commit':'identity supplied; commit allowed only after verification exits zero; see git log'}
    (checks/'verification.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    return result.returncode or int(not all(reference.values()) or bool(changed))


if __name__=='__main__':
    raise SystemExit(main())
