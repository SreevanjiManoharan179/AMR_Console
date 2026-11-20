#!/usr/bin/env python3
"""Download the pinned speech models for office tasks, then check every file.

Run only after the owner has written the approval in docs/SPEECH_INSTALL_REQUEST.md, with the
speech environment's Python, from this repository:

    ../amr-speech/venv/bin/python -I scripts/speech_download.py --approved-speech whisper_base_en whisper_small_en
    ../amr-speech/venv/bin/python -I scripts/speech_download.py --approved-speech indic_conformer

Each model comes from its pinned revision only. Every file is checked against the published
listing of that revision (SHA-256 for large files, the git blob id for small ones). A gated model
asks for a Hugging Face read token in a hidden prompt; it is used in memory for this run only,
never printed and never saved. Files go to <speech>/hf/hub (Hugging Face cache layout).
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent


def speech_base():
    import yaml
    config = yaml.safe_load((ROOT / 'config/office_tasks.yaml').read_text(encoding='utf-8'))
    speech = config.get('speech') or {}
    return (ROOT / str(speech.get('path', '../amr-speech'))).resolve(), ROOT / speech.get('models', 'config/speech_models.json')


def digests(path, size):
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(b'blob ' + str(size).encode() + b'\0')
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha256.update(chunk)
            blob.update(chunk)
    return sha256.hexdigest(), blob.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--approved-speech', action='store_true',
                        help='Use only after the owner has written the approval for these downloads.')
    parser.add_argument('models', nargs='+', help='Model keys from config/speech_models.json')
    args = parser.parse_args()
    if not args.approved_speech:
        parser.error('STOP: the owner has to approve the speech install first (docs/SPEECH_INSTALL_REQUEST.md).')
    base, models_path = speech_base()
    models = json.loads(models_path.read_text(encoding='utf-8'))['models']
    unknown = [key for key in args.models if key not in models]
    if unknown:
        parser.error('unknown model key(s): ' + ', '.join(unknown) + '. Choose from: ' + ', '.join(models))
    hub = base / 'hf' / 'hub'
    hub.mkdir(parents=True, exist_ok=True)
    os.environ['HF_HOME'] = str(base / 'hf')
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    from huggingface_hub import HfApi, hf_hub_download

    record_path = base / 'models_verified.json'
    record = json.loads(record_path.read_text()) if record_path.is_file() else {}
    for key in args.models:
        entry = models[key]
        repo, revision = entry['repo'], entry['revision']
        token = None
        if entry.get('gated'):
            print(f'{repo} is gated: accept its conditions on the Hugging Face website first, signed in as yourself.')
            token = getpass.getpass('Hugging Face read token (hidden, used for this download only, never saved): ') or None
            if not token:
                sys.exit('No token given; nothing was downloaded.')
        print(f'{key}: {repo} at {revision[:12]}… listing files')
        inventory = []
        try:
            listing = [item for item in HfApi().list_repo_tree(repo, revision=revision, recursive=True, token=token)
                       if getattr(item, 'blob_id', None) is not None and hasattr(item, 'size')]
            total = sum(item.size for item in listing)
            print(f'{key}: {len(listing)} files, {total:,} bytes')
            for number, item in enumerate(listing, 1):
                path = Path(hf_hub_download(repo, item.path, revision=revision, cache_dir=str(hub), token=token))
                size = path.stat().st_size
                if size != item.size:
                    raise ValueError(f'size differs for {item.path}')
                sha256, blob = digests(path, size)
                lfs = getattr(item, 'lfs', None)
                expected = (lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)) if lfs else None
                if expected is not None and sha256 != expected:
                    raise ValueError(f'SHA-256 differs for {item.path}')
                if expected is None and blob != item.blob_id:
                    raise ValueError(f'git blob id differs for {item.path}')
                inventory.append({'file': item.path, 'bytes': size, 'sha256': sha256})
                if number % 50 == 0 or number == len(listing):
                    print(f'{key}: {number}/{len(listing)} files checked')
        except Exception as error:
            # Never echo tokens, signed links or raw server replies.
            detail = str(error)[:120] if isinstance(error, ValueError) else 'check the network, and for a gated model your access on its page'
            sys.exit(f'{key}: download or check failed ({type(error).__name__}: {detail}). Nothing was marked as installed.')
        finally:
            token = None
        folder = hub / ('models--' + repo.replace('/', '--'))
        # Offline loading may ask for "main": point it at the pinned revision.
        (folder / 'refs').mkdir(parents=True, exist_ok=True)
        (folder / 'refs' / 'main').write_text(revision)
        record[key] = {'repo': repo, 'revision': revision, 'files': len(inventory),
                       'bytes': sum(row['bytes'] for row in inventory), 'inventory': inventory}
        record_path.write_text(json.dumps(record, indent=1) + '\n')
        print(f'{key}: all {len(inventory)} files match the published revision.')
        code = entry.get('remote_code')
        if code:
            snapshot = folder / 'snapshots' / revision
            sha = hashlib.sha256((snapshot / code).read_bytes()).hexdigest()
            print(f'{key}: this model runs its own loader code: {snapshot / code}')
            print(f'{key}: SHA-256 {sha}. It is not used until a person has read it and recorded that SHA-256 '
                  f'as reviewed_code_sha256 in config/speech_models.json.')
    print('Done. The console finds the models when the language service starts.')


if __name__ == '__main__':
    main()
