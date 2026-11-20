#!/usr/bin/env python3
"""Metadata-only dependency lock preparation; never download or install packages."""
import json
from pathlib import Path
import urllib.request

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name,parse_wheel_filename
from packaging.version import Version,InvalidVersion

ROOT=Path(__file__).resolve().parent.parent
DIRECT=ROOT/'records/task3/C_metadata';DIRECT.mkdir(parents=True,exist_ok=True)
METADATA_CACHE=ROOT/'.runtime/task3_metadata_cache';METADATA_CACHE.mkdir(parents=True,exist_ok=True)
PINS={'pip':'25.1.1','setuptools':'80.9.0','wheel':'0.45.1','torch':'2.7.1','transformers':'4.51.3',
      'huggingface-hub':'0.30.2','numpy':'2.2.6','tokenizers':'0.21.1','sentencepiece':'0.2.0',
      'indictranstoolkit':'1.1.1','safetensors':'0.5.3'}
TAGS={tag:index for index,tag in enumerate(sys_tags())}
CACHE={}


def metadata(name,version=None):
    key=name+('/'+version if version else '')
    if key not in CACHE:
        path=METADATA_CACHE/('pypi_'+key.replace('/','_')+'.json')
        if path.exists(): value=json.loads(path.read_text())
        else:
            with urllib.request.urlopen('https://pypi.org/pypi/'+key+'/json',timeout=30) as response: value=json.load(response)
            path.write_text(json.dumps(value,indent=2)+'\n')
        CACHE[key]=value
    return CACHE[key]


def wheel(files):
    candidates=[]
    for entry in files:
        if not entry['filename'].endswith('.whl') or entry.get('yanked'): continue
        try: tags=parse_wheel_filename(entry['filename'])[3]
        except ValueError: continue
        matching=[TAGS[tag] for tag in tags if tag in TAGS]
        if matching: candidates.append((min(matching),entry))
    return min(candidates,key=lambda item:item[0])[1] if candidates else None


def main():
    chosen=dict(PINS)
    for iteration in range(20):
        constraints={name:[] for name in PINS}
        for name,version in list(chosen.items()):
            data=metadata(name,version)
            for raw in data['info'].get('requires_dist') or []:
                req=Requirement(raw)
                if req.marker and not req.marker.evaluate({'extra':''}): continue
                constraints.setdefault(canonicalize_name(req.name),[]).append(str(req.specifier))
        updated={}
        for name,specs in constraints.items():
            combined=SpecifierSet(','.join(s for s in specs if s))
            if name in PINS:
                version=PINS[name]
                if Version(version) not in combined: raise ValueError(f'Pinned conflict: {name} {version} {combined}')
            else:
                data=metadata(name)
                candidates=[]
                for version,files in data['releases'].items():
                    try: v=Version(version)
                    except InvalidVersion: continue
                    if v.is_prerelease or v.is_devrelease or v not in combined: continue
                    eligible=[f for f in files if f.get('upload_time_iso_8601','')<='2026-11-02T23:59:59Z' and not f.get('yanked')]
                    eligible=[f for f in eligible if not f.get('requires_python') or Version('3.12.0') in SpecifierSet(f['requires_python'])]
                    if wheel(eligible): candidates.append(v)
                if not candidates: raise ValueError('No compatible wheel for '+name+' '+str(combined))
                version=str(max(candidates))
            updated[name]=version
        if updated==chosen: break
        chosen=updated
    else: raise RuntimeError('Dependency resolution did not settle')
    artifacts=[]
    for name,version in sorted(chosen.items()):
        data=metadata(name,version);entry=wheel(data['urls'])
        if entry is None: raise ValueError('No wheel '+name)
        artifacts.append({'name':name,'version':version,'filename':entry['filename'],'bytes':entry['size'],
                          'sha256':entry['digests']['sha256'],'url':entry['url']})
    target=ROOT/'data/indictrans2_C_requirements.lock'
    target.write_text('# CPython 3.12 Linux x86_64; metadata-only lock, not installed\n'+''.join(
        f"{a['name']}=={a['version']} --hash=sha256:{a['sha256']}\n" for a in artifacts))
    (DIRECT/'artifact_manifest.json').write_text(json.dumps({'status':'NOT_INSTALLED_WAITING_FOR_APPROVED_C',
      'method':'Public PyPI metadata only, all transitive dependencies pinned; no wheel downloaded',
      'total_wheel_bytes':sum(a['bytes'] for a in artifacts),'artifacts':artifacts},indent=2)+'\n')
    print('Prepared pinned lock:',len(artifacts),'wheels;',sum(a['bytes'] for a in artifacts),'bytes; nothing installed')


if __name__=='__main__': main()
