#!/usr/bin/env python3
"""Repeat real adapter aborts while switching rewrite/plan contexts; no quality scoring."""
import json
from pathlib import Path
import sys
import time

from runner_control import ROOT,Runner
from bench_utils import ResourceMonitor
sys.path.insert(0,str(ROOT))
from amr_language.backends import OllamaBackend,BackendError
from amr_language.office_backend import REWRITE_PROMPT,PLAN_PROMPT
from amr_language.office_schema import SCHEMA_V3


def main():
    destination=ROOT/'records/task3/cancellation_stress';destination.mkdir(exist_ok=False)
    english='Give the file to Divya; give the folder to Priya; give the parcel to Ravi; give the document to Arun; give the charger to Meera; give the letter to Neha.'
    outcomes=[]
    for contestant in ('A','B'):
        model=json.loads((ROOT/f'data/office_model_{contestant}.json').read_text())['model']
        with Runner(destination/contestant) as runner:
            runner.load(model)
            if not runner.ensure_live(): raise RuntimeError('No initial generation liveness')
            monitor=ResourceMonitor(destination/contestant/'resources.jsonl').start()
            client=OllamaBackend(model)
            try:
                for index in range(12):
                    # Alternating prompt prefix/format exercises recurrent-state cache restore.
                    planning=index%2==0
                    payload={'model':model,'stream':False,'think':False,'keep_alive':'30m',
                        'messages':[{'role':'system','content':PLAN_PROMPT if planning else REWRITE_PROMPT},
                                    {'role':'user','content':english if planning else 'Translate and preserve every detail: '+english*4}],
                        'options':{'num_ctx':4096,'num_predict':768,'temperature':0 if planning else .7,'seed':100+index}}
                    if planning: payload['format']=SCHEMA_V3
                    started=time.monotonic()
                    try:
                        value=client._request('POST','/api/chat',payload,11434,1.)
                        outcome={'response_completed':True,'done':value.get('done')}
                    except BackendError as error: outcome={'response_completed':False,'error':type(error).__name__}
                    outcome.update(contestant=contestant,index=index,elapsed_ms=(time.monotonic()-started)*1000)
                    outcome['liveness']=runner.probe();outcomes.append(outcome)
                    (destination/'results.json').write_text(json.dumps(outcomes,indent=2)+'\n')
                    if not outcome['liveness']['ok']:
                        outcome['eventual']=runner.probe(60)
                        (destination/'results.json').write_text(json.dumps(outcomes,indent=2)+'\n')
                        break
            finally: monitor.stop()
        print('Abort stress completed:',contestant,flush=True)
    (destination/'summary.json').write_text(json.dumps({'complete':True,'trials':len(outcomes),
        'immediate_liveness_passes':sum(row['liveness']['ok'] for row in outcomes)},indent=2)+'\n')


if __name__=='__main__': main()
