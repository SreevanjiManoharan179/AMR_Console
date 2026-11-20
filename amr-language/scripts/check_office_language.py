#!/usr/bin/env python3
"""Small office-language check on authored STARTER rows; no robot or owner ratings."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
import time
from zoneinfo import ZoneInfo

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language import understand,hear_reply
from amr_language.calibration import file_sha256
from amr_language.office import AGREEMENT_NOTICE
from amr_language.office_schema import normalized_plan,walk_steps
from amr_language.grounding import fingerprint
from bench_utils import ResourceMonitor,percentile,wilson


def shape(steps):
    return [(s['action'],bool(s.get('on_answer'))) for s in walk_steps(steps)]


def targets(steps):
    values=[]
    for step in walk_steps(steps):
        row={key:step[key] for key in ('to','from','at') if key in step}
        if step.get('if_not_there','').startswith('leave_with:'):
            row['alternate']=step['if_not_there'].split(':',1)[1]
        values.append(row)
    return values


def run(args):
    directory=(ROOT/args.output).resolve()
    if not directory.is_relative_to(ROOT/'records/office_checks') or directory.exists():
        raise ValueError('Use a new output folder under records/office_checks')
    names=yaml.safe_load((ROOT/'data/office_names_example.yaml').read_text())
    settings=json.loads((ROOT/f'data/office_model_{args.contestant}.json').read_text())
    settings['timeout_s']=args.timeout
    with (ROOT/'data/owner_check.csv').open(newline='') as stream:
        rows=[row for row in csv.DictReader(stream) if row['text'].strip() and row['speaker']=='STARTER']
    if args.limit: rows=rows[:args.limit]
    labels={row['id']:row for row in json.loads((ROOT/'data/owner_labels_proposed.json').read_text())['rows']}
    directory.mkdir(parents=True)
    metadata={'contestant':args.contestant,'source':'STARTER','notice':AGREEMENT_NOTICE,
              'settings':settings,'names_sha256':file_sha256(ROOT/'data/office_names_example.yaml'),
              'commands_sha256':file_sha256(ROOT/'data/owner_check.csv'),
              'replies_sha256':file_sha256(ROOT/'data/replies_check.csv'),
              'labels_sha256':file_sha256(ROOT/'data/owner_labels_proposed.json'),
              'labels_confirmed':False,'real_owner_rows_collected':0,
              'code_sha256':fingerprint({str(p.relative_to(ROOT)):file_sha256(p) for p in sorted((ROOT/'amr_language').glob('*')) if p.is_file()}),
              'console_navigation':'not established; standalone small check, not a benchmark',
              'time':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat()}
    (directory/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    monitor=ResourceMonitor(directory/'resources.jsonl').start()
    records=[]
    try:
        with (directory/'requests.jsonl').open('w') as stream:
            for index,row in enumerate(rows):
                trace=[]
                result=understand(row['text'],names,'Bharathwaj',row['language'],{**settings,'trace':trace})
                expected=labels[row['id']]
                expected_steps=normalized_plan({'steps':expected['expected_steps'],'issue':None},'Bharathwaj')['steps'] if expected['expected_steps'] else []
                issue=expected['expected_issue']
                failure=result.get('refuse') or {}
                question=result.get('ask') or {}
                if issue=='unknown_name':
                    decision_ok=result['decision']=='ask' and question.get('event') in ('unknown_name','ask_which')
                elif issue=='too_many_steps':
                    decision_ok=result['decision']=='ask' and question.get('event')=='split_job'
                elif issue:
                    decision_ok=result['decision']=='refuse' and failure.get('reason')==issue
                else:
                    decision_ok=result['decision']=='act'
                step_ok=(shape(result['steps'])==shape(expected_steps) and result['decision']=='act') if not issue else decision_ok
                names_ok=(targets(result['steps'])==targets(expected_steps) and result['decision']=='act') if not issue else decision_ok
                record={'id':row['id'],'source':'STARTER','contestant':args.contestant,'language':row['language'],
                        'text':row['text'],'expected_english':row['expected_english'],
                        'expected_steps_notes':row['expected_steps_notes'],'proposed_labels':expected,
                        'result':result,'trace':trace,'meaning_owner_rating':None,
                        'proposed_steps_match':step_ok,'proposed_names_match':names_ok,
                        'long_request':sum(1 for _ in walk_steps(expected_steps))>=3 or issue=='too_many_steps',
                        'owner_long_success':None}
                records.append(record)
                stream.write(json.dumps(record,ensure_ascii=False)+'\n'); stream.flush()
                print(json.dumps({'completed':index+1,'total':len(rows),'language':row['language'],
                                  'decision':result['decision'],'latency_ms':result['latency_ms']}),flush=True)
    finally:
        resources=monitor.stop()
    replies=[]
    with (ROOT/'data/replies_check.csv').open(newline='') as stream:
        for row in csv.DictReader(stream):
            started=time.monotonic()
            result=hear_reply(row['text'],set(row['expect'].split('|')),row['language'])
            replies.append({'id':row['id'],'source':'STARTER','language':row['language'],'expected_kind':row['expected_kind'],
                            'result':result,'correct':result['kind']==row['expected_kind'],
                            'latency_ms':(time.monotonic()-started)*1000,'method':'word list, not model-specific'})
    (directory/'replies.json').write_text(json.dumps(replies,ensure_ascii=False,indent=2)+'\n')
    summary={'contestant':args.contestant,'source':'STARTER','meaning_correct':'not rated by owner',
             'provisional':True,'real_owner_commands':0,'resources':resources,'by_language':{}}
    for language in ('en','ta','ml','te'):
        group=[r for r in records if r['language']==language]
        reply_group=[r for r in replies if r['language']==language]
        times=[r['result']['latency_ms'] for r in group]
        failures=[{'id':r['id'],'reason':(r['result']['refuse'] or r['result']['ask'] or {}).get('reason') or
                   (r['result']['refuse'] or r['result']['ask'] or {}).get('event') or 'proposed steps/names differ'}
                  for r in group if not r['proposed_steps_match'] or not r['proposed_names_match']][:5]
        summary['by_language'][language]={'requests':len(group),'meaning_owner_rated':wilson(0,0),
          'proposed_steps_match':wilson(sum(r['proposed_steps_match'] for r in group),len(group)),
          'proposed_names_match':wilson(sum(r['proposed_names_match'] for r in group),len(group)),
          'replies_fixture_match':wilson(sum(r['correct'] for r in reply_group),len(reply_group)),
          'long_request_success_owner_rated':wilson(0,0),'p50_ms':statistics.median(times) if times else None,
          'p95_ms':percentile(times,.95),'reply_p50_ms':statistics.median(r['latency_ms'] for r in reply_group),
          'reply_p95_ms':percentile([r['latency_ms'] for r in reply_group],.95),
          'decisions':dict(Counter(r['result']['decision'] for r in group)),'observed_failures_up_to_5':failures}
    (directory/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    with (directory/'owner_results_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['id','contestant','language','english_rewrite','meaning_yes_no','steps_yes_no','names_yes_no','long_success_yes_no','notes'])
        for record in records:
            writer.writerow([record['id'],args.contestant,record['language'],record['result']['english'],'','','','',''])
    print('Small check complete:',directory/'summary.json',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contestant',choices=['A','B'],default='A')
    parser.add_argument('--timeout',type=float,default=5.)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--output',required=True)
    run(parser.parse_args())
