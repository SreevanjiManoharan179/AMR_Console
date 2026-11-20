#!/usr/bin/env python3
"""Owner-only review: regression is run before asking for final data acceptance."""
import argparse
import json
from pathlib import Path
import sys
import yaml
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_learning import LearningStore,regression,accept,validate_candidate
from amr_language.office_backend import load_office_profile,prepare_office


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--names',required=True)
    parser.add_argument('--capability',required=True)
    args=parser.parse_args()
    names=yaml.safe_load(Path(args.names).read_text());settings=load_office_profile()
    settings['capability_profile']=yaml.safe_load(Path(args.capability).read_text())
    from amr_language.office_capabilities import validate_capability
    if validate_capability(settings['capability_profile']) is None:raise ValueError('Configure and confirm physical capability limits first')
    prepare_office(settings)
    store=LearningStore();store.prune()
    for row in store.pending():
        print(json.dumps(row,ensure_ascii=False,indent=2))
        action=input('review / reject / edit / quit> ').strip().lower()
        if action=='quit':break
        if action=='reject':store.review_event(row,'rejected',{});continue
        if action=='edit':
            row['candidate']=json.loads(input('Edited candidate JSON> '));validate_candidate(row['kind'],row['candidate'])
        elif action!='review':continue
        report=regression(row['candidate'],row['kind'],names,settings)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if not report['passed']:
            store.review_event(row,'rejected_regression',report);continue
        if input('All cases measured. Type approve to accept this exact data change> ').strip()=='approve':
            accept(store,row,report,owner_confirmed=True)


if __name__=='__main__':main()
