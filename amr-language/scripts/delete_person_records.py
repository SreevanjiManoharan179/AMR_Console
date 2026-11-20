#!/usr/bin/env python3
"""Delete private learning and rehearsal records for a requester (no Git rewrite)."""
import argparse
import csv
import io
import json
from pathlib import Path
import shutil
import sys
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_learning import LearningStore,_write,_safe


def delete_person(root,person):
    root=Path(root);store=LearningStore(root);rows=store.rows()
    ids={r['id'] for r in rows if r.get('requester_id')==person or person in r.get('candidate',{}).values()}
    removed=store.prune(person)
    approved=[]
    for directory in (root/'records/learning_versions').glob('*'):
        evidence=directory/'evidence.json'
        if evidence.exists() and json.loads(_safe(evidence).read_text()).get('id') in ids:
            if directory.is_symlink():raise ValueError('Symlink history refused')
            approved.append(json.loads(evidence.read_text()))
            shutil.rmtree(directory)
    # Erase an entire mixed-person session if it contains this person; partial
    # transcript redaction could leave their exact answer or helper name behind.
    for path in list((root/'records/rehearsals').glob('*.jsonl'))+list((root/'records/rehearsals').glob('*.json')):
        _safe(path)
        if path.suffix=='.json':
            value=json.loads(path.read_text());matches=value.get('requester_id')==person
        else:
            data=[json.loads(line) for line in path.read_text().splitlines()]
            matches=any(r.get('requester_id')==person or r.get('result',{}).get('requester')==person or r.get('kind')=='input' and r.get('text')=='/helper_name '+person for r in data)
        if matches:path.unlink();removed+=1
    for row in approved:
        candidate=row['candidate']
        if row['kind'] not in ('alias','office_meaning'):continue
        path=root/'data'/('learned_aliases.json' if row['kind']=='alias' else 'office_meanings.json')
        value=json.loads(_safe(path).read_text())
        if row['kind']=='alias':
            if value['aliases'].get(candidate['alias'])==candidate['target']:value['aliases'].pop(candidate['alias'])
        else:
            value['languages'][candidate['language']]=[r for r in value['languages'][candidate['language']] if r!={'word':candidate['word'],'meaning':candidate['meaning']}]
        import hashlib
        value['version']=value.get('version',0)+1;raw=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
        _write(path,raw);_write(path.with_suffix('.sha256'),(hashlib.sha256(raw).hexdigest()+'\n').encode())
    for path in (root/'records/map_requests').glob('*.jsonl'):
        rows=[json.loads(line) for line in _safe(path).read_text().splitlines()]
        keep=[r for r in rows if r.get('source') in ('SCRIPTED','STARTER_SCRIPTED','STARTER_MEASUREMENT') or not (r.get('who_asked')==person or r.get('person')==person)]
        if len(keep)!=len(rows):
            _write(path,(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in keep)).encode());removed+=len(rows)-len(keep)
    # Later snapshots can contain an earlier person's approved contribution too.
    for directory in (root/'records/learning_versions').glob('*'):
        if directory.is_symlink():raise ValueError('Symlink history refused')
        for path in directory.iterdir():
            if path.name=='evidence.json':continue
            if path.name=='owner_check.csv':
                with _safe(path).open(newline='') as stream:reader=csv.DictReader(stream);fields=reader.fieldnames;items=list(reader)
                keep=[r for r in items if not (r.get('speaker')=='OWNER' and any(i in r.get('id','') for i in ids))]
                if keep!=items:
                    out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader();writer.writerows(keep);_write(path,out.getvalue().encode())
            elif path.name in ('learned_aliases.json','office_meanings.json'):
                value=json.loads(_safe(path).read_text())
                for row in approved:
                    candidate=row['candidate']
                    if path.name=='learned_aliases.json' and row['kind']=='alias' and value['aliases'].get(candidate['alias'])==candidate['target']:value['aliases'].pop(candidate['alias'])
                    if path.name=='office_meanings.json' and row['kind']=='office_meaning':value['languages'][candidate['language']]=[r for r in value['languages'][candidate['language']] if r!={'word':candidate['word'],'meaning':candidate['meaning']}]
                _write(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
    # Approved owner rows and their candidate identifiers are private data as well.
    path=root/'data/owner_check.csv'
    if path.exists():
        with _safe(path).open(newline='') as stream:reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
        keep=[r for r in rows if not (r.get('speaker')=='OWNER' and any(i in r.get('id','') for i in ids))]
        if len(rows)!=len(keep):
            import hashlib
            out=io.StringIO(newline='');w=csv.DictWriter(out,fieldnames=fields);w.writeheader();w.writerows(keep);raw=out.getvalue().encode();_write(path,raw)
            digest=hashlib.sha256(raw).hexdigest();_write(path.with_suffix('.sha256'),(digest+'\n').encode())
            meta=root/'data/owner_check.version.json';version=json.loads(meta.read_text()).get('version',0) if meta.exists() else 0
            _write(meta,json.dumps({'version':version+1,'sha256':digest,'reason':'requested erasure'}).encode())
    return removed


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--requester',required=True);args=parser.parse_args()
    print('Removed records:',delete_person(ROOT,args.requester))
    print('Local private records erased. Existing Git history, exported reports and chat copies are outside this tool.')


if __name__=='__main__':main()
