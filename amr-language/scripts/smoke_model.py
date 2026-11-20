#!/usr/bin/env python3
"""Real local-model smoke tests; synthetic requests, no calibration or dispatch."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.core import understand
from amr_language.backends import OllamaBackend
from amr_language.calibration import file_sha256
from amr_language.core import _settings,source_span
from amr_language.grounding import Names
from amr_language.schema import validate_task_form
from bench_utils import ResourceMonitor


def main():
    settings=json.loads((ROOT/'data/installed_model_settings.json').read_text())
    config=_settings(settings)
    backend=OllamaBackend(config['model'],file_sha256(ROOT/config['model_file']))
    backend.load_manifest(ROOT/config['model_manifest_file'])
    backend.verify_identity(config,5.)
    resident=backend._request('GET','/api/ps',None,config['port'],5.)
    loaded_before=any(row.get('name')==config['model'] for row in resident.get('models',[]))
    metadata={}
    original_request=backend._request
    def measured_request(*args,**kwargs):
        value=original_request(*args,**kwargs)
        metadata.clear()
        metadata.update({key:value[key] for key in ('total_duration','load_duration','prompt_eval_count',
                        'prompt_eval_duration','eval_count','eval_duration') if key in value})
        return value
    backend._request=measured_request
    names=yaml.safe_load((ROOT/'data/dev_names.yaml').read_text())
    roster=Names(names)
    rows=[json.loads(line) for line in (ROOT/'data/dev_synthetic.jsonl').read_text().splitlines()]
    selected=[]
    for lang in ('en','ta','ml','te'):
        for category in ('go','deliver','multi'):
            selected.append(next(row for row in rows if row['language']==lang and row['category']==category))
    selected+= [next(row for row in rows if row['language']=='en' and row['category']==category)
                for category in ('ambiguous','unknown','out_of_scope')]
    output=ROOT/'records/checks/model_smoke.jsonl'
    monitor=ResourceMonitor(ROOT/'records/checks/model_smoke_resources.jsonl').start()
    results=[]
    try:
        with output.open('w') as stream:
            for index,row in enumerate(selected):
                started=time.monotonic()
                record={'source':'SYNTHETIC','id':row['id'],'language':row['language'],'text':row['text'],
                        'expected_task_form':row['expected_task_form'],
                        'first_request_cold':index==0 and not loaded_before,
                        'samples':1,'console_navigation':'not running/established'}
                try:
                    form=backend.sample(row['text'],roster,row['language'],timeout=120.,index=0,settings=config)
                    record['task_form']=form
                    validate_task_form(form)
                    record['valid']=True
                    record['source_spans_valid']=all(step[slot] is None or source_span(step[slot],row['text'])
                                                    for step in form['steps'] for slot in ('place','person','object','item'))
                    record['grounding']=[{slot:list(roster.match(slot,step[slot])) for slot in ('place','person','object')
                                          if step[slot] is not None} for step in form['steps']]
                except Exception as error:
                    record['valid']=False
                    record['error_type']=type(error).__name__
                    record['error']=str(error)
                record['latency_ms']=(time.monotonic()-started)*1000
                record['runner_metrics']=dict(metadata)
                stream.write(json.dumps(record,ensure_ascii=False)+'\n')
                stream.flush()
                results.append(record)
                print(json.dumps({'completed':index+1,'total':len(selected),'language':row['language'],
                                  'valid':record['valid'],'latency_ms':round(record['latency_ms'],1)}),flush=True)
    finally:
        resource_summary=monitor.stop()
    # Exercise the actual public API with its required k and deadline on unchanged approved names.
    approved=yaml.safe_load((ROOT/'reference/tags_example.yaml').read_text())
    api=[]
    for text in ('go to reception','take file to Priya then return to dock',"don't go to reception"):
        api.append(understand(text,approved,language='en',settings=settings))
    (ROOT/'records/checks/public_api_smoke.json').write_text(json.dumps(api,ensure_ascii=False,indent=2)+'\n')
    summary={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'source':'SYNTHETIC',
             'requests':len(results),'schema_valid':sum(row['valid'] for row in results),
             'single_sample_diagnostic_only':True,'resource_monitor':resource_summary,
             'public_api_decisions':[{'decision':r['decision'],'refusal':r['refusal']['template'] if r['refusal'] else None,
                                      'samples':r['samples'],'latency_ms':r['latency_ms']} for r in api],
             'shared_load_benchmark':'not run','real_command_accuracy':'not measured'}
    (ROOT/'records/checks/model_smoke_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    main()
