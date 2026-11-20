#!/usr/bin/env python3
"""Measure actual public API and reference grammar on SYNTHETIC development text.

No calibration, ROS, dispatch, console launch or installation. Records original
model forms by wrapping sample() for observation only, then restores the method.
The synthetic roster marker is omitted only in this diagnostic process; no file
is altered and calibration_file is forbidden, so this benchmark cannot act.
"""
import argparse
from collections import Counter,defaultdict
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
import time
import types
from zoneinfo import ZoneInfo

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.core import understand
from amr_language.backends import OllamaBackend,PROMPT_VERSION
from amr_language.calibration import file_sha256
from amr_language.core import _settings,source_span
from amr_language.grounding import Names,fingerprint
from amr_language.schema import validate_task_form
from bench_utils import ResourceMonitor,percentile,wilson


@contextmanager
def observe_forms(records):
    original=OllamaBackend.sample
    def observed(self,*args,**kwargs):
        started=time.monotonic()
        try:
            form=original(self,*args,**kwargs)
            records.append({'form':form,'latency_ms':(time.monotonic()-started)*1000})
            return form
        except Exception as error:
            records.append({'error_type':type(error).__name__,'latency_ms':(time.monotonic()-started)*1000})
            raise
    OllamaBackend.sample=observed
    try:
        yield
    finally:
        OllamaBackend.sample=original


def action_signature(form):
    return ('out_of_scope',) if form['out_of_scope'] else tuple(step['action'] for step in form['steps'])


def grounded(form,names):
    if form['out_of_scope']:
        return [{} for _ in form['steps']]
    return [{slot:list(names.match(slot,step[slot])) for slot in ('place','person','object') if step[slot] is not None}
            for step in form['steps']]


def assess(samples,row,names,api):
    valid=[]
    for sample in samples:
        if 'form' not in sample:
            continue
        try:
            validate_task_form(sample['form'])
            if not all(step[slot] is None or source_span(step[slot],row['text'])
                       for step in sample['form']['steps'] for slot in ('place','person','object','item')):
                continue
            valid.append(sample['form'])
        except (ValueError,TypeError,KeyError):
            continue
    expected=row['expected_task_form']
    prefiltered=not samples and api['refusal'] and api['refusal']['template']=='out_of_scope'
    if not valid:
        return {'sampled':bool(samples),'valid_samples':0,'sample_attempts':len(samples),
                'all_sampled_forms_valid':False,'action_or_scope_correct':bool(prefiltered and expected['out_of_scope']),
                'grounding_correct':bool(prefiltered and expected['out_of_scope']),
                'prefiltered':bool(prefiltered)}
    signatures=Counter(action_signature(form) for form in valid)
    mode=signatures.most_common(1)[0][0]
    # A deterministic tie break uses sample order; report ties rather than hiding them.
    tied=sum(count==signatures.most_common(1)[0][1] for count in signatures.values())>1
    predictions=Counter(json.dumps(grounded(form,names),sort_keys=True,ensure_ascii=False) for form in valid)
    modal_grounding=json.loads(predictions.most_common(1)[0][0])
    action_ok=mode==action_signature(expected)
    grounding_ok=modal_grounding==row['expected_ids']
    all_valid=len(valid)==len(samples)
    return {'sampled':True,'valid_samples':len(valid),'sample_attempts':len(samples),
            'all_sampled_forms_valid':all_valid,'action_or_scope_correct':action_ok and all_valid,
            'grounding_correct':grounding_ok and all_valid and action_ok,
            'modal_action_signature':mode,'modal_grounding':modal_grounding,
            'action_vote_tie':tied,'prefiltered':False}


def grammar_baseline(names):
    path=ROOT/'reference/current_grammar.py'
    module=types.ModuleType('amr_reference_grammar_benchmark')
    sys.modules[module.__name__]=module
    exec(compile(path.read_text(),str(path),'exec'),module.__dict__)
    tags=types.SimpleNamespace(**{group:rows for group,rows in names.groups.items()})
    return module.Grammar(tags)


def evaluate_grammar(grammar,row,names):
    started=time.monotonic()
    frame=grammar.parse(row['text'])
    expected=row['expected_task_form']
    destinations=[{}]
    if frame.error:
        action_ok=expected['out_of_scope']
        grounding_ok=expected['out_of_scope']
    else:
        action_ok=not expected['out_of_scope'] and [frame.action]==[step['action'] for step in expected['steps']]
        if frame.room:
            destinations[0]['place']=list(names.match('place',frame.room))
        if frame.target_kind in ('person','role','object'):
            slot='object' if frame.target_kind=='object' else 'person'
            destinations[0][slot]=list(names.match(slot,frame.target))
        elif frame.target_kind=='seat':
            rooms=names.match('place',frame.room) if frame.room else None
            seats=[key for key,item in names.groups['seats'].items()
                   if (rooms is None or item['room'] in rooms)
                   and (frame.row is None or item['row']==frame.row)
                   and (frame.col is None or item['col']==frame.col)]
            destinations[0]['place']=sorted(seats) or ['<none>']
        grounding_ok=action_ok and destinations==row['expected_ids']
    return {'parse_rejected':bool(frame.error),'action_or_scope_correct':bool(action_ok),
            'grounding_correct':bool(grounding_ok),'grounding':destinations,
            'latency_ms':(time.monotonic()-started)*1000}


def summarize(records):
    output={}
    for language in ('en','ta','ml','te'):
        rows=[r for r in records if r['language']==language]
        sampled=[r for r in rows if r['assessment']['sampled']]
        latencies=[r['api']['latency_ms'] for r in rows]
        modes=Counter(r['api']['decision'] for r in rows)
        refusals=Counter((r['api']['refusal'] or {}).get('template') for r in rows)
        output[language]={'requests':len(rows),
            'sampled_request_validity':wilson(sum(r['assessment']['all_sampled_forms_valid'] for r in sampled),len(sampled)),
            'model_sample_validity':wilson(sum(r['assessment']['valid_samples'] for r in rows),sum(r['assessment']['sample_attempts'] for r in rows)),
            'action_or_scope_correct':wilson(sum(r['assessment']['action_or_scope_correct'] for r in rows),len(rows)),
            'correct_grounding_with_correct_action':wilson(sum(r['assessment']['grounding_correct'] for r in rows),len(rows)),
            'decisions':{decision:wilson(modes[decision],len(rows)) for decision in ('act','ask','refuse')},
            'refusal_templates':dict(refusals),'latency_ms':{'median':statistics.median(latencies) if latencies else None,
            'p95':percentile(latencies,.95),'scope':'full public API; diagnostic timeout, no real calibration'},
            'wrong_accepted_destination_rate':wilson(0,0),'coverage_at_90_percent':None,
            'baseline':{'action_or_scope_correct':wilson(sum(r['baseline']['action_or_scope_correct'] for r in rows),len(rows)),
                        'correct_grounding_with_correct_action':wilson(sum(r['baseline']['grounding_correct'] for r in rows),len(rows)),
                        'parse_rejected':wilson(sum(r['baseline']['parse_rejected'] for r in rows),len(rows)),
                        'decision_rates':None,'coverage':None}}
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout-s',type=float,default=120.)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--output',default='records/benchmarks/synthetic_standalone')
    args=parser.parse_args()
    directory=(ROOT/args.output).resolve()
    if not directory.is_relative_to(ROOT) or directory.exists():
        parser.error('Use a new output folder inside the module')
    settings=json.loads((ROOT/'data/installed_model_settings.json').read_text())
    settings['timeout_s']=args.timeout_s
    if settings.get('calibration_file'):
        parser.error('Synthetic diagnostics must not use calibration')
    config=_settings(settings)
    backend=OllamaBackend(config['model'],file_sha256(ROOT/config['model_file']))
    backend.load_manifest(ROOT/config['model_manifest_file'])
    backend.verify_identity(config,5.)
    data=ROOT/'data/dev_synthetic.jsonl'
    rows=[json.loads(line) for line in data.read_text().splitlines()]
    if args.limit:
        # Stratify the optional smoke subset across languages.
        selected=[]
        for index in range(max(Counter(row['language'] for row in rows).values())):
            for language in ('en','ta','ml','te'):
                group=[row for row in rows if row['language']==language]
                if index<len(group): selected.append(group[index])
        rows=selected[:args.limit]
    raw_names=yaml.safe_load((ROOT/'data/dev_names.yaml').read_text())
    assert raw_names.pop('fixture_source')=='SYNTHETIC'
    names=Names(raw_names)
    grammar=grammar_baseline(names)
    directory.mkdir(parents=True)
    metadata={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'source':'SYNTHETIC',
              'purpose':'development diagnostics, not real-world accuracy or calibrated acceptance',
              'settings':settings,'dataset_sha256':file_sha256(data),'names_file_sha256':file_sha256(ROOT/'data/dev_names.yaml'),
              'passed_names_sha256':fingerprint(raw_names),'roster_marker_handling':'removed in this diagnostic process only; calibration forbidden',
              'registry_manifest_sha256':backend.registry_manifest_sha256,'local_manifest_sha256':backend.manifest_sha256,
              'model_file_sha256':backend.file_sha256,'prompt_version':PROMPT_VERSION,
              'package_source_sha256':fingerprint({p.name:file_sha256(p) for p in sorted((ROOT/'amr_language').glob('*.py'))}),
              'benchmark_script_sha256':file_sha256(Path(__file__)),'console_navigation':'not established; standalone run',
              'baseline':'unchanged reference/current_grammar.py parse + approved-alias grounding; no conformal threshold',
              'latency_target_s':2.,'configured_timeout_s':args.timeout_s,'requested_samples':settings['samples'],
              'validity_denominator':'sampled requests; prefiltered requests separately counted; all produced forms must pass schema+source validation',
              'accuracy_definition':'strict all-sampled-forms validity required, then modal action sequence/scope and modal grounded sequence; includes extra/missing targets',
              'coverage':'not computed without real speaker-disjoint calibration'}
    (directory/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    monitor=ResourceMonitor(directory/'resources.jsonl').start()
    records=[]
    started=time.monotonic()
    try:
        with (directory/'requests.jsonl').open('w') as output:
            for index,row in enumerate(rows):
                samples=[]
                with observe_forms(samples):
                    result=understand(row['text'],raw_names,language=row['language'],settings=settings)
                assert result['decision']!='act','Uncalibrated synthetic benchmark must never act'
                record={'id':row['id'],'source':'SYNTHETIC','language':row['language'],'text':row['text'],
                        'expected_task_form':row['expected_task_form'],'expected_ids':row['expected_ids'],
                        'category':row['category'],'api':result,'model_samples':samples,
                        'assessment':assess(samples,row,names,result),
                        'baseline':evaluate_grammar(grammar,row,names)}
                output.write(json.dumps(record,ensure_ascii=False)+'\n')
                output.flush()
                records.append(record)
                if index==0 or (index+1)%10==0 or index+1==len(rows):
                    print(json.dumps({'completed':index+1,'total':len(rows),'elapsed_s':round(time.monotonic()-started,1),
                                      'last_decision':result['decision'],'last_samples':result['samples']}),flush=True)
    finally:
        resources=monitor.stop()
    backend.verify_identity(config,5.)
    summary={'source':'SYNTHETIC','requests':len(records),'by_language':summarize(records),
             'resources':resources,'elapsed_s':time.monotonic()-started,
             'shared_load':'not run','real_command_coverage':'not measured'}
    (directory/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({'completed':len(records),'summary':str((directory/'summary.json').relative_to(ROOT)),
                      'resources':resources}),flush=True)


if __name__=='__main__':
    main()
