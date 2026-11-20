#!/usr/bin/env python3
"""Run independent owned-server execution experiments sequentially."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    base=(ROOT/args.output).resolve()
    if not base.is_relative_to(ROOT/'records/task3') or base.exists(): raise ValueError('Use a new task3 directory')
    base.mkdir(parents=True)
    ids=['STARTER-en-01','STARTER-en-07','STARTER-MIXED-ta-01','STARTER-MIXED-ml-06']
    for contestant in ('A','B'):
        for variant,parallel,dedup in [('sequential_full',1,False),('sequential_dedup',1,True),('parallel_dedup',4,True)]:
            destination=base/(contestant+'_'+variant)
            args=[sys.executable,str(ROOT/'scripts/measure_office.py'),'--contestant',contestant,
                  '--output',str(destination.relative_to(ROOT)),'--parallel',str(parallel),'--ids',*ids]
            if not dedup: args.append('--no-dedup')
            result=subprocess.run(args,cwd=ROOT,check=False)
            if result.returncode: raise SystemExit(result.returncode)


if __name__=='__main__': main()
