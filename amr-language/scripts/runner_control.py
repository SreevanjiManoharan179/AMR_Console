"""Owned local-runner lifecycle and generation probes for authorized diagnostics."""
import http.client
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent


def request(path,payload=None,timeout=60):
    connection=http.client.HTTPConnection('127.0.0.1',11434,timeout=timeout)
    started=time.monotonic()
    try:
        connection.request('POST' if payload is not None else 'GET',path,
                           json.dumps(payload).encode() if payload is not None else None,
                           {'Content-Type':'application/json'})
        response=connection.getresponse()
        body=response.read(1048577)
        if response.status!=200 or len(body)>1048576: raise RuntimeError('Local runner response failure')
        return json.loads(body),round((time.monotonic()-started)*1000,3)
    finally: connection.close()


class Runner:
    def __init__(self,directory,parallel=1,max_queue=1,overhead_gib=4):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.parallel=parallel;self.max_queue=max_queue;self.overhead_gib=overhead_gib
        self.process=None;self.events=[];self.model=None;self.restarts=0
        self.log_stream=None;self.log_path=None
    def event(self,kind,**data):
        value={'kind':kind,'time_ns':time.time_ns(),'monotonic_s':time.monotonic(),**data}
        self.events.append(value)
        with (self.directory/'runner_events.jsonl').open('a') as stream: stream.write(json.dumps(value)+'\n')
        return value
    def start(self):
        if self.process is not None: raise RuntimeError('Runner is already owned')
        try: request('/api/version',timeout=.3)
        except (OSError,RuntimeError,http.client.HTTPException): pass
        else: raise RuntimeError('An existing server owns 11434; refusing to terminate an unowned service')
        stamp=time.time_ns()
        self.log_path=ROOT/'.runtime'/f'task3_runner_{stamp}.log'
        self.log_stream=self.log_path.open('w')
        args=[sys.executable,str(ROOT/'scripts/local_runner.py'),'serve','--parallel',str(self.parallel),
              '--max-queue',str(self.max_queue),'--gpu-overhead-gib',str(self.overhead_gib)]
        started=time.monotonic()
        self.process=subprocess.Popen(args,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=self.log_stream,start_new_session=True)
        while time.monotonic()-started<20:
            if self.process.poll() is not None: raise RuntimeError('Runner exited during startup; inspect private log')
            try:
                version,_=request('/api/version',timeout=.3)
                self.event('runner_start',pid=self.process.pid,parallel=self.parallel,max_queue=self.max_queue,
                           overhead_gib=self.overhead_gib,elapsed_ms=(time.monotonic()-started)*1000,version=version)
                return
            except (OSError,RuntimeError,http.client.HTTPException): time.sleep(.1)
        raise RuntimeError('Runner startup did not complete')
    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid,signal.SIGTERM)
                try: self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=5)
            self.process=None
            if self.log_stream: self.log_stream.close();self.log_stream=None
            self.event('runner_stop')
    def load(self,model):
        self.model=model
        result,elapsed=request('/api/generate',{'model':model,'prompt':'','stream':False,'keep_alive':'30m',
                                               'options':{'num_ctx':4096}},60)
        ps,_=request('/api/ps')
        self.event('model_load',model=model,elapsed_ms=elapsed,done=result.get('done'),ps=ps)
        return ps
    def probe(self,timeout=2):
        started=time.monotonic()
        try:
            result,elapsed=request('/api/generate',{'model':self.model,'prompt':'Say hello.','stream':False,
                    'think':False,'keep_alive':'30m','options':{'num_ctx':4096,'num_predict':4,'temperature':0}},timeout)
            ok=result.get('done') is True and bool(result.get('response','').strip())
            return self.event('liveness',ok=ok,elapsed_ms=elapsed,response=result.get('response'),
                              eval_count=result.get('eval_count'),limit_s=timeout)
        except (OSError,RuntimeError,ValueError,http.client.HTTPException) as error:
            return self.event('liveness',ok=False,elapsed_ms=(time.monotonic()-started)*1000,
                              error=type(error).__name__,limit_s=timeout)
    def restart(self,reason):
        model=self.model;started=time.monotonic();self.close();self.start();self.load(model);self.restarts+=1
        self.event('runner_restart',reason=reason,elapsed_ms=(time.monotonic()-started)*1000,count=self.restarts)
    def ensure_live(self):
        first=self.probe()
        if first['ok']: return True
        self.restart('failed generation liveness probe')
        return self.probe()['ok']
    def allocation(self):
        value,_=request('/api/ps')
        rows=value.get('models',[])
        commands=[]
        for candidate in Path('/proc').iterdir():
            if not candidate.name.isdigit(): continue
            try:
                if os.getpgid(int(candidate.name))!=self.process.pid: continue
                argv=(candidate/'cmdline').read_bytes().decode().strip('\0').split('\0')
                if argv and argv[0].endswith('/llama-server'):
                    commands.append({'argv':argv,'effective_parallel_slots':int(argv[argv.index('-np')+1]) if '-np' in argv else None})
            except (OSError,ValueError,ProcessLookupError): pass
        return {'ps':value,'gpu_fraction':[row['size_vram']/row['size'] if row.get('size') else None for row in rows],
                'runner_processes':commands,'requested_parallel':self.parallel,
                'interpretation':'API allocation ratio, not a direct CPU-time measurement'}
    def __enter__(self): self.start();return self
    def __exit__(self,*args): self.close()
