#!/usr/bin/env python3
"""Liveness-gated office measurement: 60 seconds per call, judge 5s afterwards."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
import time

import yaml
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language import understand
from amr_language.calibration import file_sha256
from amr_language.office_backend import prepare_office,settings_for_office,make_backend
from amr_language.office_schema import validate_plan
from bench_utils import ResourceMonitor,percentile,wilson
from runner_control import Runner
from report_office_check import score


def measure_one(row,names,settings,runner,backend):
    attempts=[];before_restarts=runner.restarts
    for attempt in range(2):
        pre_ok=runner.ensure_live()
        if not pre_ok:
            attempts.append({'attempt':attempt,'status':'not_measured','reason':'preflight generation failed'})
            break
        tick=time.monotonic();trace=[];stats_start=len(backend.stats)
        result=understand(row['text'],names,'Bharathwaj',row['language'],{**settings,'trace':trace})
        elapsed=(time.monotonic()-tick)*1000
        post=runner.probe()
        technical=(result.get('refuse') or {}).get('reason') in ('model_timeout','model_unavailable')
        ok=post['ok'] and not technical
        attempts.append({'attempt':attempt,'status':'measured' if ok else 'not_measured',
                         'result':result,'trace':trace,'calls':backend.stats[stats_start:],
                         'request_wall_ms':elapsed,'post_liveness':post,
                         'reason':None if ok else 'transport safety limit/error or failed post-generation liveness'})
        if ok: break
        # Every inference error gets a generation check; failed/stalled trials are never scored.
        runner.restart('request error or failed post-generation probe; rerun request once')
    chosen=attempts[-1]
    return {'id':row['id'],'source':row['speaker'],'language':row['language'],'text':row['text'],
            'intended_english':row['expected_english'],'notes':row.get('notes',''),
            'status':chosen['status'],'attempts':attempts,'runner_restarts':runner.restarts-before_restarts,
            'result':chosen.get('result'),'trace':chosen.get('trace',[]),'calls':chosen.get('calls',[]),
            'request_wall_ms':chosen.get('request_wall_ms') if chosen['status']=='measured' else None,
            'within_5s':chosen.get('request_wall_ms',float('inf'))<=5000 if chosen['status']=='measured' else None}


def summarize(records,resources):
    summary={'resources':resources,'groups':{},'not_measured':sum(r['status']!='measured' for r in records),
             'runner_restarts':sum(r['runner_restarts'] for r in records),'owner_ratings':'pending, not inferred'}
    for source in sorted({r['source'] for r in records}):
        for language in ('en','ta','ml','te'):
            group=[r for r in records if r['source']==source and r['language']==language]
            if not group: continue
            measured=[r for r in group if r['status']=='measured'];times=[r['request_wall_ms'] for r in measured]
            pairs=[p for r in measured for p in r['trace'] if 'index' in p and p.get('distinct_pair')]
            plan_calls=[c for r in measured for c in r['calls'] if c['stage']=='plan']
            raw=[p['raw_plan'] for p in pairs if 'raw_plan' in p];valid=0
            for plan in raw:
                try: validate_plan(plan);valid+=1
                except ValueError: pass
            metrics=[p for r in measured for p in r['trace'] if p.get('kind')=='pipeline_metrics']
            comparisons=[r['proposed_comparison'] for r in measured if r.get('proposed_comparison')]
            summary['groups'][source+':'+language]={'total':len(group),'measured':len(measured),
                'not_measured':len(group)-len(measured),'restarts':sum(r['runner_restarts'] for r in group),
                'p50_ms':statistics.median(times) if times else None,'p95_ms':percentile(times,.95),
                'within_5s':wilson(sum(r['within_5s'] for r in measured),len(measured)),
                'plan_validity':wilson(valid,len(plan_calls)),'parsed_plan_validity':wilson(valid,len(raw)),
                'plan_call_responses':len(plan_calls),
                'empty_null_issue_plans':sum(p.get('steps')==[] and p.get('issue') is None for p in raw),
                'distinct_plans':len(pairs),'raw_plan_responses':len(raw),
                'plans_reused':sum(p.get('plans_reused',0) for p in metrics),
                'rewrites':sum(p.get('rewrite_draws',0) for p in metrics),
                'proposed_targets_match':wilson(sum(p['proposed_targets_match'] for p in comparisons),len(comparisons)),
                'proposed_exact_plan_match':wilson(sum(p['proposed_exact_plan_match'] for p in comparisons),len(comparisons)),
                'decisions':dict(Counter(r['result']['decision'] for r in measured)),
                'rewrite_p50_ms':statistics.median(p['rewrite_wall_ms'] for p in metrics) if metrics else None,
                'plan_grounding_p50_ms':statistics.median(p['plan_and_grounding_wall_ms'] for p in metrics) if metrics else None}
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contestant',choices=['A','B'],required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--parallel',type=int,choices=[1,4],default=1)
    parser.add_argument('--no-dedup',action='store_true');parser.add_argument('--ids',nargs='*')
    parser.add_argument('--original-only',action='store_true');parser.add_argument('--plan-tokens',type=int,default=768)
    args=parser.parse_args();directory=(ROOT/args.output).resolve()
    if not directory.is_relative_to(ROOT/'records/task3') or directory.exists(): raise ValueError('Use new task3 directory')
    directory.mkdir(parents=True)
    settings=json.loads((ROOT/f'data/office_model_{args.contestant}.json').read_text())
    settings.update(mode='measurement',pipeline_parallel=args.parallel,deduplicate_plans=not args.no_dedup,plan_tokens=args.plan_tokens)
    startup=prepare_office(settings);backend=make_backend(settings_for_office(settings))
    names=yaml.safe_load((ROOT/'data/office_names_example.yaml').read_text())
    with (ROOT/'data/owner_check.csv').open(newline='') as stream: rows=[r for r in csv.DictReader(stream) if r['text'].strip()]
    if args.original_only: rows=[r for r in rows if r['speaker']=='STARTER']
    if args.ids: rows=[r for r in rows if r['id'] in args.ids]
    labels={r['id']:r for r in json.loads((ROOT/'data/owner_labels_proposed.json').read_text())['rows']}
    metadata={'date':datetime.now().astimezone().isoformat(),'contestant':args.contestant,'settings':settings,
      'startup':startup,'rows':len(rows),'real_owner_rows':sum(not r['speaker'].startswith('STARTER') for r in rows),
      'labels_confirmed':False,'timing':'startup hash, load and liveness excluded; whole request wall time included; 60s safety per model call',
      'source_sha256':{str(p.relative_to(ROOT)):file_sha256(p) for folder in ('amr_language','scripts') for p in sorted((ROOT/folder).glob('*.py'))},
      'data_sha256':{str(p.relative_to(ROOT)):file_sha256(p) for p in (ROOT/'data/owner_check.csv',ROOT/'data/owner_labels_proposed.json',ROOT/'data/office_names_example.yaml')}}
    (directory/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    monitor=ResourceMonitor(directory/'resources.jsonl').start();records=[]
    try:
        with Runner(directory,parallel=args.parallel,max_queue=8 if args.parallel==4 else 1) as runner:
            runner.load(settings['model'])
            (directory/'allocation.json').write_text(json.dumps(runner.allocation(),indent=2)+'\n')
            with (directory/'requests.jsonl').open('w') as stream:
                for index,row in enumerate(rows):
                    record=measure_one(row,names,settings,runner,backend)
                    if row['id'] in labels and record['status']=='measured':
                        old={'id':row['id'],'language':row['language'],'proposed_labels':labels[row['id']],'result':record['result']}
                        record['proposed_comparison']=score(old)
                    record['owner_meaning_rating']=None
                    records.append(record);stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
                    print(json.dumps({'completed':index+1,'total':len(rows),'id':row['id'],'status':record['status'],
                                      'ms':record['request_wall_ms'],'restarts':record['runner_restarts']}),flush=True)
    finally: resources=monitor.stop()
    summary=summarize(records,resources)
    (directory/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    with (directory/'rewrite_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['id','source','language','status','draw','intended_english','model_english','owner_meaning_yes_no','notes'])
        for row in records:
            for pair in row['trace']:
                if 'index' in pair:
                    writer.writerow([row['id'],row['source'],row['language'],row['status'],pair['index'],row['intended_english'],pair.get('rewrite_raw',pair.get('english','')),'',row['notes']])
    print('Completed liveness-gated measurement:',directory,flush=True)


if __name__=='__main__': main()
