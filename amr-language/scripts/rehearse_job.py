#!/usr/bin/env python3
"""Rehearse a whole office job in text, with no robot or real timers."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import yaml

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.rehearsal import Rehearsal
from amr_language.office_backend import prepare_office,load_office_profile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requester')
    parser.add_argument('--language',choices=['en','ta','ml','te'])
    parser.add_argument('--text')
    parser.add_argument('--names',default=str(ROOT/'reference/tags_example.yaml'))
    parser.add_argument('--profile',choices=['office_B'],default='office_B')
    parser.add_argument('--settings',help='Explicit alternate profile JSON')
    parser.add_argument('--capability',required=True,help='Owner-filled confirmed capability YAML')
    parser.add_argument('--timeout',type=float)
    parser.add_argument('--answers-file',help='Optional authored text-rehearsal replies; logged as STARTER, not owner data')
    args=parser.parse_args()
    names=yaml.safe_load(Path(args.names).read_text())
    settings=json.loads(Path(args.settings).read_text()) if args.settings else load_office_profile(args.profile)
    settings['capability_profile']=yaml.safe_load(Path(args.capability).read_text())
    if args.timeout is not None: settings['timeout_s']=args.timeout
    print('STARTUP (outside request timing):',json.dumps(prepare_office(settings)))
    requester=args.requester or input('requester ID> ').strip()
    language=args.language or input('language (en/ta/ml/te)> ').strip()
    text=args.text if args.text is not None else input('typed request> ')
    print('Use /queue <requester> <language> <typed request> at a reply prompt for a second requester.')
    print('Use /person <approved person ID> to identify a respondent at a shared place.')
    stamp=datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S_%f')
    path=ROOT/'records/rehearsals'/f'job_{stamp}.jsonl'
    input_fn=input
    if args.answers_file:
        answers=iter(Path(args.answers_file).read_text().splitlines())
        def scripted(prompt):
            try: return next(answers)
            except StopIteration: raise EOFError()
        input_fn=scripted
    session=Rehearsal(names,settings,log_path=path,input_fn=input_fn,
                      source='STARTER_SCRIPTED' if args.answers_file else 'UNREVIEWED_TYPED')
    result=session.run(text,requester,language)
    print('Session:',result['status'])
    print('Record:',path)


if __name__=='__main__': main()
