#!/usr/bin/env python3
"""Task4 liveness-gated hybrid comparison with/without the fixed office glossary."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

import yaml
from runner_control import ROOT,Runner
from bench_utils import ResourceMonitor,percentile,wilson
from measure_office import measure_one
from report_office_check import score
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import prepare_office,settings_for_office,make_backend
from amr_language.office_schema import validate_plan
from amr_language.calibration import file_sha256


def summarize(rows,resources):
    result={'resources':resources,'groups':{},'missing':sum(r['status']!='measured' for r in rows),
            'restarts':sum(r['runner_restarts'] for r in rows),'notice':'Two agreeing grammar-led draws are weaker than four; uncalibrated; yes remains mandatory.'}
    for source in sorted({r['source'] for r in rows}):
        for language in ('en','ta','ml','te'):
            group=[r for r in rows if r['source']==source and r['language']==language]
            if not group:continue
            measured=[r for r in group if r['status']=='measured'];times=[r['request_wall_ms'] for r in measured]
            pairs=[p for r in measured for p in r['trace'] if 'index' in p and p.get('distinct_pair') and p.get('planner') in ('grammar','model')]
            valid=0
            for pair in pairs:
                try:validate_plan(pair.get('raw_plan'));valid+=1
                except (ValueError,TypeError):pass
            result['groups'][source+':'+language]={'rows':len(group),'measured':len(measured),'missing':len(group)-len(measured),
              'p50_ms':statistics.median(times) if times else None,'p95_ms':percentile(times,.95),
              'within_5s':wilson(sum(r['within_5s'] for r in measured),len(measured)),
              'draws':dict(Counter(r['result']['draws'] for r in measured)),
              'grammar_pairs':sum(p['planner']=='grammar' for p in pairs),'model_pairs':sum(p['planner']=='model' for p in pairs),
              'grammar_only_requests':sum(r['result']['planner']=='grammar' for r in measured),
              'schema_valid':wilson(valid,len(pairs)),
              'empty_null_plans':sum(p.get('raw_plan',{}).get('steps')==[] and p.get('raw_plan',{}).get('issue') is None for p in pairs),
              'target_match':wilson(sum(r.get('proposed_comparison',{}).get('proposed_targets_match',False) for r in measured),sum('proposed_comparison' in r for r in measured)),
              'decisions':dict(Counter(r['result']['decision'] for r in measured)),'restarts':sum(r['runner_restarts'] for r in group),
              'meaning_owner_rating':None}
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--contestant',choices=['A','B'],required=True)
    parser.add_argument('--glossary',choices=['off','on'],required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--limit',type=int,default=0);args=parser.parse_args()
    directory=(ROOT/args.output).resolve()
    if not directory.is_relative_to(ROOT/'records/task4') or directory.exists():raise ValueError('Use a new task4 output folder')
    directory.mkdir(parents=True)
    settings=settings_for_office(json.loads((ROOT/f'data/office_model_{args.contestant}.json').read_text()))
    settings.update(mode='measurement',planner_mode='hybrid',draw_policy='adaptive',office_meanings=args.glossary=='on',input_source='STARTER_MEASUREMENT')
    startup=prepare_office(settings);backend=make_backend(settings)
    names=yaml.safe_load((ROOT/'data/task4_names_example.yaml').read_text())
    with (ROOT/'data/owner_check.csv').open(newline='') as f:rows=[r for r in csv.DictReader(f) if r['text'].strip()]
    rows=[r for r in rows if not r['speaker'].startswith('STARTER')]+[r for r in rows if r['speaker'].startswith('STARTER')]
    if args.limit:rows=rows[:args.limit]
    labels={r['id']:r for r in json.loads((ROOT/'data/owner_labels_proposed.json').read_text())['rows']}
    metadata={'date':datetime.now().astimezone().isoformat(),'contestant':args.contestant,'glossary':args.glossary,'settings':settings,
      'startup':startup,'real_owner_rows':sum(not r['speaker'].startswith('STARTER') for r in rows),
      'map_facts':'Explicit scripted map/reachability/location fixture, not Gazebo route verification',
      'provisional':'Two agreeing draws are weaker than four; agreement is uncalibrated; explicit readback yes mandatory.',
      'source_sha256':{str(p.relative_to(ROOT)):file_sha256(p) for p in sorted((ROOT/'amr_language').glob('*')) if p.is_file()},
      'data_sha256':{str(p.relative_to(ROOT)):file_sha256(p) for p in (ROOT/'data/owner_check.csv',ROOT/'data/owner_labels_proposed.json',ROOT/'data/task4_names_example.yaml',ROOT/'data/office_meanings.json',ROOT/'data/grammar_holdout.csv')}}
    (directory/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    monitor=ResourceMonitor(directory/'resources.jsonl').start();records=[]
    try:
        with Runner(directory) as runner:
            runner.load(settings['model']);(directory/'allocation.json').write_text(json.dumps(runner.allocation(),indent=2)+'\n')
            with (directory/'requests.jsonl').open('w') as out:
                for index,row in enumerate(rows):
                    record=measure_one(row,names,settings,runner,backend)
                    if row['id'] in labels and record['status']=='measured':record['proposed_comparison']=score({'id':row['id'],'language':row['language'],'proposed_labels':labels[row['id']],'result':record['result']})
                    record['owner_meaning_rating']=None;records.append(record)
                    out.write(json.dumps(record,ensure_ascii=False)+'\n');out.flush()
                    print(json.dumps({'contestant':args.contestant,'glossary':args.glossary,'completed':index+1,'total':len(rows),
                        'id':row['id'],'status':record['status'],'ms':record['request_wall_ms'],'draws':(record['result'] or {}).get('draws')}),flush=True)
    finally:resources=monitor.stop()
    (directory/'summary.json').write_text(json.dumps(summarize(records,resources),indent=2)+'\n')
    print('Complete:',directory,flush=True)


if __name__=='__main__':main()
