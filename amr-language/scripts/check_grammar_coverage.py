#!/usr/bin/env python3
"""Evaluate a frozen grammar candidate; never edits the held-out sheet or parser."""
import csv
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_grammar import parse_english
from amr_language.office_schema import validate_plan


def main():
    directory=ROOT/'records/task4/grammar_coverage_v1'
    if directory.exists():raise ValueError('First heldout evaluation already recorded; do not silently overwrite')
    manifest=json.loads((ROOT/'records/task4/grammar_candidate_freeze.json').read_text())
    holdout=ROOT/'data/grammar_holdout.csv'
    assert hashlib.sha256(holdout.read_bytes()).hexdigest()==manifest['holdout_sha256']
    assert hashlib.sha256((ROOT/'amr_language/office_grammar.py').read_bytes()).hexdigest()==manifest['grammar_sha256']
    groups={}
    with (ROOT/'data/owner_check.csv').open(newline='') as f:
        groups['starter_intended_english']=[{'id':r['id'],'text':r['expected_english']} for r in csv.DictReader(f) if r['speaker'].startswith('STARTER')]
    for c in ('A','B'):
        groups['task3_'+c+'_rewrites']=[]
        for line in (ROOT/f'records/task3/final_{c}/requests.jsonl').read_text().splitlines():
            row=json.loads(line)
            groups['task3_'+c+'_rewrites'].extend({'id':row['id']+'/'+str(p['index']),'text':p['rewrite_raw']} for p in row['trace'] if 'rewrite_raw' in p)
    with holdout.open(newline='') as f:groups['heldout']=list(csv.DictReader(f))
    directory.mkdir();summary={}
    with (directory/'requests.jsonl').open('w') as out:
        for group,rows in groups.items():
            records=[]
            for row in rows:
                tick=time.monotonic();plan=parse_english(row['text'],'Bharathwaj');elapsed=(time.monotonic()-tick)*1000
                if plan is not None:validate_plan(plan)
                record={'group':group,**row,'parsed':plan is not None,'plan':plan,'latency_ms':elapsed}
                records.append(record);out.write(json.dumps(record,ensure_ascii=False)+'\n')
            unique={r['text'] for r in records};parsed={r['text'] for r in records if r['parsed']}
            summary[group]={'rows':len(rows),'parsed':sum(r['parsed'] for r in records),'unique_texts':len(unique),'unique_parsed':len(parsed),
                'coverage':sum(r['parsed'] for r in records)/len(rows),'note':'Coverage includes explicit supported refusal issues, not semantic correctness or grounded-name accuracy.'}
    (directory/'summary.json').write_text(json.dumps({'candidate':manifest,'groups':summary,'policy':'No tuning after this held-out evaluation; any later change must be separately versioned.'},indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
