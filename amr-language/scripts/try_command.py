#!/usr/bin/env python3
"""Show office v3 English, plan, readback and decision. Typed input only."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office import AGREEMENT_NOTICE,understand
from amr_language.office_backend import prepare_office,load_office_profile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('text',nargs='?')
    parser.add_argument('--requester',required=True)
    parser.add_argument('--language',choices=['en','ta','ml','te'])
    parser.add_argument('--names',default=str(ROOT/'reference/tags_example.yaml'))
    parser.add_argument('--profile',choices=['office_B'],default='office_B')
    parser.add_argument('--settings',help='Explicit alternate profile JSON')
    parser.add_argument('--capability',required=True,help='Owner-filled confirmed capability YAML')
    parser.add_argument('--timeout',type=float)
    args=parser.parse_args()
    names=yaml.safe_load(Path(args.names).read_text())
    settings=json.loads(Path(args.settings).read_text()) if args.settings else load_office_profile(args.profile)
    settings['capability_profile']=yaml.safe_load(Path(args.capability).read_text())
    if args.timeout is not None: settings['timeout_s']=args.timeout
    startup=prepare_office(settings)
    print('STARTUP (outside request timing):',json.dumps(startup))
    trace=[]
    settings['trace']=trace
    text=args.text if args.text is not None else input('typed request> ')
    print(AGREEMENT_NOTICE)
    print('Thinking…',flush=True)
    result=understand(text,names,args.requester,args.language,settings)
    print('SCREEN DATA:',json.dumps(result,ensure_ascii=False,indent=2))
    folder=ROOT/'records/rehearsals'
    folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S_%f')
    path=folder/f'command_{stamp}.json'
    path.write_text(json.dumps({'typed_text':text,'requester_id':args.requester,'language':result['language'],'readback':result['readback'],'decisions':{'decision':result['decision'],'requester_yes':False},'outcome':'preview only'},ensure_ascii=False,indent=2)+'\n')
    from amr_language.office_learning import LearningStore
    store=LearningStore(retention_days=settings.get('learning_retention_days',30));store.prune()
    store.append('owner_row',{'language':result['language'],'text':text,'expected_english':text,'expected_steps_notes':'Owner must confirm intent; preview only'},typed_text=text,requester_id=args.requester,language=result['language'],readback=result['readback'],decisions={'preview_only':True},outcome=result['decision'])
    print('Record:',path)


if __name__=='__main__': main()
