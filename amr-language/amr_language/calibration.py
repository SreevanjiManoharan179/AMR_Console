"""Load only real-command calibration frozen to this pipeline and roster.

This is a provenance/compatibility check, not authentication of the data collector.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .backends import PROMPT_VERSION, SYSTEM_PROMPT
from .conformal import calibrate, prediction_set
from .grounding import fingerprint
from .schema import SCHEMA, parse_json

ROOT = Path(__file__).resolve().parent.parent


def local_path(value):
    path = Path(value)
    path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('File must be inside the module')
    return path


def file_sha256(path):
    path = Path(path)
    stat = path.stat()
    return _cached_file_sha256(str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=128)
def _cached_file_sha256(path, device, inode, size, mtime, ctime):
    digest = hashlib.sha256()
    path = Path(path)
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    current = path.stat()
    if (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns) != (device,inode,size,mtime,ctime):
        raise ValueError('File changed while hashing')
    return digest.hexdigest()


def signature(names, settings, backend, language):
    source_digest = fingerprint({p.name: file_sha256(p) for p in sorted((ROOT / 'amr_language').glob('*.py'))})
    return fingerprint({'names': names.raw, 'schema': SCHEMA, 'prompt': SYSTEM_PROMPT,
                        'prompt_version': PROMPT_VERSION, 'source_digest': source_digest,
                        'model': backend.name, 'model_file_sha256': backend.file_sha256,
                        'model_manifest_sha256': getattr(backend, 'manifest_sha256', None),
                        'registry_manifest_sha256': getattr(backend, 'registry_manifest_sha256', None),
                        'runner_version': settings.get('runner_version'),
                        'samples': settings['samples'], 'temperature': settings['temperature'],
                        'num_ctx': settings['num_ctx'], 'num_predict': settings['num_predict'],
                        'seed': settings['seed'], 'language_hint': language})


def load_threshold(path, expected_signature):
    path = local_path(path)
    if path.stat().st_size > 1024*1024:
        raise ValueError('Calibration metadata too large')
    value = parse_json(path.read_text())
    if (value.get('source') != 'real' or value.get('alpha') != .1
            or value.get('aggregation') != 'max'
            or value.get('pipeline_signature') != expected_signature):
        raise ValueError('Calibration is not applicable')
    speakers, calibration_rows = [], []
    for split in ('calibration', 'test'):
        record = value[split]
        source = local_path(record['file'])
        if source.stat().st_size > 20*1024*1024 or file_sha256(source) != record['sha256']:
            raise ValueError('Frozen split changed')
        rows = [parse_json(line) for line in source.read_text().splitlines() if line.strip()]
        if not rows or any(row.get('source') != 'real' or not row.get('speaker') for row in rows):
            raise ValueError('Need real speaker-labelled commands')
        found = {row['speaker'] for row in rows}
        if found != set(record['speakers']):
            raise ValueError('Speaker manifest differs')
        speakers.append(found)
        if split == 'calibration':
            for row in rows:
                if set(row['probabilities']) != set(row['truth']):
                    raise ValueError('Need truth for every scored slot')
                calibration_rows.append((row['probabilities'], row['truth']))
    if speakers[0] & speakers[1]:
        raise ValueError('Speakers overlap between calibration and test')
    prediction_set({'validation': 1.}, value['q_hat'])
    if abs(calibrate(calibration_rows)-value['q_hat']) > 1e-12:
        raise ValueError('Threshold differs from frozen calibration scores')
    return value['q_hat']
