#!/usr/bin/env python3
"""Freeze owner-reviewed sheets only after real rows and explicit confirmation exist."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.calibration import file_sha256
from amr_language.office_schema import validate_plan,walk_steps

FILES=('data/owner_check.csv','data/replies_check.csv','data/owner_labels_proposed.json')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    hashes={name:file_sha256(ROOT/name) for name in FILES}
    with (ROOT/FILES[0]).open(newline='') as stream:
        rows=[row for row in csv.DictReader(stream) if row['text'].strip() and row['speaker'] and not row['speaker'].startswith('STARTER')]
    labels={row['id']:row for row in json.loads((ROOT/FILES[2]).read_text())['rows']}
    counts=Counter(row['language'] for row in rows)
    long_counts=Counter()
    problems=[]
    for row in rows:
        label=labels.get(row['id'])
        if not label or not label.get('confirmed_by_owner'):
            problems.append('Unconfirmed/missing JSON label: '+row['id']);continue
        if label.get('expected_issue') is None:
            validate_plan({'steps':label['expected_steps'],'issue':None})
            if sum(1 for _ in walk_steps(label['expected_steps']))>=3: long_counts[row['language']]+=1
    for language in ('en','ta','ml','te'):
        if counts[language]<10: problems.append(f'{language}: need 10 owner/friend rows, have {counts[language]}')
        if long_counts[language]<4: problems.append(f'{language}: need 4 confirmed long requests, have {long_counts[language]}')
    confirmation=ROOT/'data/office_label_confirmation.json'
    if not confirmation.exists(): problems.append('Owner confirmation file is absent')
    else:
        value=json.loads(confirmation.read_text())
        if value.get('confirmed_by_owner') is not True or value.get('reply_labels_confirmed') is not True or value.get('sha256')!=hashes:
            problems.append('Confirmation does not cover these exact sheet and label bytes')
    status={'status':'PENDING_OWNER_DATA_AND_CONFIRMATION' if problems else 'READY_TO_FREEZE',
            'sha256':hashes,'owner_rows_by_language':dict(counts),'long_owner_rows_by_language':dict(long_counts),
            'problems':problems,'note':'These pending hashes are drift checks, not a confirmed test-set freeze.'}
    (ROOT/'records/office_checks/labels_status.json').write_text(json.dumps(status,indent=2)+'\n')
    print(json.dumps(status,indent=2))
    if problems:
        return 0 if args.check_only else 2
    if args.check_only: return 0
    stamp=datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S_%f')
    directory=ROOT/'records/office_checks/frozen'/stamp
    directory.mkdir(parents=True,exist_ok=False)
    for name in FILES: shutil.copyfile(ROOT/name,directory/Path(name).name)
    shutil.copyfile(confirmation,directory/confirmation.name)
    (directory/'sha256.json').write_text(json.dumps({'status':'OWNER_CONFIRMED','sha256':hashes},indent=2)+'\n')
    print('Frozen:',directory)
    return 0


if __name__=='__main__': raise SystemExit(main())
