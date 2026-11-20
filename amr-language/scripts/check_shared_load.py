#!/usr/bin/env python3
"""Owner-operated shared-load measurement. Never controls console/ROS/navigation.

Only this file, its tests and measurement evidence are new; v0.4.3 stays frozen.
Read-only nvidia-smi/gz clients are bounded; no console PID is ever signalled.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
# Direct `python3 scripts/...` execution starts with scripts/ on sys.path.
# Load the existing local package; do not require an install or PYTHONPATH.
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
DEV=ROOT.parent/'amr-console-dev'
CONSOLES=(ROOT.parent/'amr-console',DEV)
PHASES=('P0','P1','P2','P3','P4')
SELECTED=tuple(f'STARTER-{lang}-{n:02}' for lang in ('en','ta','ml','te') for n in (1,2,3,6,10))
BASELINE=ROOT/'records/task4b/B_t07_complete_2026-11-03/requests.jsonl'
PROMPTS={
 'P0':'P0: Open ~/AMR_Console/amr-console-dev with bash setup/run_console.sh. Leave the console open with NO session. Close any model runner you own yourself before this campaign. Press Enter to sample the baseline.',
 'P1':'P1: Start proposed_system navigation with ground-truth recording ON. Keep the session running for P2 and P3. Enter its run ID below. Sampling starts before you send the two typed commands.',
 'P2':'P2: B is loaded/warmed outside timing. Use the same session/settings and starting pose as P1. Send "go to the cafeteria". When the robot starts driving, press Enter to start the first ten model requests. After arrival send "go home".',
 'P3':'P3: Keep the navigation session open. A long Tamil request will start. About one second later the script will print PRESS STOP NOW. Click STOP in the console yourself, then immediately press Enter here. The script never sends STOP.',
 'P4':'P4 optional: Start a new proposed_system navigation session with ground-truth recording ON (P3 stopped the previous session). B plus CPU-only C will be warmed outside timing. Repeat "go to the cafeteria", then "go home".',
}


def stamp():return datetime.now(ZoneInfo('Asia/Kolkata')).isoformat()
def sha(raw):return hashlib.sha256(raw).hexdigest()
def number(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Expected a finite nonnegative measurement')
    return value

def percentile(values,q=.95):return sorted(values)[max(0,math.ceil(q*len(values))-1)] if values else None

def stats(values):
    values=[number(v) for v in values]
    return {'n':len(values),'p50':statistics.median(values) if values else None,'p95':percentile(values)}


def frozen_manifest():
    """Read the tag, never checkout/modify the language package or old scripts."""
    names=subprocess.check_output(['git','ls-tree','-r','--name-only','v0.4.3'],cwd=ROOT,text=True).splitlines()
    files=[p for p in names if p.startswith(('amr_language/','data/','scripts/','tests/')) or p=='pyproject.toml']
    result={}
    for name in files:
        expected=subprocess.check_output(['git','show','v0.4.3:'+name],cwd=ROOT)
        actual=(ROOT/name).read_bytes()
        if sha(expected)!=sha(actual):raise ValueError('Frozen v0.4.3 file changed: '+name)
        result[name]=sha(actual)
    return result


class Archive:
    def __init__(self,path,*,root=ROOT,create=True):
        root=Path(root).resolve();path=Path(path)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('Symlink output refused')
        self.path=path.resolve()
        if self.path.parent!=root/'records' or not re.fullmatch(r'shared_load_[A-Za-z0-9_-]+',self.path.name):raise ValueError('Output must be a new records/shared_load_<date> folder')
        if create:self.path.mkdir(parents=True,exist_ok=False)
        elif not self.path.is_dir():raise ValueError('Missing measurement archive')
    def target(self,name):
        path=self.path/name
        if path.is_symlink() or not path.resolve().is_relative_to(self.path):raise ValueError('Archive path escaped')
        if path.exists() and path.stat().st_nlink!=1:raise ValueError('Hardlinked output refused')
        return path
    def write(self,name,value):
        path=self.target(name);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    def append(self,name,value):
        path=self.target(name);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a') as stream:stream.write(json.dumps(value,ensure_ascii=False)+'\n')


class ConsoleRecords:
    """Only run records/audits may be read. Paths from record contents are ignored."""
    def __init__(self,root=DEV,*,trusted_roots=CONSOLES):
        self.root=Path(root).resolve()
        if self.root not in [Path(p).resolve() for p in trusted_roots]:raise ValueError('Unapproved console root')
    def read(self,relative):
        relative=Path(relative);parts=relative.parts
        command=len(parts)==5 and parts[:2]==('data','runs') and parts[3]=='commands' and parts[4].endswith('.json')
        audit=len(parts)==5 and parts[:3]==('data','logs','console') and parts[4] in ('run.json','cleanup.json','cleanup_recheck.json')
        if not command and not audit:raise ValueError('Only command/run/cleanup records may be read')
        if any(p in ('.','..') for p in parts):raise ValueError('Traversal refused')
        path=self.root/relative
        if not path.resolve().is_relative_to(self.root) or path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=self.root):raise ValueError('Console record symlink escaped')
        raw=path.read_bytes()
        return {'path':str(relative),'sha256':sha(raw),'mtime_ns':path.stat().st_mtime_ns,'record':json.loads(raw)}
    @staticmethod
    def run_id(value):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',value):raise ValueError('Invalid run ID')
        return value
    def session(self,run_id,active=True):
        run_id=self.run_id(run_id);value=self.read(f'data/logs/console/{run_id}/run.json');row=value['record']
        if row.get('run_id')!=run_id or row.get('action_id')!='nav.start' or row.get('params',{}).get('map')!='proposed_system':raise ValueError('Select a proposed_system navigation run')
        if 'ground_truth:=true' not in row.get('argv',[]) and row.get('params',{}).get('ground_truth') is not True:raise ValueError('Ground-truth recording must be on')
        if active and (row.get('end_at') is not None or row.get('status') in ('stopped','error','finished','completed')):raise ValueError('Session already ended; select the current active run')
        partition=row.get('env',{}).get('GZ_PARTITION')
        if not isinstance(partition,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}',partition):raise ValueError('Session has no safe GZ_PARTITION')
        return value
    def commands(self,run_id):
        run_id=self.run_id(run_id);base=self.root/'data/runs'/run_id/'commands'
        result={}
        for path in sorted(base.glob('*.json')):
            try:value=self.read(path.relative_to(self.root));result[value['path']]=value
            except (OSError,ValueError):continue # An active file may not be fully written yet.
        return result
    def cleanup(self,run_id,before):
        result={}
        for name in ('cleanup.json','cleanup_recheck.json'):
            try:
                value=self.read(f'data/logs/console/{self.run_id(run_id)}/{name}')
                if before.get(name)==value['sha256']:value['stale']=True
                result[name]=value
            except (OSError,ValueError) as error:result[name]={'error':type(error).__name__}
        return result


def parse_navigation(value):
    row=value['record'];text=' '.join(row.get('text','').casefold().strip(' .!?').split())
    target={'go to the cafeteria':'cafeteria','go to cafeteria':'cafeteria','go home':'home'}.get(text)
    if not target or row.get('parsed_json',{}).get('location')!=target:raise ValueError('Not one of the two typed goals')
    if row.get('command_map')!='proposed_system':raise ValueError('Wrong map')
    nav=row.get('nav2',{});wall=number(nav.get('navigation_wall_s'));error=number(row.get('true_final_error_m'))
    before=row.get('truth_before',{});after=row.get('truth_after',{})
    if before.get('source')!='/amr/ground_truth' or after.get('source')!='/amr/ground_truth':raise ValueError('Ground truth absent')
    lo=number(before.get('received_monotonic'));hi=number(after.get('received_monotonic'))
    if hi<=lo:raise ValueError('Invalid ground-truth clock interval')
    # Exact nav2 start/end aren't recorded; use the enclosing truth sample interval,
    # retain its size and label this approximation rather than inventing timestamps.
    return {'target':target,'navigation_wall_s':wall,'true_final_error_m':error,'status':nav.get('status'),
      'success':nav.get('status')=='SUCCEEDED','interval':[lo,hi],'interval_source':'enclosing ground-truth samples',
      'start_xy':[before.get('x'),before.get('y')],'map_sha256':row.get('map_sha256'),'locations_sha256':row.get('locations_sha256'),
      'partition':row.get('transport',{}).get('GZ_PARTITION'),'evidence':value}


def phase_navigation(before,after,partition):
    records=[];errors=[];failures=[]
    for path,value in after.items():
        if path in before:continue # A rewritten old record is still not a new trial.
        raw=value['record'];status=raw.get('nav2',{}).get('status')
        if status in ('FAILED','ABORTED','CANCELED','CANCELLED','REJECTED','ERROR','TIMEOUT'):
            failures.append({'target':raw.get('parsed_json',{}).get('location'),'status':status,'evidence':value})
        try:
            row=parse_navigation(value)
            if row['partition']!=partition:raise ValueError('Wrong session partition')
            records.append(row)
        except (TypeError,ValueError) as exc:errors.append({'path':path,'error':str(exc),'evidence':value})
    goals={g:[r for r in records if r['target']==g] for g in ('cafeteria','home')}
    complete=all(len(v)==1 for v in goals.values())
    if complete and goals['home'][0]['interval'][0]<goals['cafeteria'][0]['interval'][1]:complete=False;errors.append({'error':'Goals not in cafeteria-then-home order'})
    return {'status':'measured' if complete else 'not_measured','commands':records,'errors':errors,'failures':failures,
            'reason':None if complete else 'Need exactly one new ground-truth command for each goal, in order'}


def overlap(start,end,intervals):
    if end<=start:return 0.
    spans=sorted((max(start,a),min(end,b)) for a,b in intervals if b>start and a<end)
    total=0.;last=start
    for a,b in spans:
        a=max(a,last)
        if b>a:total+=b-a
        last=max(last,b)
    return min(1.,total/(end-start))


def parse_csv(text,fields):
    result=[]
    for values in csv.reader(io.StringIO(text)):
        if not values or not any(v.strip() for v in values):continue
        if len(values)!=len(fields):raise ValueError('Unexpected nvidia-smi CSV columns')
        row={}
        for key,value in zip(fields,values):
            value=value.strip()
            if key in ('pid','gpu_index'):row[key]=int(value)
            elif key.endswith(('_mib','_percent')):row[key]=None if value in ('N/A','[N/A]','-','Not Supported') else number(float(value))
            else:row[key]=value
        result.append(row)
    return result


def parse_pmon(text):
    header=None;rows=[]
    for line in text.splitlines():
        fields=line.split()
        if line.startswith('#'):
            if 'pid' in fields and 'fb' in fields:header=fields[1:]
            continue
        if not fields or not header or len(fields)<len(header):continue
        row=dict(zip(header,fields))
        if row.get('pid','-').isdigit() and row.get('fb','-').isdigit():
            rows.append({'gpu_index':int(row['gpu']),'pid':int(row['pid']),'type':row['type'],'used_mib':int(row['fb']),'process_name':row.get('command','unknown')})
    return rows


def parse_rtf(text):
    match=re.search(r'\breal_time_factor\s*:\s*([0-9.eE+-]+)',text)
    return number(float(match[1])) if match else None


def read_command(argv,timeout=2.,env=None):
    # These are fresh read-only measurement clients, never session/simulation PIDs.
    try:
        value=subprocess.run(argv,capture_output=True,text=True,timeout=timeout,env=env,check=False)
        return {'argv':argv,'returncode':value.returncode,'stdout':value.stdout,'stderr':value.stderr[:1000]}
    except (OSError,subprocess.TimeoutExpired) as error:return {'argv':argv,'error':type(error).__name__}


def proc_sample(previous,now,tracked=()):
    mem={k:int(v.strip().split()[0]) for k,v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())}
    values=[int(s) for s in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    total=sum(values);idle=values[3]+values[4];old=previous.get('cpu')
    cpu=100*(1-(idle-old[1])/(total-old[0])) if old and total>old[0] else None
    processes=[];ticks=os.sysconf('SC_CLK_TCK');page=os.sysconf('SC_PAGE_SIZE');new={}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            text=(path/'stat').read_text();name=text[text.index('(')+1:text.rindex(')')];parts=text[text.rindex(')')+2:].split();pid=int(path.name)
            if pid not in tracked and not re.search(r'gz|rviz|ollama|llama|nav2|controller|planner|amcl|map_server|bt_navigator',name,re.I):continue
            usage=int(parts[11])+int(parts[12]);identity=(pid,int(parts[19]));oldp=previous.get('processes',{}).get(identity)
            percent=100*(usage-oldp)/(ticks*(now-previous['time'])) if oldp is not None and now>previous['time'] else None
            processes.append({'pid':pid,'process_name':name,'rss_kib':max(0,int(parts[21])*page//1024),'cpu_percent_one_core':percent})
            new[identity]=usage
        except (OSError,ValueError,IndexError):continue
    return {'cpu_percent_host':cpu,'ram_total_kib':mem['MemTotal'],'ram_available_kib':mem['MemAvailable'],'processes':processes},{'cpu':(total,idle),'processes':new,'time':now}


class Sampler:
    def __init__(self,archive,phase,api_ps=False,partition=None):
        self.archive=archive;self.phase=phase;self.api_ps=api_ps;self.partition=partition
        self.stop_event=threading.Event();self.tracked={};self.threads=[];self.rows=[];self.graphics=[];self.rtf=[]
    def start(self):
        for method in (self.fast,self.slow):
            thread=threading.Thread(target=method,daemon=True);thread.start();self.threads.append(thread)
        return self
    def track(self,kind,pid):self.tracked[kind]=pid
    def fast(self):
        previous={}
        while not self.stop_event.is_set():
            tick=time.monotonic();row={'time':stamp(),'monotonic_s':tick}
            for key,fields,argv in (
              ('gpu',('gpu_index','uuid','total_mib','used_mib','free_mib','utilization_percent'),['nvidia-smi','--query-gpu=index,uuid,memory.total,memory.used,memory.free,utilization.gpu','--format=csv,noheader,nounits']),
              ('compute',('pid','process_name','used_mib'),['nvidia-smi','--query-compute-apps=pid,process_name,used_gpu_memory','--format=csv,noheader,nounits'])):
                raw=read_command(argv,timeout=.8);row[key+'_raw']=raw
                try:row[key]=parse_csv(raw['stdout'],fields) if raw.get('returncode')==0 else []
                except (ValueError,KeyError):row[key]=[];row[key+'_parse_error']=True
            try:
                host,previous=proc_sample(previous,tick,tuple(self.tracked.values())+tuple(p['pid'] for p in row['compute'])+tuple(p['pid'] for p in (self.graphics[-1]['processes'] if self.graphics else [])));row.update(host)
            except (OSError,ValueError,KeyError):row['host_error']=True
            if self.api_ps:
                try:
                    from runner_control import request
                    row['api_ps']=request('/api/ps',timeout=.5)[0]
                except Exception as error:row['api_ps_error']=type(error).__name__
            row['tracked_pids']=dict(self.tracked);row['sample_wall_s']=time.monotonic()-tick
            self.rows.append(row);self.archive.append(self.phase+'/resources.jsonl',row)
            self.stop_event.wait(max(0.,.2-(time.monotonic()-tick)))
    def slow(self):
        topic=None;gz=shutil.which('gz');env=os.environ.copy()
        if self.partition and gz:
            env['GZ_PARTITION']=self.partition
            raw=read_command([gz,'topic','-l'],env=env)
            topics=[line.strip() for line in raw.get('stdout','').splitlines() if re.fullmatch(r'/world/[A-Za-z0-9_-]+/stats',line.strip())]
            if len(topics)==1:topic=topics[0]
            self.archive.append(self.phase+'/rtf.jsonl',{'kind':'discovery','partition':self.partition,'raw':raw,'selected_topic':topic})
        while not self.stop_event.is_set():
            tick=time.monotonic();raw=read_command(['nvidia-smi','pmon','-s','m','-c','1'])
            row={'monotonic_s':tick,'raw':raw,'processes':parse_pmon(raw.get('stdout',''))}
            self.graphics.append(row);self.archive.append(self.phase+'/graphics_processes.jsonl',row)
            if topic:
                raw=read_command([gz,'topic','-e','-n','1','-t',topic],env=env)
                try:value=parse_rtf(raw.get('stdout','')) if raw.get('returncode')==0 else None
                except ValueError:value=None
                record={'monotonic_s':time.monotonic(),'rtf':value,'partition':self.partition,'topic':topic,'source':'gz_stats','raw':raw}
                self.rtf.append(record);self.archive.append(self.phase+'/rtf.jsonl',record)
            self.stop_event.wait(max(0.,1.-(time.monotonic()-tick)))
    def stop(self):
        self.stop_event.set()
        for thread in self.threads:thread.join(timeout=8.)
        if any(t.is_alive() for t in self.threads):raise RuntimeError('Measurement client thread failed to finish')
        gaps=[b['monotonic_s']-a['monotonic_s'] for a,b in zip(self.rows,self.rows[1:])]
        return {'samples':len(self.rows),'target_interval_s':.2,'actual_interval_s':stats(gaps),'overruns':sum(r['sample_wall_s']>.2 for r in self.rows)}


def cleanup_pass(evidence):
    initial=evidence.get('cleanup.json',{});recheck=evidence.get('cleanup_recheck.json',{})
    if initial.get('stale') or recheck.get('stale') or 'record' not in initial or 'record' not in recheck:return None
    a=initial['record'];b=recheck['record'];audit=a.get('audit',{})
    return (a.get('requested_stop') is True and a.get('clean') is True and a.get('forced') is False and a.get('bond_abort') is False
      and a.get('survivors')==0 and b.get('clean') is True and b.get('forced') is False and b.get('survivors')==0
      and b.get('bond_abort') is False and b.get('simulation_lock_held') is False and audit.get('clean') is True
      and audit.get('post_inventory_complete') is True and audit.get('isolation_pass') is True and audit.get('window_close_complete') is True
      and not audit.get('application_crashes') and not audit.get('abnormal_process_exits') and not audit.get('live_owned_survivors') and not audit.get('live_unowned_survivors'))


def load_cases():
    with (ROOT/'data/owner_check.csv').open(newline='') as stream:rows={r['id']:r for r in csv.DictReader(stream) if r.get('speaker','').startswith('STARTER') and r['id'].startswith('STARTER-')}
    if len(rows)!=70:raise ValueError('Expected 70 unchanged starters')
    return [rows[ident] for ident in SELECTED]


def request_settings(settings,backend,trace=None):
    from amr_language.office_backend import LocalOfficeBackend,CrossSystemOfficeBackend
    config=dict(settings)
    # The frozen API resolves/caches its local backend itself. Only its explicit
    # mock and cross-system adapters may be supplied through settings['backend'].
    if backend is not None and (isinstance(backend,CrossSystemOfficeBackend) or not isinstance(backend,LocalOfficeBackend)):config['backend']=backend
    if trace is not None:config['trace']=trace
    return config


def measure_request(row,names,settings,runner,backend,c_worker=None):
    from amr_language import understand
    attempts=[]
    for index in range(2):
        pre=runner.probe(timeout=60);cpre=c_worker.probe(60) if c_worker else {'ok':True}
        if not pre.get('ok') or not cpre.get('ok'):
            trial={'attempt':index,'status':'not_measured','reason':'pre-generation liveness','pre':pre,'C_pre':cpre}
        else:
            trace=[];started=time.monotonic();begun=time.time_ns();error=None
            try:result=understand(row['text'],names,'Bharathwaj',row['language'],request_settings(settings,backend,trace))
            except Exception as exc:result=None;error=type(exc).__name__
            end=time.monotonic();post=runner.probe(timeout=60);cpost=c_worker.probe(60) if c_worker else {'ok':True}
            reason=(result or {}).get('refuse') or {}
            good=result is not None and not error and post.get('ok') and cpost.get('ok') and reason.get('reason') not in ('model_timeout','model_unavailable','invalid_context')
            trial={'attempt':index,'status':'measured' if good else 'not_measured','request_start_ns':begun,'start':started,'end':end,'wall_ms':(end-started)*1000,
                'result':result,'trace':trace,'pre':pre,'post':post,'C_pre':cpre,'C_post':cpost,'error':error,'reason':None if good else 'transport/safety/context/liveness failure'}
        attempts.append(trial)
        if trial['status']=='measured':break
        if trial.get('result') and (trial['result'].get('refuse') or {}).get('reason')=='invalid_context':break
        # Never abort in-flight calls; only recover our runner between completed attempts.
        if index==0:
            runner.restart('shared-load request/liveness failure; one retry')
            if c_worker and not c_worker.probe(60).get('ok'):break
    chosen=attempts[-1]
    return {'id':row['id'],'language':row['language'],'source':'STARTER','attempts':attempts,'status':chosen['status'],
      'start':chosen.get('start'),'end':chosen.get('end'),'wall_ms':chosen.get('wall_ms') if chosen['status']=='measured' else None}


class Campaign:
    def __init__(self,archive,console,input_fn=input,output_fn=print):
        self.archive=archive;self.console=console;self.input=input_fn;self.print=output_fn
        self.phases={p:{'status':'not_measured','reason':'Owner has not run this phase'} for p in PHASES}
        self.runner=None;self.worker=None;self.backend=None;self.settings=None;self.names=None
    def save(self):self.archive.write('phases.json',self.phases)
    def wait_enter(self,message):
        while True:
            if not self.input(message).strip():return
            self.print('This terminal only accepts Enter at this prompt. Send navigation commands in Firefox: AMR Console > Commands > Request. Sampling has not advanced.')
    def enter(self,message):
        while True:
            answer=self.input(message+' [Enter; type skip to leave unmeasured]> ').strip().casefold()
            if answer in ('','skip'):return answer!='skip'
            self.print('Use Enter or skip here. Navigation commands belong in the AMR Console Request box.')
    def select_session(self):
        value=self.input('Current proposed_system run ID (from the console; skip to omit)> ').strip()
        if value=='skip':return None
        return self.console.session(value)
    def baseline(self):
        if not self.enter(PROMPTS['P0']):return
        sampler=Sampler(self.archive,'P0').start()
        try:
            self.wait_enter('Baseline sampling is running. Leave the console idle for at least 10 seconds, then press Enter> ')
        finally:summary=sampler.stop()
        self.phases['P0']={'status':'measured','sampler':summary};self.save()
    def warm_B(self):
        from runner_control import Runner
        from amr_language.office_backend import load_office_profile,prepare_office,make_backend
        import yaml
        self.settings=load_office_profile('office_B');self.settings.update(mode='measurement',input_source='STARTER_MEASUREMENT')
        # These frozen scripted facts make full planner timing reproducible, not a
        # physical tray approval or a console map change. No resulting task is sent.
        self.names=yaml.safe_load((ROOT/'data/task4_names_example.yaml').read_text())
        self.settings['capability_profile']=yaml.safe_load((ROOT/'tests/fixtures/robot_capability.yaml').read_text())
        self.archive.write('benchmark_context.json',{'names_source':'data/task4_names_example.yaml','capability_source':'tests/fixtures/robot_capability.yaml','physical_approval':False,'settings':self.settings})
        self.runner=Runner(self.archive.target('owned_model'),overhead_gib=4)
        self.runner.start();self.runner.load(self.settings['model'])
        prepare=prepare_office(self.settings);self.backend=make_backend(self.settings)
        # Fail before asking the owner to drive if the measurement context is
        # incompatible with the frozen API; restarting B cannot repair context.
        from amr_language.office_backend import settings_for_office
        from amr_language.office_names import OfficeNames
        from amr_language.office_map import MapFacts
        settings_for_office(request_settings(self.settings,self.backend))
        MapFacts(OfficeNames(self.names,'Bharathwaj'))
        self.archive.write('B_startup.json',{'startup':prepare,'allocation':self.runner.allocation(),'warmup':self.runner.probe(60)})
    def navigation(self,phase,session,with_model=False):
        run=session['record']['run_id'];partition=session['record']['env']['GZ_PARTITION'];before=self.console.commands(run)
        sampler=Sampler(self.archive,phase,api_ps=with_model,partition=partition).start()
        if self.runner and self.runner.process:sampler.track('B_runner',self.runner.process.pid)
        if self.worker:sampler.track('C_worker',self.worker.process.pid)
        rows=[];summary=None
        try:
            self.print(PROMPTS[phase])
            self.print('Note at least three Gazebo bottom-bar RTF observations DURING driving, in case automatic stats are unavailable.')
            if with_model:
                self.wait_enter('Send cafeteria now; when the robot is driving press Enter (no language plan is dispatched)> ')
                home_driving=threading.Event()
                def batch():
                    for index,row in enumerate(load_cases()):
                        if index==10:home_driving.wait() # Spread ten requests onto each leg.
                        result=measure_request(row,self.names,self.settings,self.runner,self.backend,self.worker)
                        rows.append(result);self.archive.append(phase+'/requests.jsonl',result)
                        self.print(f"{phase}: {len(rows)}/20 {row['id']} {result['status']} {result['wall_ms']} ms")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(batch)
                    try:
                        self.wait_enter('After cafeteria arrives send "go home". When the robot is returning, press Enter to release the last ten requests> ')
                        home_driving.set()
                        self.wait_enter('When home completes press Enter. All 20 calls will finish; idle tails will be excluded from shared-load scores> ')
                        owner_finished=time.monotonic()
                        future.result() # No five-second/twenty-second abort while measuring.
                    finally:home_driving.set()
            else:
                self.wait_enter('Send "go to the cafeteria", then after arrival "go home". When both finish press Enter> ')
                owner_finished=time.monotonic()
            after=self.console.commands(run);nav=phase_navigation(before,after,partition)
            summary=sampler.stop() # Do not sample an arbitrarily long manual RTF-entry pause.
            intervals=[r['interval'] for r in nav['commands']]
            for row in rows:
                row['navigation_overlap_fraction']=overlap(row['start'],row['end'],intervals) if row.get('start') is not None else None
                row['shared_measured']=row['status']=='measured' and nav['status']=='measured' and row['navigation_overlap_fraction']>=.999999
            automatic=[r['rtf'] for r in sampler.rtf if r.get('rtf') is not None and any(a<=r['monotonic_s']<=b for a,b in intervals)]
            if automatic:rtf={'source':'gz_stats_during_navigation','values':automatic}
            else:
                raw=self.input('RTF could not be read during driving. Type comma-separated Gazebo bottom-bar RTF values observed DURING this phase, or blank for not measured> ')
                try:values=[number(float(v.strip())) for v in raw.split(',') if v.strip()]
                except ValueError:values=[]
                rtf={'source':'owner_typed_during_navigation' if values else 'not_measured','values':values,'raw':raw}
        finally:
            if summary is None:summary=sampler.stop()
        self.phases[phase]={'status':nav['status'],'navigation':nav,'session':session,'requests':rows,'rtf':rtf,'sampler':summary,'owner_finished':owner_finished}
        self.archive.write(phase+'/phase.json',self.phases[phase]);self.save()
    def stop_check(self,session):
        if not self.enter(PROMPTS['P3']):return
        run=session['record']['run_id'];self.console.session(run) # Must still be active.
        old=self.console.cleanup(run,{});before={k:v.get('sha256') for k,v in old.items()}
        row=next(r for r in load_cases() if r['id']=='STARTER-ta-10');sampler=Sampler(self.archive,'P3',api_ps=True).start()
        event=threading.Event();times={}
        def long_request():
            # Mark after preflight to align the one-second instruction with actual understanding.
            pre=self.runner.probe(60)
            if not pre.get('ok'):times['pre_failed']=True;event.set();return {'status':'not_measured','pre':pre}
            from amr_language import understand
            times['start']=time.monotonic();event.set()
            result=understand(row['text'],self.names,'Bharathwaj','ta',self.settings)
            times['end']=time.monotonic()
            return {'status':'model_timeout' if (result.get('refuse') or {}).get('reason')=='model_timeout' else 'completed','result':result,'wall_ms':(times['end']-times['start'])*1000,'post':self.runner.probe(60)}
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(long_request);event.wait()
                if times.get('pre_failed'):result={'status':'not_measured','reason':'B preflight failed'}
                else:
                    time.sleep(max(0.,times['start']+1-time.monotonic()))
                    prompt=time.monotonic();pending_prompt=not future.done() and 'end' not in times;self.print('PRESS STOP NOW in the console, then immediately press Enter here.')
                    self.wait_enter('STOP clicked> ');clicked=time.monotonic();pending_clicked=not future.done() and 'end' not in times
                    self.wait_enter('When console cleanup has completed, press Enter to read its audit> ')
                    long=future.result();audit=self.console.cleanup(run,before)
                    next_row=next(r for r in load_cases() if r['id']=='STARTER-en-01');restarts=self.runner.restarts
                    following=measure_request(next_row,self.names,self.settings,self.runner,self.backend)
                    next_works=following['status']=='measured' and following['attempts'][-1].get('result',{}).get('decision') in ('act','ask')
                    timing_verified=pending_prompt and pending_clicked;cleanup_status=cleanup_pass(audit)
                    complete=timing_verified and cleanup_status is not None and following['status']=='measured'
                    stop_pass=(cleanup_status and next_works and self.runner.restarts==restarts) if complete else None
                    result={'next_request_works':next_works,'status':'measured' if complete else 'not_measured','reason':None if complete else 'STOP overlap, cleanup audit or next-request evidence incomplete','long_request':long,'request_id':row['id'],'times':times,'stop_prompt':prompt,'owner_stop_marker':clicked,
                      'pending_at_prompt':pending_prompt,'pending_at_owner_marker':pending_clicked,'stop_during_B_verified':pending_prompt and pending_clicked,
                      'cleanup':audit,'cleanup_pass':cleanup_pass(audit),'next_request':following,'restarts_for_next':self.runner.restarts-restarts,
                      'pass':stop_pass}
        finally:summary=sampler.stop()
        result['sampler']=summary;self.phases['P3']=result;self.archive.write('P3/phase.json',result);self.save()
    def run(self,skip_baseline=False,skip_navigation=False):
        self.save()
        try:
            if skip_navigation:self.print('Using saved P0 and P1. Keep the same navigation session open at home; continuing with B startup for P2.')
            else:
                if not skip_baseline:self.baseline()
                else:self.print('Using the saved P0 baseline. Keep the current console navigation session open; restarting P1 sampling only.')
                self.print(PROMPTS['P1']);session=self.select_session()
                if session:self.navigation('P1',session)
            if self.enter('Load B for P2? Existing unowned model services will not be stopped'):
                self.warm_B()
                session=self.select_session()
                if session:
                    self.navigation('P2',session,True);self.stop_check(session)
                if self.enter(PROMPTS['P4']+' Run this optional phase?'):
                    from amr_language.office_c_client import CWorker
                    from amr_language.office_backend import CrossSystemOfficeBackend
                    session=self.select_session()
                    if session:
                        self.worker=CWorker('cpu',self.archive.target('C_startup'))
                        self.archive.write('C_startup/ready.json',{'command':self.worker.command,'ready':self.worker.ready,'probe':self.worker.probe(60)})
                        self.backend=CrossSystemOfficeBackend(self.backend,self.worker);self.settings={**self.settings,'draw_policy':'cross_system'}
                        self.navigation('P4',session,True)
        finally:
            if self.worker:self.worker.close()
            if self.runner:self.runner.close()
            self.save()


def read_jsonl(path):
    if not path.exists():return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def phase_summary(archive,phase,data):
    resources=read_jsonl(archive.target(phase+'/resources.jsonl'))
    graphics=read_jsonl(archive.target(phase+'/graphics_processes.jsonl'))
    gpu=[g for row in resources for g in row.get('gpu',[])];ps=[r for r in resources if 'api_ps' in r]
    allocation=[];missing=[]
    for row in ps:
        models=[m for m in row['api_ps'].get('models',[]) if m.get('name')=='qwen3.5:4b-q4_K_M' or m.get('model')=='qwen3.5:4b-q4_K_M']
        if len(models)!=1:missing.append(row['monotonic_s']);continue
        m=models[0];size=m.get('size');vram=m.get('size_vram')
        allocation.append(type(size) is int and size>0 and type(vram) is int and size==vram)
    process={}
    for row in resources:
        for p in row.get('processes',[]):
            k=p['pid'];item=process.setdefault(k,{'pid':k,'name':p['process_name'],'ram_peak_kib':None,'cpu_peak_one_core_percent':None,'gpu_peak_mib':None,'gpu_sources':[]})
            item['ram_peak_kib']=max(item['ram_peak_kib'] or 0,p['rss_kib'])
            if p.get('cpu_percent_one_core') is not None:item['cpu_peak_one_core_percent']=max(item['cpu_peak_one_core_percent'] or 0,p['cpu_percent_one_core'])
    for source,rows in (('compute_query',resources),('pmon_compute_and_graphics',graphics)):
        for row in rows:
            for p in row.get('compute' if source=='compute_query' else 'processes',[]):
                k=p['pid'];item=process.setdefault(k,{'pid':k,'name':p['process_name'],'ram_peak_kib':None,'cpu_peak_one_core_percent':None,'gpu_peak_mib':None,'gpu_sources':[]})
                if p.get('used_mib') is not None:item['gpu_peak_mib']=max(item['gpu_peak_mib'] or 0,p['used_mib'])
                if source not in item['gpu_sources']:item['gpu_sources'].append(source)
    measured=[r for r in data.get('requests',[]) if r['status']=='measured'];shared=[r for r in measured if r.get('shared_measured')]
    ram=[r['ram_total_kib']-r['ram_available_kib'] for r in resources if 'ram_available_kib' in r]
    intervals=[r['interval'] for r in data.get('navigation',{}).get('commands',[])]
    rtf=[r['rtf'] for r in read_jsonl(archive.target(phase+'/rtf.jsonl')) if r.get('rtf') is not None and any(a<=r['monotonic_s']<=b for a,b in intervals)]
    values=data.get('rtf',{}).get('values',[]) if not rtf else rtf
    gpu_values=[g['used_mib'] for g in gpu if g.get('used_mib') is not None]
    c_pids={r['tracked_pids']['C_worker'] for r in resources if r.get('tracked_pids',{}).get('C_worker')}
    return {'C_process_ram_peak_kib':max((p['rss_kib'] for r in resources for p in r.get('processes',[]) if p['pid'] in c_pids),default=None),'status':data.get('status','not_measured'),'resource_samples':len(resources),'sampler':data.get('sampler'),
      'gpu_peak_mib':max((g['used_mib'] for g in gpu if g.get('used_mib') is not None),default=None),
      'gpu_devices':sorted({g['uuid'] for g in gpu}),'gpu_total_mib':sorted({g['total_mib'] for g in gpu if g.get('total_mib') is not None}),
      'gpu_below_7_5_GB':max(gpu_values)<7.5e9/1024**2 if gpu_values and len(gpu_values)==len(gpu) else None,'gpu_below_7_5_GiB':max(gpu_values)<7680 if gpu_values and len(gpu_values)==len(gpu) else None,
      'B_fully_GPU':False if missing or any(v is False for v in allocation) else True if resources and allocation and len(ps)==len(resources) else None,
      'B_ps_samples':len(ps),'B_missing_samples':len(missing),'B_ps_errors':sum('api_ps_error' in r for r in resources),
      'host_used_ram_peak_kib':max(ram,default=None),'cpu_percent_host':stats([r['cpu_percent_host'] for r in resources if r.get('cpu_percent_host') is not None]),
      'processes':list(process.values()),'rtf':stats(values),'rtf_source':data.get('rtf',{}).get('source','not_measured'),
      'requests_not_attempted':max(0,(20 if phase in ('P2','P4') else 0)-len(data.get('requests',[]))),'requests_attempted':len(data.get('requests',[])),'requests_measured':len(measured),'requests_not_measured':len(data.get('requests',[]))-len(measured),
      'requests_fully_within_navigation':len(shared),'requests_idle_or_partial':len(measured)-len(shared),
      'latency_ms_all':stats([r['wall_ms'] for r in measured]),'latency_ms_shared':stats([r['wall_ms'] for r in shared]),
      'within_5s_shared':{'count':sum(r['wall_ms']<=5000 for r in shared),'denominator':len(shared)},
      'within_20s_shared':{'count':sum(r['wall_ms']<=20000 for r in shared),'denominator':len(shared)},
      'within_5s_all':{'count':sum(r['wall_ms']<=5000 for r in measured),'denominator':len(measured)},
      'within_20s_all':{'count':sum(r['wall_ms']<=20000 for r in measured),'denominator':len(measured)}}


def compare_navigation(p1,p2):
    result=[]
    for goal in ('cafeteria','home'):
        a=[r for r in p1.get('navigation',{}).get('commands',[]) if r['target']==goal]
        b=[r for r in p2.get('navigation',{}).get('commands',[]) if r['target']==goal]
        if len(a)!=1 or len(b)!=1:
            known=[r for p in (p1,p2) for r in p.get('navigation',{}).get('failures',[]) if r['target']==goal]
            result.append({'goal':goal,'status':'not_measured','both_succeeded':False if known else None,'known_failures':known});continue
        a=a[0];b=b[0]
        hashes=all(a.get(k) and a[k]==b.get(k) for k in ('map_sha256','locations_sha256'))
        positions=a.get('start_xy',[])+b.get('start_xy',[])
        same_start=len(positions)==4 and all(type(v) in (int,float) and math.isfinite(v) for v in positions) and math.dist(a['start_xy'],b['start_xy'])<=.1
        ratio=b['navigation_wall_s']/a['navigation_wall_s'] if a['navigation_wall_s']>0 else None
        difference=b['true_final_error_m']-a['true_final_error_m']
        result.append({'goal':goal,'status':'measured','same_map_and_locations':hashes,'start_pose_within_0_10m':same_start,
          'P1_s':a['navigation_wall_s'],'P2_s':b['navigation_wall_s'],'time_ratio':ratio,'time_pass':ratio is not None and ratio<=1.15 and a['success'] and b['success'],
          'P1_error_m':a['true_final_error_m'],'P2_error_m':b['true_final_error_m'],'error_change_m':difference,'error_pass':abs(difference)<=.10,'both_succeeded':a['success'] and b['success']})
    return result


def build_summary(archive,baseline_path=BASELINE):
    phases=json.loads(archive.target('phases.json').read_text());summaries={p:phase_summary(archive,p,phases.get(p,{})) for p in PHASES}
    previous=read_jsonl(baseline_path);old={r['id']:r for r in previous if r.get('status')=='measured'}
    ids=[r['id'] for r in phases.get('P2',{}).get('requests',[]) if r.get('shared_measured')]
    matched=[old[i]['request_wall_ms'] for i in ids if i in old and old[i].get('request_wall_ms') is not None]
    standalone=stats(matched);current=summaries['P2']['latency_ms_shared'];ratios={}
    for key in ('p50','p95'):ratios[key]=current[key]/standalone[key] if len(matched)==len(ids) and current[key] is not None and standalone[key] else None
    r1=summaries['P1']['rtf']['p50'];r2=summaries['P2']['rtf']['p50'];rtf_ratio=r2/r1 if r1 and r2 is not None else None
    nav=compare_navigation(phases.get('P1',{}),phases.get('P2',{}));stop=phases.get('P3',{})
    criteria={'B_fully_GPU':summaries['P2']['B_fully_GPU'],'gpu_below_7_5_GB':summaries['P2']['gpu_below_7_5_GB'],
      'rtf_drop_at_most_10_percent':rtf_ratio>=.9 if rtf_ratio is not None else None,
      'navigation_time':False if any(r.get('both_succeeded') is False for r in nav) else all(r.get('time_pass') for r in nav) if all(r['status']=='measured' for r in nav) else None,
      'final_error_change':all(r.get('error_pass') for r in nav) if all(r['status']=='measured' for r in nav) else None,
      'STOP':stop.get('pass') if stop.get('status')=='measured' else None}
    session_rows=[phases.get(p,{}).get('session',{}).get('record',{}) for p in ('P1','P2')]
    same_settings=(session_rows[0].get('params')==session_rows[1].get('params') and session_rows[0].get('commit')==session_rows[1].get('commit')) if all(session_rows) else None
    comparable=same_settings and all(r.get('same_map_and_locations') and r.get('start_pose_within_0_10m') for r in nav) and all(summaries[p]['status']=='measured' for p in ('P1','P2'))
    complete=comparable and summaries['P2']['requests_fully_within_navigation']==20 and all(v is not None for v in criteria.values())
    if complete and all(criteria.values()):recommendation='Proposed shared-load lines passed in this run. B is a candidate for shared operation in this tested configuration; repeat trials before calling it safe.'
    elif any(v is False for v in criteria.values()):recommendation='Shared operation failed at least one proposed line. Prefer B while the robot is idle until the failed line is resolved and rechecked. CPU C is optional and is not a standalone substitute for the B planner.'
    else:recommendation='Not enough shared-load evidence. Keep language and navigation unconnected; use B previews while idle until the owner completes the measured phases. No safe-sharing conclusion is established.'
    return {'generated_at':stamp(),'phases':summaries,'navigation_comparison':nav,'rtf_P2_over_P1':rtf_ratio,'criteria':criteria,'complete_shared_test':complete,'same_console_session_settings_and_commit':same_settings,
      'standalone_baseline':{'path':str(baseline_path.relative_to(ROOT)) if baseline_path.is_relative_to(ROOT) else str(baseline_path),'sha256':sha(baseline_path.read_bytes()),'same_ids':standalone,'all_starter_ms':stats([r['request_wall_ms'] for r in old.values() if r.get('request_wall_ms') is not None]),'P2_over_standalone':ratios},
      'recommendation':recommendation,'units':'GPU MiB; strict 7.5 decimal GB = 7152.56 MiB used for the proposed line; 7.5 GiB = 7680 MiB result is also retained in summary.json','repetition':'One outward/return pair per phase is descriptive, not a statistically established degradation bound.'}


def render_report(summary,archive):
    def value(v):return 'not measured' if v is None else str(round(v,4)) if isinstance(v,float) else str(v)
    lines=['# Shared-load check — frozen v0.4.3','',f"Generated {summary['generated_at']}. Raw archive: `{archive.path.relative_to(ROOT)}`.",'',
      '[checked] Measurement script only. Language behavior remains v0.4.3. Both console folders are read-only; no console/ROS/Gazebo session was started, stopped or signalled by this script. Only owned B/C workers are managed. No installs or task dispatch.', '',
      '## Owner operation','',
      f'`python3 scripts/check_shared_load.py --run {archive.path.relative_to(ROOT)}` starts the prepared, unrun campaign. If this archive was already run, use a new `--output records/shared_load_<date>_retry` folder. Regenerate with `--report <archive>` afterwards.','',
      'Confirmation prompts accept only an empty Enter; navigation text stays in Firefox’s AMR Console Commands > Request field. To retry an interrupted pre-B P1 while preserving P0, use `--continue-p1 <old-archive> --output <new-archive>`; raw P0 files are copied with hashes, the old archive stays intact, and no old P1 samples count. If B startup failed before a runner was created after a successful P1, `--continue-p2 <old-archive> --output <new-archive>` preserves both P0 and P1. P0: open the dev console without a session. P1: start proposed_system with ground truth on, enter the run ID, send cafeteria then home. P2: B warms outside timing; repeat both goals while 20 requests run, pressing Enter when each leg starts (ten requests per leg). P3: click console STOP yourself about one second into the long Tamil request, then confirm cleanup. P4 is optional: start a new session, repeat both goals with B plus isolated CPU C. Type observed driving RTF if automatic read-only stats are unavailable. The script prints detailed prompts.','',
      'The 20 fixed starters are 01/02/03/06/10 for en/ta/ml/te, including short and long requests. Names and numeric capability inputs are frozen SCRIPTED fixtures, solely for full-planner load; they do not authorize physical payloads. No output plan is dispatched. P4 uses the frozen cross-system mode (one C translation plus B greedy), so its draw policy differs from P2 adaptive B. C, when selected, runs only in `.runtime/indictrans2-C/venv/bin/python -I`, on CPU.','',
      '## Phases','', '| Phase | Status | Shared language trials | p50 / p95 ms (shared) | Peak GPU MiB | Peak host used RAM KiB | RTF p50 / source |', '|---|---|---:|---|---:|---:|---|']
    for phase,p in summary['phases'].items():
        lines.append(f"| {phase} | {p['status'].replace('_',' ')} | {p['requests_fully_within_navigation']}/20 | {value(p['latency_ms_shared']['p50'])} / {value(p['latency_ms_shared']['p95'])} | {value(p['gpu_peak_mib'])} | {value(p['host_used_ram_peak_kib'])} | {value(p['rtf']['p50'])} / {p['rtf_source']} |")
    lines+=['','[checked] Unrun phases are **not measured**, never zero-performance results. Compute-query and pmon graphics memory are separate observations; do not add their overlapping PID allocations. Sampled peaks may miss short spikes. Target GPU/RAM/CPU cadence is 200 ms; actual gaps/overruns, API errors and CPU metrics are in summary.json. Graphics process/RTF clients are slower, separately timestamped. Navigation overlap uses enclosing ground-truth samples, an approximation rather than exact Nav2 event timing.','',
      '## Proposed pass lines','', '| Item | Measured result / pass |','|---|---|']
    for key,v in summary['criteria'].items():lines.append(f'| {key} | {value(v)} |')
    p=summary['phases']['P2'];baseline=summary['standalone_baseline']
    lines += ['', f"P2 allocation: {p['B_ps_samples']} /api/ps samples, {p['B_missing_samples']} missing B entries, {p['B_ps_errors']} API errors. Full GPU requires positive size == size_vram in every sample. Peak threshold: {summary['units']}.",'',
      f"P2 RTF / P1 RTF = {value(summary['rtf_P2_over_P1'])}. Same recorded console settings/commit: {value(summary['same_console_session_settings_and_commit'])}. Owner-entered RTF is an unverified visual observation; automatic RTF includes driving samples only.",'',
      f"P2 requests: {p['requests_attempted']} attempted of 20, {p['requests_not_attempted']} not attempted, {p['requests_measured']} measured, {p['requests_not_measured']} unmeasured, {p['requests_idle_or_partial']} idle/partially-overlapping trials excluded from shared results. Within 5 s: {p['within_5s_shared']['count']}/{p['within_5s_shared']['denominator']}; within 20 s: {p['within_20s_shared']['count']}/{p['within_20s_shared']['denominator']}. Zero denominators mean not measured. All-trial counts/latency are preserved separately in summary.json.",'',
      f"Task 4b standalone matched-ID p50/p95: {value(baseline['same_ids']['p50'])} / {value(baseline['same_ids']['p95'])} ms; P2 ratios: {value(baseline['P2_over_standalone']['p50'])} / {value(baseline['P2_over_standalone']['p95'])}. Source `{baseline['path']}`, hash `{baseline['sha256']}`. This is a historical comparison across planner versions (task 4b versus v0.4.3), not a controlled causal estimate.",'',
      '| Goal | P1/P2 seconds | Time ratio | P1/P2 final error m | Change m | Success / comparable start & map |','|---|---|---|---|---|---|']
    for row in summary['navigation_comparison']:
        lines.append(f"| {row['goal']} | {value(row.get('P1_s'))} / {value(row.get('P2_s'))} | {value(row.get('time_ratio'))} | {value(row.get('P1_error_m'))} / {value(row.get('P2_error_m'))} | {value(row.get('error_change_m'))} | {value(row.get('both_succeeded'))} / {value(row.get('same_map_and_locations'))}, {value(row.get('start_pose_within_0_10m'))} |")
    if summary['phases']['P4']['C_process_ram_peak_kib'] is not None:lines+=['',f"P4 CPU C process peak sampled RAM: {summary['phases']['P4']['C_process_ram_peak_kib']} KiB."]
    stop_data=json.loads(archive.target('phases.json').read_text()).get('P3',{})
    lines+=['', '## STOP evidence', '',f"Status: {stop_data.get('status','not_measured')}. Long B request outcome: {stop_data.get('long_request',{}).get('status','not measured')}; cleanup: {value(stop_data.get('cleanup_pass'))}; B pending at prompt / owner STOP marker: {value(stop_data.get('pending_at_prompt'))} / {value(stop_data.get('pending_at_owner_marker'))}; next request works: {value(stop_data.get('next_request_works'))}; model restarts for next request: {value(stop_data.get('restarts_for_next'))}. Exact audits and timing markers are retained in P3/phase.json."]
    lines += ['', '## GPU process memory','', '| Phase | PID | Process | GPU peak MiB | RAM peak KiB | CPU peak (one core = 100%) | Sources |','|---|---:|---|---:|---:|---:|---|']
    count=0
    for phase,data in summary['phases'].items():
        for row in data['processes']:
            count+=1;name=row['name'].replace('|','/').replace('\n',' ')
            lines.append(f"| {phase} | {row['pid']} | {name} | {value(row['gpu_peak_mib'])} | {value(row['ram_peak_kib'])} | {value(row['cpu_peak_one_core_percent'])} | {', '.join(row['gpu_sources'])} |")
    if not count:lines.append('| — | — | gz sim, RViz, Ollama runner, others: not measured | — | — | — | — |')
    lines += ['', '## Recommendation','',summary['recommendation'],'',
      '[not checked] Physical safety, native meaning accuracy and deployment readiness. A single pair per phase is descriptive. Missing/incomplete/idle-tail data cannot establish safe sharing. No phase numbers are inferred from older console runs. STOP timing is verified only when B is still pending at both the one-second prompt and the owner’s immediate STOP marker; a fast B completion leaves the test inconclusive. Cleanup must pass both the initial audit and recheck, without forced cleanup; successful recovery only after a model restart is disclosed and does not pass the strict STOP line.','']
    return '\n'.join(lines)


def continuation_checkpoint(source,manifest,next_phase='P1'):
    if next_phase not in ('P1','P2'):raise ValueError('Invalid continuation phase')
    metadata=json.loads(source.target('metadata.json').read_text())
    phases=json.loads(source.target('phases.json').read_text())
    if metadata.get('frozen_manifest')!=manifest:raise ValueError('Saved baseline used different frozen inputs')
    completed=('P0',) if next_phase=='P1' else ('P0','P1')
    if any(phases.get(p,{}).get('status')!='measured' for p in completed):raise ValueError('Required completed baseline is missing')
    pending=('P1','P2','P3','P4') if next_phase=='P1' else ('P2','P3','P4')
    if any(phases.get(p,{}).get('status')!='not_measured' for p in pending) or source.target('B_startup.json').exists() or source.target('owned_model').exists():raise ValueError('Only an interrupted pre-B campaign can be continued; use a fresh campaign otherwise')
    files={}
    for phase in completed:
        for filename in ('resources.jsonl','graphics_processes.jsonl','rtf.jsonl','phase.json'):
            name=phase+'/'+filename;path=source.target(name)
            if not path.is_file():
                if filename in ('resources.jsonl','graphics_processes.jsonl') or phase=='P1' and filename=='phase.json':raise ValueError('Saved baseline raw evidence is missing')
                continue
            files[name]=sha(path.read_bytes())
        if not read_jsonl(source.target(phase+'/resources.jsonl')):raise ValueError('Saved baseline is empty')
    return {'phase':phases['P0'],'completed_phases':{p:phases[p] for p in completed},'next_phase':next_phase,'source_archive':str(source.path),'metadata_sha256':sha(source.target('metadata.json').read_bytes()),'phases_sha256':sha(source.target('phases.json').read_bytes()),'raw_sha256':files}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--prepare-only',action='store_true',help='Create unmeasured metadata/prompts; do not sample or start workers')
    group.add_argument('--run',type=Path,help='Run a prepared archive; refuses an already-started campaign')
    group.add_argument('--continue-p1',type=Path,help='Start a fresh P1 archive, copying only a completed P0 from an interrupted pre-B campaign')
    group.add_argument('--continue-p2',type=Path,help='Continue from completed P0 and P1 when B startup never began; keep both baselines')
    group.add_argument('--report',type=Path,help='Read raw archive and regenerate docs/SHARED_LOAD_CHECK.md only')
    parser.add_argument('--output',type=Path,default=ROOT/'records'/('shared_load_'+datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()))
    args=parser.parse_args(argv);exit_code=0
    if args.report:
        archive=Archive(args.report,create=False)
    else:
        manifest=frozen_manifest()
        if args.run:
            archive=Archive(args.run,create=False);metadata=json.loads(archive.target('metadata.json').read_text())
            if metadata.get('owner_run_started_at') or any(p.get('status')!='not_measured' for p in json.loads(archive.target('phases.json').read_text()).values()):raise ValueError('Campaign already started; choose a fresh output to avoid mixing trials')
            if metadata['script_sha256']!=sha(Path(__file__).read_bytes()) or metadata['frozen_manifest']!=manifest:raise ValueError('Prepared script/frozen input changed')
        else:
            source_path=args.continue_p1 or args.continue_p2
            source=Archive(source_path,create=False) if source_path else None
            checkpoint=continuation_checkpoint(source,manifest,'P2' if args.continue_p2 else 'P1') if source else None
            cases=load_cases()
            archive=Archive(args.output);metadata={'created_at':stamp(),'base_tag':'v0.4.3','frozen_manifest':manifest,'script_sha256':sha(Path(__file__).read_bytes()),
              'case_ids':list(SELECTED),'starters_sha256':sha((ROOT/'data/owner_check.csv').read_bytes()),'case_selection':'fixed short/long mix; not accuracy tuning',
              'console_root':str(DEV),'measurement_mode':'60 s safety per model call; no 5 s/20 s aborts; liveness gated',
              'prompts':PROMPTS,'scope':'Measurement only; no dispatch/console control; SCRIPTED names/capability facts'}
            archive.write('metadata.json',metadata);archive.write('selected_cases.json',cases)
            phases={p:{'status':'not_measured','reason':'Owner has not run this phase'} for p in PHASES}
            if checkpoint:
                metadata['P0_continuation' if not args.continue_p2 else 'P0_P1_continuation']=checkpoint
                phases.update(checkpoint['completed_phases'])
                for name,digest in checkpoint['raw_sha256'].items():
                    raw=source.target(name).read_bytes()
                    if sha(raw)!=digest:raise ValueError('P0 changed during copy')
                    dest=archive.target(name);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
                archive.write('metadata.json',metadata)
            archive.write('phases.json',phases)
        if not args.prepare_only:
            metadata['owner_run_started_at']=stamp();archive.write('metadata.json',metadata)
            campaign=Campaign(archive,ConsoleRecords())
            campaign.phases=json.loads(archive.target('phases.json').read_text())
            try:
                options={'skip_baseline':bool(args.continue_p1 or args.continue_p2)}
                if args.continue_p2:options['skip_navigation']=True
                campaign.run(**options)
            except (EOFError,KeyboardInterrupt,Exception) as exc:
                archive.write('interruption.json',{'time':stamp(),'type':type(exc).__name__,'message':str(exc)[:1000]})
                exit_code=130 if isinstance(exc,(EOFError,KeyboardInterrupt)) else 2
                print(f'Campaign incomplete: {type(exc).__name__}: {exc}. Evidence retained. No console process was controlled.',file=sys.stderr)
            finally:
                archive.write('frozen_after.json',frozen_manifest())
    summary=build_summary(archive);archive.write('summary.json',summary)
    report_path=ROOT/'docs/SHARED_LOAD_CHECK.md'
    if report_path.is_symlink() or report_path.parent.resolve()!=ROOT/'docs' or report_path.exists() and report_path.stat().st_nlink!=1:raise ValueError('Report path must be an ordinary module file')
    report_path.write_text(render_report(summary,archive))
    print('Raw archive:',archive.path);print('Report:',ROOT/'docs/SHARED_LOAD_CHECK.md')
    return exit_code


if __name__=='__main__':raise SystemExit(main())
