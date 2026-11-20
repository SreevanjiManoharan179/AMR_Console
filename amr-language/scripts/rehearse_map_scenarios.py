#!/usr/bin/env python3
"""Authored English/Tamil map rehearsals with recorded rewrites, never a robot."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import yaml
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import MockOfficeBackend
from amr_language.rehearsal import Rehearsal


def main():
    directory=ROOT/'records/task4/map_rehearsals'
    if directory.exists():raise ValueError('Use preserved first scenario results; do not overwrite')
    directory.mkdir(parents=True)
    path=ROOT/'data/task4_names_example.yaml';original=path.read_bytes();base=yaml.safe_load(original)
    scenarios=[
      ('unknown','Go to server room.','server room-க்கு போங்க.',[]),
      ('off_map_decline','Go to new pantry.','புதிய pantry-க்கு போங்க.',['cafeteria','no']),
      ('off_map_confirm','Go to new pantry.','புதிய pantry-க்கு போங்க.',['cafeteria','yes']),
      ('unreachable','Go to storage.','storage-க்கு போங்க.',['later']),
      ('missing_usual','Give the file to Arun.','file-ஐ Arun-கிட்ட கொடுங்க.',['open office','yes','taken']),
      ('hint_decline','Give the file to Divya.','file-ஐ Divya-கிட்ட கொடுங்க.',['yes','not here','conference hall','no']),
      ('hint_confirm','Give the file to Divya.','file-ஐ Divya-கிட்ட கொடுங்க.',['yes','not here','conference hall','yes','taken']),
      ('hint_never','Give the file to Divya.','file-ஐ Divya-கிட்ட கொடுங்க.',['yes','not here','conference hall']),
      ('hint_unmapped','Give the file to Divya.','file-ஐ Divya-கிட்ட கொடுங்க.',['yes','not here','new pantry']),
      ('seen_three_times','Give the file to Divya.','file-ஐ Divya-கிட்ட கொடுங்க.',['yes','not here','conference hall','yes','taken']),
    ]
    report=[]
    for language in ('en','ta'):
        for label,english,tamil,replies in scenarios:
            names=deepcopy(base);names['map_id']='SCRIPTED_TASK4_'+label+'_'+language
            names['rooms'].append({'name':'new_pantry','aliases':['new pantry'],'aliases_by_language':{'ta':['புதிய pantry']},
                                   'on_map':False,'reachable':False,'nearest_mapped_place':'cafeteria'})
            if label=='unreachable':next(p for p in names['rooms'] if p['name']=='storage').update(reachable=False,unreachable_reason='blocked')
            if label=='missing_usual':next(p for p in names['people'] if p['name']=='Arun')['usual_place']=None
            before=deepcopy(names);sessions=[]
            for occurrence in range(3 if label=='seen_three_times' else 1):
                answers=iter(replies)
                def scripted(_):
                    try:return next(answers)
                    except StopIteration:raise EOFError()
                mock=MockOfficeBackend([english]*4,[{}]*4)
                file=directory/f'{label}_{language}_{occurrence+1}.jsonl'
                settings={'backend':mock,'follow_person_hint':'never' if label=='hint_never' else 'ask'}
                session=Rehearsal(names,settings,input_fn=scripted,output_fn=lambda text:None,log_path=None,source='SCRIPTED')
                result=session.run(english if language=='en' else tamil,'Bharathwaj',language)
                file.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in session.records))
                assert names==before
                sessions.append({'status':result['status'],'jobs':[{'decision':j['decision'],'outcomes':j.get('outcomes',[])} for j in result['jobs']],
                                 'hint_steps':sum(r['kind']=='confirmed_hint_step' for r in session.records),
                                 'events':[r['event'] for r in session.records if r['kind']=='robot'],'record':str(file.relative_to(ROOT))})
            report.append({'scenario':label,'language':language,'source':'SCRIPTED','model':'recorded mock rewrites, not quality measurement',
                           'names_unchanged':True,'sessions':sessions})
    assert path.read_bytes()==original
    (directory/'summary.json').write_text(json.dumps({'scenarios':report,'names_file_sha256':hashlib.sha256(original).hexdigest(),
      'map_or_names_changes':False,'robot_motion':False,'native_review':'needs native-speaker check'},ensure_ascii=False,indent=2)+'\n')
    print('Recorded',len(report),'English/Tamil scenario groups; names unchanged; no robot')


if __name__=='__main__':main()
