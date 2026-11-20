#!/usr/bin/env python3
"""Complete the fixed-planner temperature comparison in fresh owned-server runs."""
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parent.parent
for temperature,name in (('0.7','B_t07_complete_2026-11-03'),('0.3','B_t03_complete_2026-11-03')):
    output=ROOT/'records/task4b'/name
    if output.exists():raise SystemExit('Refusing to overwrite '+str(output))
    result=subprocess.run([sys.executable,str(ROOT/'scripts/measure_task4b.py'),'--contestant','B',
        '--glossary','off','--temperature',temperature,'--output',str(output)],cwd=ROOT,check=False)
    if result.returncode:raise SystemExit(result.returncode)
