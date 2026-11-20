#!/usr/bin/env python3
"""Warm an installed local A/B model before a separately labelled warm check."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import make_backend,prepare_office,settings_for_office,load_office_profile
from bench_utils import ResourceMonitor


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--contestant',choices=['A','B'])
    group.add_argument('--profile',choices=['office_B'],default='office_B')
    args=parser.parse_args()
    config=settings_for_office(json.loads((ROOT/f'data/office_model_{args.contestant}.json').read_text())) if args.contestant else load_office_profile(args.profile)
    args.contestant=args.contestant or 'B'
    startup=prepare_office(config)
    client=make_backend(config)
    client.verify(config,5.)
    stamp=datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y%m%d_%H%M%S_%f')
    directory=ROOT/'records/rehearsals';directory.mkdir(parents=True,exist_ok=True)
    path=directory/f'warmup_{args.contestant}_{stamp}.json'
    started=time.monotonic()
    monitor=ResourceMonitor(path.with_name(path.stem+'_resources.jsonl')).start()
    try:
        result=client.client._request('POST','/api/generate',{'model':config['model'],'prompt':'','stream':False,
            'keep_alive':'5m','options':{'num_ctx':config['num_ctx'],'num_predict':1}},config['port'],60.)
        if result.get('done') is not True: raise ValueError('Warmup did not complete')
    finally:
        resources=monitor.stop()
    record={'time':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'contestant':args.contestant,
            'model':config['model'],'startup':startup,'elapsed_ms':(time.monotonic()-started)*1000,
            'scope':'empty-prompt model load, separate from warm request-to-readback latency',
            'resources':resources,'result_done':result['done']}
    path.write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    print('Record:',path)


if __name__=='__main__': main()
