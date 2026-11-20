#!/usr/bin/env python3
"""Measure actual generated-token count for a supported six-step capacity probe."""
import json
from pathlib import Path
import sys

import yaml
from runner_control import ROOT,Runner
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import prepare_office,make_backend,settings_for_office
from amr_language.office_names import OfficeNames
from amr_language.office_schema import validate_plan


def main():
    directory=ROOT/'records/task3/plan_capacity';directory.mkdir(exist_ok=False)
    english='Give the file to Divya; give the folder to Priya; give the parcel to Ravi; give the document to Arun; give the charger to Meera; give the letter to Neha.'
    names=yaml.safe_load((ROOT/'data/office_names_example.yaml').read_text());roster=OfficeNames(names,'Bharathwaj')
    records=[]
    for contestant in ('A','B'):
        settings=settings_for_office(json.loads((ROOT/f'data/office_model_{contestant}.json').read_text()))
        startup=prepare_office(settings);backend=make_backend(settings)
        with Runner(directory/contestant) as runner:
            runner.load(settings['model'])
            if not runner.ensure_live(): raise RuntimeError('No live runner')
            start=len(backend.stats)
            try:
                plan=backend.plan(english,roster,0,60,settings);validate_plan(plan)
                record={'contestant':contestant,'english':english,'valid':True,'plan':plan,'call':backend.stats[start:]}
            except (ValueError,RuntimeError) as error:
                record={'contestant':contestant,'english':english,'valid':False,'error':str(error),'call':backend.stats[start:]}
            record['post_liveness']=runner.probe();records.append(record)
    (directory/'results.json').write_text(json.dumps(records,indent=2)+'\n')
    print(json.dumps(records),flush=True)


if __name__=='__main__': main()
