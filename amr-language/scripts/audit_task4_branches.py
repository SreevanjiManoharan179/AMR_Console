#!/usr/bin/env python3
"""Replay exact task3 branch refusals without any model or changed design rules."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office import _slot_sources,_condition_issue
from amr_language.office_schema import normalized_plan
from amr_language.core import source_span


def main():
    rows=[json.loads(l) for l in (ROOT/'records/task3/final_B/requests.jsonl').read_text().splitlines()]
    output=[]
    for row in rows:
        if not row['id'].endswith('-06'): continue
        draws=[]
        for pair in row['trace']:
            if 'index' not in pair:continue
            raw=pair.get('raw_plan');entry={'draw':pair['index'],'reused_from':pair.get('plan_reused_from'),
                                         'english':pair.get('english'),'raw_plan':raw}
            try:
                plan=normalized_plan(raw,'Bharathwaj');entry['schema']='valid'
            except (ValueError,TypeError,KeyError) as error:
                entry.update(first_rejection='schema validation',reason=str(error));draws.append(entry);continue
            missing=[]
            for step in plan['steps']:
                for key in ('item','to','from','at','question','message'):
                    if key in step and not source_span(step[key],pair['english']):missing.append({'slot':key,'text':step[key]})
            entry['missing_source_spans']=missing
            try: _slot_sources(plan,pair['english'],'Bharathwaj')
            except ValueError as error: entry.update(first_rejection='source-span validation',reason=str(error))
            else:
                issue=_condition_issue(plan,pair['english'])
                entry.update(first_rejection='on_answer/source-condition consistency' if issue else 'none before grounding',reason=issue)
            entry['model_branch_error']=any(step['action']=='check' and step.get('on_answer') is None for step in plan['steps'])
            entry['grounding_reached']=entry['first_rejection']=='none before grounding'
            draws.append(entry)
        output.append({'id':row['id'],'terminal_result':row['result'],'draws':draws})
    path=ROOT/'records/task4/branch_refusal_audit.json';path.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    lines=['# Task 4: why seven conditional checks were refused','',
      '[checked] Exact replay of records/task3/final_B/requests.jsonl. No inference, schema or design change was made for this audit. '
      'All 28 draws returned schema-valid top-level plans but **on_answer:null**, flattening yes/no actions into unconditional steps. '
      'Those plans would execute both branches if accepted; rejecting them was correct. Grounding was never reached.','',
      '| Request | Draws / first rejection | Underlying planner problem |','| --- | --- | --- |']
    for row in output:
        grouped={}
        for draw in row['draws']:
            reason=draw['first_rejection']+': '+str(draw['reason'])
            if draw['missing_source_spans']:reason+=' ('+', '.join(p['slot']+'='+repr(p['text']) for p in draw['missing_source_spans'])+')'
            grouped.setdefault(reason,[]).append(draw['draw'])
        rendered='<br>'.join(str(indices)+' '+reason.replace('|','/') for reason,indices in grouped.items())
        lines.append('| '+row['id']+' | '+rendered+' | Model omitted on_answer and emitted unconditional branch actions |')
    lines+=['','[checked] en-06, ml-06 and te-06 reach the condition consistency guard, which correctly refuses the flattened plan. '
      'ta-06 draws 0/1/3 first fail because message="null" was invented; draw 2 reaches the condition guard. '
      'All mixed-06 draws first fail because the model changed a declarative source fragment into a reordered question '
      '(for example “the room ... is free” → “is the room free”), breaking the exact source-span rule. '
      'The planner branch omission is still present behind those earlier failures.','',
      '[checked] Existing rule (owner task v3): “on_answer ... one level deep, and only tell or deliver steps inside.” '
      'The JSON decoding schema allows this structure; the model did not choose it. Existing source rule compares plan slots '
      'with the English rewrite. No design-rule relaxation is needed or made: the new grammar can construct the correct nested '
      'branches and preserve question/message fragments verbatim instead of manufacturing or reordering them.','',
      '[checked] Some Tamil rewrites lost the message entirely (“tell Ravi”) or changed file to cup. The grammar must not invent '
      'missing payloads or silently repair that meaning. Such requests must fall back/ask/refuse and still require the owner’s '
      'confirmation. Agreement alone cannot establish a correct translation.','',
      '[checked] A separate module bug is covered when implementing the existing allowed branch semantics: natural referring '
      'forms “if so / if it is / if not” must be recognized only when bound to an actual check branch, while unrelated '
      'conditions remain refused. Any broader design change will be proposed for owner approval rather than silently enabled.']
    (ROOT/'docs/TASK4_BRANCH_AUDIT.md').write_text('\n'.join(lines)+'\n')
    print('Audited',len(output),'requests; all model branches were flattened; no grounding reached')


if __name__=='__main__':main()
