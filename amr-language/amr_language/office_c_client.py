"""Process-only client for pinned C. Never imports torch/transformers into caller."""
import json
import os
from pathlib import Path
import queue
import threading
import subprocess
import time
import uuid
from .backends import BackendError,BackendTimeout
from .calibration import ROOT


class CWorker:
    def __init__(self,device,directory):
        if device not in ('cpu','cuda'):raise ValueError('Invalid C device')
        self.device=device;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.command=[str(ROOT/'.runtime/indictrans2-C/venv/bin/python'),'-I',str(ROOT/'scripts/translation_worker_C.py'),'--device',device]
        env=os.environ.copy()
        for key in ('PYTHONPATH','PYTHONHOME','LD_LIBRARY_PATH','HF_TOKEN','HUGGING_FACE_HUB_TOKEN'):env.pop(key,None)
        self.log=(self.directory/'C_worker_stderr.txt').open('a')
        self.process=subprocess.Popen(self.command,cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=self.log,text=True,bufsize=1,start_new_session=True)
        self.calls=[];self._responses=queue.Queue();self._request_lock=threading.Lock()
        self._reader=threading.Thread(target=self._read_responses,daemon=True);self._reader.start()
        try:
            self.ready=self._read(120.)
            if self.ready.get('kind')!='ready' or self.ready.get('isolated') is not True or self.ready.get('ros_paths'):
                raise BackendError('C did not start in isolation')
        except Exception:self.close();raise
    def _read_responses(self):
        try:
            for line in self.process.stdout:
                try:
                    value=json.loads(line)
                    if not isinstance(value,dict):raise ValueError('Expected response object')
                    self._responses.put(value)
                except ValueError:self._responses.put(BackendError('Invalid C response'))
        except (OSError,ValueError):self._responses.put(BackendError('C response stream closed'))
        finally:self._responses.put(None)
    def _read(self,timeout):
        try:value=self._responses.get(timeout=timeout)
        except queue.Empty as error:raise BackendTimeout('C safety limit exceeded') from error
        if value is None:raise BackendError('C worker exited')
        if isinstance(value,Exception):raise value
        return value
    def request(self,payload,timeout):
        if self.process is None or self.process.poll() is not None:raise BackendError('C worker is not running')
        tick=time.monotonic();request_id=uuid.uuid4().hex
        if not self._request_lock.acquire(timeout=timeout):raise BackendTimeout('C worker is busy')
        try:
            message={**payload,'request_id':request_id}
            try:
                self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n');self.process.stdin.flush()
                deadline=tick+timeout
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise BackendTimeout('C safety limit exceeded')
                    value=self._read(remaining)
                    if value.get('request_id')==request_id:break
                    # Late output is drained, never reused for another request.
            except (OSError,ValueError) as error:raise BackendError('C process transport failed') from error
            value['ipc_wall_ms']=(time.monotonic()-tick)*1000
            return value
        finally:self._request_lock.release()
    def translate(self,text,language,timeout=60.):
        value=self.request({'op':'translate','text':text,'language':language},timeout);self.calls.append(value)
        if not value.get('ok'):
            if value.get('error') in ('input_too_long','truncated_translation','empty_translation','invalid_input'):
                raise ValueError('C '+value['error'])
            raise BackendError('C inference failed')
        return value['english']
    def probe(self,timeout=2.):
        try:
            value=self.request({'op':'probe'},timeout)
            return {**value,'ok':value.get('ok') is True and bool(value.get('english'))}
        except (BackendError,BackendTimeout):return {'ok':False,'error':'C generation liveness failed'}
    def close(self):
        if getattr(self,'process',None):
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(json.dumps({'op':'close','request_id':uuid.uuid4().hex})+'\n');self.process.stdin.flush();self.process.wait(timeout=8)
                except (OSError,subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:self.process.wait(timeout=8)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            self.process.stdin.close()
            if getattr(self,'_reader',None):self._reader.join(timeout=1.)
            self.process.stdout.close()
            self.process=None
        if getattr(self,'log',None):self.log.close();self.log=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
