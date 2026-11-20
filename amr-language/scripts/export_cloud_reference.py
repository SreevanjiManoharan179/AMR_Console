#!/usr/bin/env python3
"""Prepare manual cloud-reference prompts. Does not contact a cloud service."""
import csv
import json
from pathlib import Path
import sys

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import REWRITE_PROMPT,PLAN_PROMPT
from amr_language.office_schema import SCHEMA_V3
from amr_language.office_names import OfficeNames


def main():
    names=yaml.safe_load((ROOT/'data/office_names_example.yaml').read_text())
    roster=OfficeNames(names,'Bharathwaj')
    with (ROOT/'data/owner_check.csv').open(newline='') as stream:
        rows=[row for row in csv.DictReader(stream) if row['text']]
    lines=['# R: cloud reference, not the product','',
      '[checked] These are prepared prompts only. No browser submission, response or timing has been collected. '
      'Use the same request IDs and record the visible model label, date, complete rewrite/plan, elapsed time and owner ratings. '
      'The browser does not necessarily expose temperature, seed or system roles; do not claim identical sampling or a local GPU measurement. '
      'Use a clean chat context for each request to avoid earlier requests contaminating it.','',
      '## Stage 1: request-specific rewrite text','']
    for row in rows:
        aliases=[]
        for entry in roster.prompt_roster(row['language'])['approved']:
            for alias in entry['aliases']:
                if alias in row['text'] and not alias.isascii(): aliases.append(alias+' = '+entry['id'])
        context=('\nName spellings: '+'; '.join(aliases)) if aliases else ''
        lines+=['### '+row['id'],'','```text','SYSTEM INSTRUCTION TEXT:',REWRITE_PROMPT+context,
                '\nUSER REQUEST:',row['text'],'```','']
    lines+=['## Stage 2: plan each saved English rewrite','',
            '[checked] Paste this same planner instruction and schema with the saved English rewrite and requester ID. '
            'Retain the output as plan data; it is never a robot utterance.','',
            '```text',PLAN_PROMPT,'Requester: Bharathwaj','English rewrite: <paste the saved rewrite>',
            'JSON schema:',json.dumps(SCHEMA_V3,ensure_ascii=False),'```','',
            '[checked] If collecting four reference pairs, store all four and mark sampling controls as unavailable unless '
            'actually exposed. Meaning and long-request success still need human ratings. Do not substitute the local '
            'model\'s outputs or these prepared prompts for cloud-reference observations.']
    (ROOT/'data/cloud_reference_prompts.md').write_text('\n'.join(lines)+'\n')
    results=ROOT/'data/cloud_reference_results.csv'
    if not results.exists():
        with results.open('w',newline='') as stream:
            csv.writer(stream).writerow(['id','language','visible_model','english_rewrite','plan_json','elapsed_ms',
                                        'meaning_yes_no','steps_yes_no','names_yes_no','long_success_yes_no','notes'])
    print('Prepared manual R prompts and empty results sheet; no cloud reference run')


if __name__=='__main__': main()
