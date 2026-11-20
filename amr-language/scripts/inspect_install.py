#!/usr/bin/env python3
"""Freeze the approved local model's actual files and serving identity; no downloads."""
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.backends import OllamaBackend
from amr_language.calibration import file_sha256

MODEL='qwen3.5:2b-q4_K_M'


def inspect():
    runtime=ROOT/'.runtime'
    manifests=runtime/'models/manifests'
    matches=[path for path in manifests.rglob('*') if path.is_file() and path.name=='2b-q4_K_M']
    if len(matches)!=1:
        raise ValueError('Expected exactly one installed 2B manifest')
    manifest=matches[0]
    value=json.loads(manifest.read_bytes())
    blobs=[]
    for layer in [value['config'],*value['layers']]:
        algorithm,digest=layer['digest'].split(':',1)
        if algorithm!='sha256' or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Unexpected blob digest')
        path=runtime/'models/blobs'/('sha256-'+digest)
        if not path.resolve().is_relative_to(runtime.resolve()):
            raise ValueError('Blob path escaped runtime')
        actual=file_sha256(path)
        if actual!=digest or path.stat().st_size!=layer['size']:
            raise ValueError('Installed blob checksum or size mismatch')
        blobs.append({'media_type':layer['mediaType'],'path':str(path.relative_to(ROOT)),
                      'bytes':path.stat().st_size,'sha256':actual})
    weights=[blob for blob in blobs if blob['media_type']=='application/vnd.ollama.image.model']
    if len(weights)!=1:
        raise ValueError('Expected exactly one main-weight blob')
    backend=OllamaBackend(MODEL,weights[0]['sha256'])
    backend.load_manifest(manifest)
    backend.verify_identity({'port':11434,'runner_version':'0.40.12'},5.)
    if not backend.registry_manifest_sha256.startswith('1e2d21a4f03a'):
        raise ValueError('Model tag changed since the reviewed proposal; review new manifest before use')
    settings={'model':MODEL,'samples':7,'timeout_s':2.,'temperature':.7,'num_ctx':4096,
              'num_predict':512,'seed':1729,'port':11434,'model_file':weights[0]['path'],
              'model_manifest_file':str(manifest.relative_to(ROOT)),'runner_version':'0.40.12'}
    record={'timestamp':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'grade':'checked',
            'runner_version':'0.40.12','model':MODEL,'manifest_file':str(manifest.relative_to(ROOT)),
            'manifest_sha256':backend.manifest_sha256,'blobs':blobs,
            'registry_manifest_sha256':backend.registry_manifest_sha256,
            'binary_sha256':file_sha256(runtime/'ollama/bin/ollama'),
            'model_blob_bytes':sum(blob['bytes'] for blob in blobs),
            'extracted_runtime_regular_file_bytes':sum(p.stat().st_size for p in (runtime/'ollama').rglob('*') if p.is_file() and not p.is_symlink()),
            'settings_file':'data/installed_model_settings.json',
            'calibration':'not collected; settings do not authorize act'}
    (ROOT/'data/installed_model_settings.json').write_text(json.dumps(settings,indent=2)+'\n')
    (ROOT/'records/checks/model_inventory.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))


if __name__=='__main__':
    inspect()
