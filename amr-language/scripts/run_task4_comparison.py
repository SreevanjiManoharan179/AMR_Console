#!/usr/bin/env python3
"""Run all four A/B glossary conditions with independent owned local servers."""
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def main():
    for contestant in ('A','B'):
        for glossary in ('off','on'):
            output=f'records/task4/{contestant}_glossary_{glossary}'
            result=subprocess.run([sys.executable,str(ROOT/'scripts/measure_task4.py'),'--contestant',contestant,
                '--glossary',glossary,'--output',output],cwd=ROOT,check=False)
            if result.returncode:raise SystemExit(result.returncode)


if __name__=='__main__':main()
