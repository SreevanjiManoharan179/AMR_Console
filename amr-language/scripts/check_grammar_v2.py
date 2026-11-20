#!/usr/bin/env python3
"""Evaluate a frozen v2 grammar on both immutable authored holdouts and starters."""
import csv,hashlib,importlib.util,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_grammar import parse_english
from amr_language.office_schema import validate_plan


def main():
    output=ROOT/'records/task4b/grammar_coverage';assert not output.exists()
    freeze=json.loads((ROOT/'records/task4b/holdout_v2_freeze.json').read_text())
    assert hashlib.sha256((ROOT/freeze['file']).read_bytes()).hexdigest()==freeze['sha256']
    old=ROOT/'records/task4b/grammar_before.py'
    assert hashlib.sha256(old.read_bytes()).hexdigest()==freeze['baseline_grammar_sha256']
    spec=importlib.util.spec_from_file_location('amr_language._task4b_baseline',old)
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    groups={}
    for version,path in [('holdout_v1','data/grammar_holdout.csv'),('holdout_v2','data/grammar_holdout_v2.csv')]:
        with (ROOT/path).open(newline='') as stream:groups[version]=list(csv.DictReader(stream))
    with (ROOT/'data/owner_check.csv').open(newline='') as stream:
        groups['intended_starters']=[{'id':r['id'],'text':r['expected_english']} for r in csv.DictReader(stream) if r['speaker'].startswith('STARTER') and r['text'].strip()]
    groups['task4_B_off_rewrites']=[]
    for line in (ROOT/'records/task4/B_glossary_off/requests.jsonl').read_text().splitlines():
        row=json.loads(line)
        groups['task4_B_off_rewrites'] += [{'id':row['id']+'/'+str(p['index']),'text':p.get('rewrite_raw','')} for p in row['trace'] if 'index' in p]
    output.mkdir();summary={}
    grammar=ROOT/'amr_language/office_grammar.py'
    (output/'candidate.py').write_bytes(grammar.read_bytes())
    with (output/'requests.jsonl').open('w') as out:
        for version,parser in [('grammar_v1',baseline.parse_english),('grammar_v2',parse_english)]:
            summary[version]={}
            for name,requests in groups.items():
                parsed=0;issues=0
                for row in requests:
                    tick=time.monotonic();plan=parser(row['text'],'Bharathwaj');elapsed=(time.monotonic()-tick)*1000
                    if plan is not None:validate_plan(plan);parsed+=1;issues+=plan['issue'] is not None
                    out.write(json.dumps({'grammar':version,'group':name,**row,'parsed':plan is not None,'plan':plan,'ms':elapsed},ensure_ascii=False)+'\n')
                summary[version][name]={'parsed':parsed,'rows':len(requests),'explicit_refusal_parses':issues,'coverage':parsed/len(requests)}
    record={'time':time.time(),'grammar_sha256':hashlib.sha256(grammar.read_bytes()).hexdigest(),
            'data_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ('data/grammar_holdout.csv','data/grammar_holdout_v2.csv')},
            'groups':summary,'notice':'Authored parsing/schema coverage, not grounded correctness or meaning. No tuning after first held-out evaluation.'}
    (output/'summary.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record,indent=2))

if __name__=='__main__':main()
