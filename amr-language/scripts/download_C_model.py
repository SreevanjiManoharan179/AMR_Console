#!/usr/bin/env python3
"""Exact approved-C downloader; run only after human approval and gate acceptance."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-C',action='store_true',help='Use only after the owner explicitly says approved C')
    args=parser.parse_args()
    if not args.approved_C: parser.error('STOP: owner must approve C and personally accept the Hugging Face gate')
    manifest=json.loads((ROOT/'data/indictrans2_C_model.json').read_text())
    destination=ROOT/'.runtime/indictrans2-C';destination.mkdir(parents=True,exist_ok=True)
    os.environ['HF_HOME']=str(destination/'hf-cache')
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    os.environ['HF_HUB_DISABLE_XET']='1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS']='1'
    from huggingface_hub import hf_hub_download
    token=getpass.getpass('Hugging Face read token (hidden, never saved): ')
    if not token: raise SystemExit('No token supplied; nothing downloaded')
    inventory=[]
    try:
        for descriptor in manifest['files']:
            path=Path(hf_hub_download(repo_id=manifest['repo_id'],revision=manifest['revision'],
                filename=descriptor['rfilename'],local_dir=str(destination/'model'),token=token))
            size=path.stat().st_size
            if size!=descriptor['size']: raise ValueError('Pinned file size differs')
            sha256=hashlib.sha256();git_sha1=hashlib.sha1(b'blob '+str(size).encode()+b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):
                    sha256.update(chunk);git_sha1.update(chunk)
            if descriptor.get('lfs'):
                if sha256.hexdigest()!=descriptor['lfs']['sha256']: raise ValueError('Pinned file checksum differs')
            elif git_sha1.hexdigest()!=descriptor['blobId']:
                raise ValueError('Pinned git blob differs')
            inventory.append({'file':descriptor['rfilename'],'bytes':size,'sha256':sha256.hexdigest()})
    except Exception as error:
        # Do not echo authentication headers, signed URLs, token or raw exception text.
        raise SystemExit('Download/verification failed: '+type(error).__name__)
    finally: token=None
    (ROOT/'records/task3/C_model_inventory.json').write_text(json.dumps({'repo_id':manifest['repo_id'],
        'revision':manifest['revision'],'files':inventory},indent=2)+'\n')
    print('Pinned files downloaded and verified. Review the custom Python sources before model loading.')


if __name__=='__main__': main()
