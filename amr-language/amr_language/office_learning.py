"""Owner-reviewed data suggestions. Never edits names, maps, grammar or weights."""
import csv
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import uuid
from .calibration import ROOT

KINDS={'alias','office_meaning','owner_row','person_place'}
FIELDS={'typed_text','requester_id','language','readback','decisions','outcome'}
RECORDS_ENV='AMR_LANGUAGE_RECORDS_DIR'


def records_dir(root=None):
    """The folder this module writes its records to.

    An explicit root means <root>/records. Without one: $AMR_LANGUAGE_RECORDS_DIR when it is set
    (an absolute path; automated tests set it so that they never write into this module's own
    records/), otherwise <module>/records."""
    if root is not None:return Path(root).resolve()/'records'
    override=os.environ.get(RECORDS_ENV)
    if not override:return ROOT/'records'
    if not os.path.isabs(override):raise ValueError(RECORDS_ENV+' must be an absolute path')
    return Path(override).resolve()


def _safe(path):
    path=Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('Symlink data path refused')
    if path.exists():
        st=path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:raise ValueError('Data path must be a regular file with one link')
    return path


def _write(path,raw):
    path=_safe(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()


class LearningStore:
    def __init__(self,root=None,retention_days=30):
        self.root=Path(root).resolve() if root is not None else ROOT
        self.directory=records_dir(root)/'learning_suggestions'
        if type(retention_days) is not int or not 1<=retention_days<=3650:raise ValueError('Invalid retention')
        self.retention_days=retention_days
    def append(self,kind,candidate,*,source='UNREVIEWED_TYPED',**evidence):
        if kind not in KINDS or set(evidence)-FIELDS:raise ValueError('Unsupported suggestion/privacy fields')
        validate_candidate(kind,candidate)
        now=datetime.now(timezone.utc)
        row={'id':uuid.uuid4().hex,'time':now.isoformat(),'kind':kind,'candidate':candidate,'source':source,'status':'pending',**evidence}
        self.directory.mkdir(parents=True,exist_ok=True)
        path=_safe(self.directory/(now.date().isoformat()+'.jsonl'))
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX);os.write(fd,(json.dumps(row,ensure_ascii=False)+'\n').encode())
        finally:os.close(fd)
        return row
    def rows(self):
        result=[]
        for path in sorted(self.directory.glob('*.jsonl')):
            _safe(path)
            result.extend(json.loads(line) for line in path.read_text().splitlines() if line)
        return result
    def pending(self):
        rows=self.rows();reviewed={r['suggestion_id'] for r in rows if r.get('kind')=='review'}
        return [r for r in rows if r.get('kind') in KINDS and r['id'] not in reviewed]
    def review_event(self,row,status,report):
        path=_safe(self.directory/(datetime.now(timezone.utc).date().isoformat()+'.jsonl'))
        event={'id':uuid.uuid4().hex,'time':datetime.now(timezone.utc).isoformat(),'kind':'review','suggestion_id':row['id'],'requester_id':row.get('requester_id'),'status':status,'outcome':report}
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        try:os.write(fd,(json.dumps(event,ensure_ascii=False)+'\n').encode())
        finally:os.close(fd)
    def prune(self,person=None,now=None):
        now=now or datetime.now(timezone.utc);rows=self.rows()
        reviewed={r['suggestion_id'] for r in rows if r.get('kind')=='review'}
        removed=set()
        for r in rows:
            if (person is not None and (r.get('requester_id')==person or person in r.get('candidate',{}).values())) or (person is None and r.get('kind') in KINDS and r['id'] not in reviewed and datetime.fromisoformat(r['time'])<now-timedelta(days=self.retention_days)):removed.add(r['id'])
        for path in self.directory.glob('*.jsonl'):
            keep=[r for r in map(json.loads,path.read_text().splitlines()) if r['id'] not in removed and r.get('suggestion_id') not in removed]
            _write(path,(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in keep)).encode())
        return len(removed)


def validate_candidate(kind,candidate):
    if not isinstance(candidate,dict):raise ValueError('Candidate must be data')
    keys={'alias':{'alias','target'},'office_meaning':{'language','word','meaning'},'owner_row':{'language','text','expected_english','expected_steps_notes'},'person_place':{'person','place'}}[kind]
    if set(candidate)!=keys or any(not isinstance(v,str) or not v.strip() or len(v)>3200 for v in candidate.values()):raise ValueError('Invalid candidate data')
    if kind in ('office_meaning','owner_row') and candidate['language'] not in ('en','ta','ml','te'):raise ValueError('Invalid language')


def load_aliases(root=ROOT):
    path=Path(root)/'data/learned_aliases.json'
    if not path.exists():return {}
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=(path.with_suffix('.sha256')).read_text().strip():raise ValueError('Learned aliases hash mismatch')
    data=json.loads(raw)
    if type(data.get('version')) is not int or not isinstance(data.get('aliases'),dict):raise ValueError('Invalid learned aliases')
    return data['aliases']


def corpus(root=ROOT):
    root=Path(root);cases=[]
    with (root/'data/owner_check.csv').open(newline='') as stream:
        for row in csv.DictReader(stream):
            # Starter rows plus explicitly owner-confirmed appended rows only.
            if row['id'].startswith('STARTER-') or row.get('speaker')=='OWNER' and row.get('notes','').startswith('OWNER_CONFIRMED:'):
                cases.append({'id':row['id'],'text':row['text'],'language':row['language']})
    for filename in ('grammar_holdout.csv','grammar_holdout_v2.csv'):
        with (root/'data'/filename).open(newline='') as stream:
            cases.extend({'id':filename+':'+r['id'],'text':r['text'],'language':'en'} for r in csv.DictReader(stream))
    if sum(r['id'].startswith('STARTER-') for r in cases)!=70:raise ValueError('All 70 starters required')
    return cases



def data_stamp(root):
    paths=('data/learned_aliases.json','data/learned_aliases.sha256','data/office_meanings.json','data/office_meanings.sha256','data/owner_check.csv','data/grammar_holdout.csv','data/grammar_holdout_v2.csv')
    return {p:hashlib.sha256(_safe(Path(root)/p).read_bytes()).hexdigest() for p in paths}


def candidate_stamp(kind,candidate):
    return hashlib.sha256(json.dumps([kind,candidate],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def regression(candidate,kind,names,settings,*,root=ROOT,evaluate=None):
    """Full baseline/candidate pipeline. No change is accepted on unmeasured cases."""
    from .office import understand,_agreement_key
    from .office_backend import LocalOfficeBackend,make_backend,settings_for_office
    from .office_names import OfficeNames
    live=evaluate is None
    evaluate=evaluate or understand
    backend=make_backend(settings_for_office(settings)) if live else None
    def probe():
        if not isinstance(backend,LocalOfficeBackend):return True
        try:
            value=backend.client._request('POST','/api/generate',{'model':backend.name,'prompt':'Reply hello.','stream':False,'think':False,'keep_alive':'5m','options':{'temperature':0.,'num_ctx':4096,'num_predict':16}},settings.get('port',11434),60.)
            return value.get('done') is True and bool(value.get('response','').strip())
        except Exception:return False
    validate_candidate(kind,candidate)
    stamp=data_stamp(root)
    base_aliases=load_aliases(root);new_aliases=dict(base_aliases)
    if kind=='alias':
        roster=OfficeNames(names,'Bharathwaj')
        if not roster.known(candidate['target']):raise ValueError('Alias target must already be approved')
        new_aliases[candidate['alias']]=candidate['target']
    failures=[];measurements=[];not_measured=[]
    for case in corpus(root):
        outputs=[]
        for aliases in (base_aliases,new_aliases):
            context=dict(names);context['review_aliases']=aliases
            config={**settings,'mode':'measurement','input_source':'SCRIPTED','review_only':True}
            if kind=='office_meaning':
                config['office_meanings']=True
                config['review_meaning']=candidate if aliases is new_aliases else None
            if not probe():
                outputs.append(None);continue
            value=evaluate(case['text'],context,'Bharathwaj',case['language'],config)
            post_ok=probe()
            if (value.get('refuse') or {}).get('reason') in ('model_timeout','model_unavailable','invalid_context') or not post_ok:value=None
            outputs.append(value)
        before,after=outputs
        if before is None or after is None:
            not_measured.append(case['id']);measurements.append({'id':case['id'],'status':'not_measured'});continue
        changed=_agreement_key({'steps':before.get('steps'),'queued':before.get('queued_parts'),'omissions':before.get('omissions')})!=_agreement_key({'steps':after.get('steps'),'queued':after.get('queued_parts'),'omissions':after.get('omissions')})
        failed=before['decision']=='act' and (after['decision']!='act' or changed) or before['decision']=='refuse' and after['decision']=='act'
        if failed:failures.append(case['id'])
        measurements.append({'id':case['id'],'before':before['decision'],'after':after['decision'],'changed_plan':changed})
    if stamp!=data_stamp(root):raise ValueError('Regression data changed during measurement')
    return {'data_stamp':stamp,'candidate_stamp':candidate_stamp(kind,candidate),'passed':not failures and not not_measured,'failures':failures,'not_measured':not_measured,'measured':len(measurements)-len(not_measured),'cases':measurements,'oracle':'preserve previous accepted behavior; not a native accuracy rating'}


def accept(store,row,report,*,owner_confirmed=False):
    if not owner_confirmed:raise ValueError('Owner approval required')
    if not report.get('passed') or report.get('measured',0)!=len(corpus(store.root)):raise ValueError('Full regression must pass before acceptance')
    kind=row['kind'];candidate=row['candidate'];validate_candidate(kind,candidate)
    if report.get('data_stamp')!=data_stamp(store.root) or report.get('candidate_stamp')!=candidate_stamp(kind,candidate):raise ValueError('Candidate/data changed since regression; measure again')
    if kind=='person_place':
        store.review_event(row,'approved_for_console_review',report);return # Never edits names/map.
    data_dir=store.root/'data'
    if kind=='owner_row':
        path=_safe(data_dir/'owner_check.csv')
        with path.open(newline='') as stream:reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
        rows.append({'id':'OWNER-'+row['id'],'speaker':'OWNER',**candidate,'notes':'OWNER_CONFIRMED:'+row['id']})
        out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader();writer.writerows(rows);raw=out.getvalue().encode()
        meta_path=data_dir/'owner_check.version.json';version=json.loads(meta_path.read_text()).get('version',0) if meta_path.exists() else 0
    else:
        path=data_dir/('learned_aliases.json' if kind=='alias' else 'office_meanings.json')
        data=json.loads(path.read_text());version=data.get('version',0)
        if kind=='alias':data['aliases'][candidate['alias']]=candidate['target']
        else:data['languages'][candidate['language']].append({'word':candidate['word'],'meaning':candidate['meaning']})
        data['version']=version+1;raw=(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode();meta_path=None
    # Keep versioned data snapshots (private, out of Git), including approval evidence.
    history=store.directory.parent/'learning_versions'/row['id'];history.mkdir(parents=True,exist_ok=False)
    _write(history/path.name,raw)
    _write(history/'evidence.json',json.dumps(row,ensure_ascii=False).encode())
    _write(path,raw);digest=hashlib.sha256(raw).hexdigest()
    _write(path.with_suffix('.sha256'),(digest+'\n').encode())
    if meta_path:_write(meta_path,json.dumps({'version':version+1,'sha256':digest}).encode())
    store.review_event(row,'approved',{'sha256':digest,'version':version+1,**report})


def record_feedback(text,requester_id,language,readback,decision,outcome,*,root=None,retention_days=30):
    """Console callback after real confirmation/outcome; creates a pending row only."""
    if decision not in ('yes','no','not_asked','unclear'):raise ValueError('Invalid requester decision')
    store=LearningStore(root,retention_days);store.prune()
    return store.append('owner_row',{'language':language,'text':text,'expected_english':text,'expected_steps_notes':'Owner must supply/confirm intended English and steps before approval'},requester_id=requester_id,language=language,typed_text=text,readback=readback,decisions={'requester_reply':decision},outcome=outcome)
