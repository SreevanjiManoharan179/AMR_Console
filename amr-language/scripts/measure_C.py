#!/usr/bin/env python3
"""One frozen C campaign: CPU/GPU, B-unloaded translation and B-loaded pairing."""
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

import yaml
from runner_control import ROOT,Runner,request
from bench_utils import ResourceMonitor,percentile,wilson
from measure_task4b import summarize
from report_office_check import score
sys.path.insert(0,str(ROOT))
from amr_language import understand
from amr_language.backends import BackendError,BackendTimeout
from amr_language.office_backend import prepare_office,settings_for_office,make_backend,CrossSystemOfficeBackend
from amr_language.office_c_client import CWorker

BASE=ROOT/'records/task4_C_2026-11-03'


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def stamp():return datetime.now().astimezone().isoformat()


def start_worker(device,directory,events):
    worker=CWorker(device,directory)
    events.append({'event':'C_start','time':stamp(),'command':worker.command,'ready':worker.ready})
    write(directory/'C_processes.json',events)
    return worker


def record_base(row,chosen,attempts,restarts):
    return {'id':row['id'],'source':row['speaker'],'language':row['language'],'text':row['text'],
        'intended_english':row['expected_english'],'notes':row.get('notes',''),'status':chosen['status'],
        'attempts':attempts,'runner_restarts':restarts,'result':chosen.get('result'),
        'trace':chosen.get('trace',[]),'calls':chosen.get('calls',[]),'C_call':chosen.get('C_call'),
        'request_wall_ms':chosen.get('request_wall_ms') if chosen['status']=='measured' else None,
        'within_5s':chosen.get('request_wall_ms',float('inf'))<=5000 if chosen['status']=='measured' else None,
        'owner_meaning_rating':None}


def translation_summary(records,resources,events):
    result={'resources':resources,'groups':{},'missing':sum(r['status']!='measured' for r in records),
        'C_restarts':max(0,len(events)-1),'B_state':'unloaded; no B planner or B agreement in this translation-only phase'}
    for source in sorted({r['source'] for r in records}):
        for language in ('en','ta','ml','te'):
            group=[r for r in records if (r['source'],r['language'])==(source,language)]
            if not group:continue
            measured=[r for r in group if r['status']=='measured'];times=[r['request_wall_ms'] for r in measured]
            trans=[r['C_call'].get('translation_ms') for r in measured if r.get('C_call') and r['C_call'].get('translation_ms') is not None]
            result['groups'][source+':'+language]={'rows':len(group),'measured':len(measured),'missing':len(group)-len(measured),
                'p50_ms':statistics.median(times) if times else None,'p95_ms':percentile(times,.95),
                'translation_p50_ms':statistics.median(trans) if trans else None,'translation_p95_ms':percentile(trans,.95),
                'valid_translations':sum(bool(r.get('C_call',{}).get('ok')) for r in measured),
                'english_passthrough':language=='en','within_5s':wilson(sum(t<=5000 for t in times),len(times))}
    return result


def peak_rss(records,events):
    values=[e['ready']['startup_max_rss_kib'] for e in events]
    values += [a[key]['max_rss_kib'] for r in records for a in r['attempts'] for key in ('C_call','C_post','post','C_pre','pre') if isinstance(a.get(key),dict) and a[key].get('max_rss_kib')]
    return max(values) if values else None


def unloaded(device,rows,metadata):
    directory=BASE/(device+'_B_unloaded');directory.mkdir()
    try:request('/api/version',timeout=.3)
    except (OSError,RuntimeError):pass
    else:raise RuntimeError('B-unloaded profile requires a free owned-server port; no unowned service will be stopped')
    write(directory/'metadata.json',{**metadata,'device':device,'phase':'translation_only','B_state':'unloaded'})
    monitor=ResourceMonitor(directory/'resources.jsonl').start();worker=None;events=[];records=[]
    try:
        worker=start_worker(device,directory,events)
        with (directory/'requests.jsonl').open('w') as stream:
            for index,row in enumerate(rows):
                attempts=[];before=len(events)
                for attempt in range(2):
                    pre=worker.probe()
                    if not pre.get('ok'):
                        attempts.append({'attempt':attempt,'status':'not_measured','reason':'C pre-generation liveness failed','pre':pre})
                    else:
                        tick=time.monotonic();begin=len(worker.calls);error=None;technical=False
                        try:worker.translate(row['text'],row['language'],60.)
                        except ValueError as exc:error=str(exc) # capacity/truncation is a measured model failure
                        except (BackendError,BackendTimeout):technical=True;error='C transport/safety error'
                        wall=(time.monotonic()-tick)*1000;post=worker.probe()
                        call=worker.calls[begin] if len(worker.calls)>begin else {}
                        good=post.get('ok') and not technical
                        attempts.append({'attempt':attempt,'status':'measured' if good else 'not_measured',
                            'request_wall_ms':wall,'C_call':call,'translation_error':error,'post':post})
                    if attempts[-1]['status']=='measured':break
                    if attempt==0:
                        worker.close();worker=start_worker(device,directory,events)
                chosen=attempts[-1];record=record_base(row,chosen,attempts,len(events)-before)
                records.append(record);stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
                print(json.dumps({'phase':directory.name,'completed':index+1,'id':row['id'],'status':record['status']}),flush=True)
    finally:
        if worker:worker.close()
        resources=monitor.stop()
    summary=translation_summary(records,resources,events);summary['C_process_peak_rss_kib']=peak_rss(records,events)
    write(directory/'summary.json',summary)


def paired(device,rows,names,labels,settings,local_backend,metadata):
    directory=BASE/(device+'_B_loaded');directory.mkdir()
    write(directory/'metadata.json',{**metadata,'device':device,'phase':'live_C_translation_vs_live_B_greedy','B_state':'loaded'})
    monitor=ResourceMonitor(directory/'resources.jsonl').start();worker=None;events=[];records=[]
    try:
        with Runner(directory) as runner:
            runner.load(settings['model']) # B first, preserving its existing full-GPU allocation when C starts
            write(directory/'B_allocation_before_C.json',runner.allocation())
            worker=start_worker(device,directory,events)
            backend=CrossSystemOfficeBackend(local_backend,worker)
            write(directory/'B_allocation_with_C.json',runner.allocation())
            with (directory/'requests.jsonl').open('w') as stream:
                for index,row in enumerate(rows):
                    attempts=[];before=runner.restarts;C_before=len(events)
                    for attempt in range(2):
                        b_pre=runner.ensure_live();c_pre=worker.probe()
                        if not b_pre or not c_pre.get('ok'):
                            attempts.append({'attempt':attempt,'status':'not_measured','reason':'pre-generation liveness failed',
                                             'B_pre':b_pre,'C_pre':c_pre})
                        else:
                            tick=time.monotonic();trace=[];b_start=len(backend.stats);c_start=len(worker.calls)
                            result=understand(row['text'],names,'Bharathwaj',row['language'],
                                {**settings,'backend':backend,'draw_policy':'cross_system','trace':trace})
                            wall=(time.monotonic()-tick)*1000
                            b_post=runner.probe();c_post=worker.probe()
                            technical=(result.get('refuse') or {}).get('reason') in ('model_timeout','model_unavailable')
                            good=b_post['ok'] and c_post.get('ok') and not technical
                            attempts.append({'attempt':attempt,'status':'measured' if good else 'not_measured',
                                'result':result,'trace':trace,'calls':backend.stats[b_start:],
                                'C_call':worker.calls[c_start] if len(worker.calls)>c_start else {},
                                'request_wall_ms':wall,'B_post':b_post,'C_post':c_post})
                        if attempts[-1]['status']=='measured':break
                        if attempt==0:
                            worker.close();runner.restart('C/B liveness or transport failure; retry once')
                            worker=start_worker(device,directory,events);backend=CrossSystemOfficeBackend(local_backend,worker)
                    record=record_base(row,attempts[-1],attempts,runner.restarts-before)
                    record['C_restarts']=len(events)-C_before
                    if row['id'] in labels and record['status']=='measured':
                        record['proposed_comparison']=score({'id':row['id'],'language':row['language'],
                            'proposed_labels':labels[row['id']],'result':record['result']})
                    records.append(record);stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
                    print(json.dumps({'phase':directory.name,'completed':index+1,'id':row['id'],
                        'status':record['status'],'ms':record['request_wall_ms'],
                        'decision':(record['result'] or {}).get('decision')}),flush=True)
            write(directory/'B_allocation_end.json',runner.allocation())
            worker.close();worker=None
    finally:
        if worker:worker.close()
        resources=monitor.stop()
    summary=summarize(records,resources);summary['C_process_peak_rss_kib']=peak_rss(records,events)
    summary['C_restarts']=max(0,len(events)-1)
    for key,group in summary['groups'].items():
        source,language=key.split(':')
        calls=[r['C_call'] for r in records if (r['source'],r['language'])==(source,language) and r['status']=='measured' and r.get('C_call')]
        times=[c['translation_ms'] for c in calls if c.get('translation_ms') is not None]
        group.update(translation_p50_ms=statistics.median(times) if times else None,translation_p95_ms=percentile(times,.95),
            valid_translations=sum(bool(c.get('ok')) for c in calls),english_passthrough=language=='en')
    summary['notice']='One deterministic C translation plus one B greedy rewrite; exactly two sources; no majority. Uncalibrated and yes mandatory. English C side is passthrough.'
    write(directory/'summary.json',summary)


def main():
    if BASE.exists():raise SystemExit('Refusing to overwrite this C campaign')
    fixed=json.loads((ROOT/'records/task4b/evaluated_source_manifest.json').read_text())
    protected=('office_grammar.py','office_names.py','office_clarifications.py','office_schema.py','office_speech.py','office.py','office_map.py')
    for name in protected:
        key='amr_language/'+name
        if sha(ROOT/key)!=fixed[key]:raise SystemExit('Task 4b planner component changed: '+name)
    BASE.mkdir()
    settings=settings_for_office(json.loads((ROOT/'data/office_model_B.json').read_text()))
    settings.update(mode='measurement',planner_mode='hybrid',draw_policy='cross_system',office_meanings=False,input_source='STARTER_MEASUREMENT')
    # Prepare only B; C always initializes in its isolated child.
    startup=prepare_office(settings);local_backend=make_backend(settings)
    names=yaml.safe_load((ROOT/'data/task4_names_example.yaml').read_text())
    with (ROOT/'data/owner_check.csv').open(newline='') as stream:rows=[r for r in csv.DictReader(stream) if r['text'].strip()]
    rows=[r for r in rows if not r['speaker'].startswith('STARTER')]+[r for r in rows if r['speaker'].startswith('STARTER')]
    labels={r['id']:r for r in json.loads((ROOT/'data/owner_labels_proposed.json').read_text())['rows']}
    cache=ROOT/'.runtime/indictrans2-C/pip-cache'
    cache_before={'files':sum(p.is_file() for p in cache.rglob('*')),'bytes':sum(p.stat().st_size for p in cache.rglob('*') if p.is_file())}
    metadata={'date':stamp(),'task4b_commit':'5921fcb','settings':settings,'B_startup':startup,
        'C_process_command':[str(ROOT/'.runtime/indictrans2-C/venv/bin/python'),'-I',str(ROOT/'scripts/translation_worker_C.py')],
        'timing':'Startup/hash/load and generation liveness excluded. Translation-only phases profile B-unloaded memory; paired phases measure true C+B request wall time with B loaded. No five-second abort; 60-second per-call safety.',
        'agreement':'C deterministic beam5 translation versus one B greedy rewrite; exactly two sources, not independent votes or calibrated confidence. English C side is original-text passthrough.',
        'real_owner_rows':sum(not r['speaker'].startswith('STARTER') for r in rows),'pip_cache_before':cache_before,
        'source_sha256':{str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'amr_language').glob('*')) if p.is_file()},
        'harness_sha256':{str(p.relative_to(ROOT)):sha(p) for p in (Path(__file__).resolve(),ROOT/'scripts/translation_worker_C.py',ROOT/'scripts/runner_control.py',ROOT/'scripts/bench_utils.py')},
        'data_sha256':{str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'data/owner_check.csv',ROOT/'data/owner_labels_proposed.json',ROOT/'data/task4_names_example.yaml',ROOT/'data/indictrans2_C_model.json')},
        'protected_task4b_components':{n:fixed['amr_language/'+n] for n in protected}}
    write(BASE/'metadata.json',metadata)
    for device in ('cpu','cuda'):
        unloaded(device,rows,metadata)
        paired(device,rows,names,labels,settings,local_backend,metadata)
    cache_after={'files':sum(p.is_file() for p in cache.rglob('*')),'bytes':sum(p.stat().st_size for p in cache.rglob('*') if p.is_file())}
    write(BASE/'completion.json',{'time':stamp(),'phases':4,'pip_cache_before':cache_before,'pip_cache_after':cache_after,'pip_cache_retained':cache_after==cache_before})
    print('C campaign complete; four fresh phases, no cache deletion.',flush=True)


if __name__=='__main__':main()
