"""Measurement-controller tests use fake workers, never C in a ROS interpreter."""
import importlib.util
import json
from pathlib import Path
import threading
import pytest
from amr_language.office_backend import LocalOfficeBackend,settings_for_office
from amr_language.office_c_client import CWorker


@pytest.fixture
def controller(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root/'scripts'))
    spec=importlib.util.spec_from_file_location('C_measurement_test',root/'scripts/measure_C.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    base=tmp_path/'campaign';base.mkdir();monkeypatch.setattr(module,'BASE',base)
    class Monitor:
        def __init__(self,*args):pass
        def start(self):return self
        def stop(self):return {'peak_total_gpu_mib':5}
    class Worker(CWorker):
        fail_translation=False
        def __init__(self,*args):
            self.calls=[];self.ready={'isolated':True,'startup_max_rss_kib':10};self.command=['fake-C-python','-I']
        def translate(self,text,language,timeout=60.):
            assert timeout==60.
            value={'ok':not self.fail_translation,'english':text,'translation_ms':1.,'max_rss_kib':20}
            if self.fail_translation:value['error']='input_too_long'
            self.calls.append(value)
            if self.fail_translation:raise ValueError('C input_too_long')
            return text
        def probe(self,*args):return {'ok':True,'english':'Hello','max_rss_kib':21}
        def close(self):pass
    class Runner:
        def __init__(self,*args):self.restarts=0
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def load(self,model):pass
        def allocation(self):return {'test_only':True}
        def ensure_live(self):return True
        def probe(self):return {'ok':True}
    class Client:
        def verify_identity(self,*args):pass
        def _request(self,method,path,payload,*args):
            assert payload['options']['temperature']==0.
            return {'done':True,'message':{'content':payload['messages'][1]['content']}}
    backend=object.__new__(LocalOfficeBackend)
    backend.name='mock-local-B';backend.file_sha256=None;backend.client=Client();backend.stats=[];backend.stats_lock=threading.Lock()
    monkeypatch.setattr(module,'ResourceMonitor',Monitor);monkeypatch.setattr(module,'CWorker',Worker);monkeypatch.setattr(module,'Runner',Runner)
    monkeypatch.setattr(module,'request',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('no server')))
    row={'id':'AUTHORED-TEST','speaker':'STARTER','language':'en','text':'Give the file to Divya.','expected_english':'Give the file to Divya.'}
    return module,base,Worker,backend,row


def test_live_pair_measurement_records_two_sources_and_post_liveness(controller,names):
    module,base,worker,backend,row=controller
    settings=settings_for_office({'mode':'measurement'})
    module.paired('cpu',[row],names,{},settings,backend,{'unit_test_only':True})
    record=json.loads((base/'cpu_B_loaded/requests.jsonl').read_text())
    assert record['status']=='measured' and record['result']['draws']==2
    assert record['result']['decision']=='act'
    assert record['attempts'][0]['B_post']['ok'] and record['attempts'][0]['C_post']['ok']
    assert record['request_wall_ms']>=0 and record['C_call']['translation_ms']==1.
    summary=json.loads((base/'cpu_B_loaded/summary.json').read_text())
    assert summary['C_process_peak_rss_kib']==21


def test_capacity_error_is_measured_invalid_translation_not_infrastructure_missing(controller):
    module,base,worker,backend,row=controller;worker.fail_translation=True
    module.unloaded('cpu',[row],{'unit_test_only':True})
    record=json.loads((base/'cpu_B_unloaded/requests.jsonl').read_text())
    assert record['status']=='measured' and record['C_call']['ok'] is False
    assert 'input_too_long' in record['attempts'][0]['translation_error']
    summary=json.loads((base/'cpu_B_unloaded/summary.json').read_text())
    assert summary['missing']==0 and summary['groups']['STARTER:en']['valid_translations']==0
