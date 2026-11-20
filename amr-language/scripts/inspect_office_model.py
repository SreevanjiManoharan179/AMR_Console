#!/usr/bin/env python3
"""Verify an already installed A/B model; never downloads or deletes files."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.backends import OllamaBackend
from amr_language.calibration import file_sha256
from amr_language.office_backend import DEFAULT_SETTINGS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contestant',choices=['A','B'],required=True)
    args=parser.parse_args()
    tag,prefix={'A':('2b-q4_K_M','1e2d21a4f03a'),'B':('4b-q4_K_M','d8b0f5e9760c')}[args.contestant]
    manifest=ROOT/'.runtime/models/manifests/registry.ollama.ai/library/qwen3.5'/tag
    data=json.loads(manifest.read_bytes())
    blobs=[]
    for descriptor in [data['config'],*data['layers']]:
        algorithm,digest=descriptor['digest'].split(':',1)
        if algorithm!='sha256' or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid blob digest')
        path=(ROOT/'.runtime/models/blobs'/('sha256-'+digest)).resolve()
        if not path.is_relative_to((ROOT/'.runtime/models/blobs').resolve()): raise ValueError('Blob escaped')
        if path.stat().st_size!=descriptor['size'] or file_sha256(path)!=digest:
            raise ValueError('Blob checksum/size mismatch')
        blobs.append({'path':str(path.relative_to(ROOT)),'sha256':digest,'bytes':path.stat().st_size,
                      'media_type':descriptor['mediaType']})
    weights=next(row for row in blobs if row['media_type']=='application/vnd.ollama.image.model')
    model='qwen3.5:'+tag
    client=OllamaBackend(model,weights['sha256'])
    client.load_manifest(manifest)
    if not client.registry_manifest_sha256.startswith(prefix): raise ValueError('Registry artifact changed; review required')
    config={**DEFAULT_SETTINGS,'model':model,'model_file':weights['path'],
            'model_manifest_file':str(manifest.relative_to(ROOT))}
    client.verify_identity(config,5.)
    output=ROOT/f'data/office_model_{args.contestant}.json'
    output.write_text(json.dumps(config,indent=2)+'\n')
    record={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'contestant':args.contestant,
            'model':model,'runner_version':config['runner_version'],'main_weights_sha256':weights['sha256'],
            'local_manifest_sha256':client.manifest_sha256,'registry_manifest_sha256':client.registry_manifest_sha256,
            'blobs':blobs,'settings_file':str(output.relative_to(ROOT))}
    path=ROOT/f'records/office_checks/model_{args.contestant}_inventory.json'
    path.write_text(json.dumps(record,indent=2)+'\n')
    print('Verified',model,'and wrote',output.relative_to(ROOT),'and',path.relative_to(ROOT))


if __name__=='__main__': main()
