"""Measurement and duplicate reuse invariants, independent of model quality."""
from copy import deepcopy
import importlib
from pathlib import Path
import time

import pytest
from amr_language import understand as public_understand
from amr_language.office_backend import MockOfficeBackend

def understand(text,names,requester,language=None,settings=None):
    return public_understand(text,names,requester,language,{**(settings or {}),'planner_mode':'model','draw_policy':'four'})


FORM={'steps':[{'action':'deliver','item':'file','to':'Divya'}],'issue':None}


def test_duplicate_rewrites_are_one_distinct_pair_not_four_votes(names):
    backend=MockOfficeBackend(['Give the file to Divya.','Give  the file to Divya.']*2,[FORM])
    trace=[]
    result=understand('Give the file to Divya.',names,'Bharathwaj',settings={'backend':backend,'trace':trace})
    assert result['decision']=='act'
    assert sum(call[0]=='rewrite' for call in backend.calls)==4
    assert sum(call[0]=='plan' for call in backend.calls)==1
    assert trace[-1]['distinct_pairs']==1 and trace[-1]['plans_reused']==3
    assert trace[-1]['valid_distinct_pairs']==1


def test_different_rewrites_not_cached_and_failed_distinct_pair_blocks_act(names):
    texts=['Give the file to Divya.','Deliver the file to Divya.','Hand the file to Divya.','Take the file to Divya.']
    backend=MockOfficeBackend(texts,[FORM,{},FORM,FORM]);trace=[]
    result=understand(texts[0],names,'Bharathwaj',settings={'backend':backend,'trace':trace})
    assert result['decision']=='ask'
    assert sum(call[0]=='plan' for call in backend.calls)==4
    assert trace[-1]['distinct_pairs']==4


def test_measurement_uses_60s_per_call_not_product_deadline(names):
    backend=MockOfficeBackend(['Give the file to Divya.'],[FORM])
    timeouts=[]
    rewrite=backend.rewrite;plan=backend.plan
    def recorded_rewrite(text,roster,lang,index,timeout,settings):
        timeouts.append(timeout);time.sleep(.01)
        return rewrite(text,roster,lang,index,timeout,settings)
    def recorded_plan(english,roster,index,timeout,settings):
        timeouts.append(timeout);return plan(english,roster,index,timeout,settings)
    backend.rewrite=recorded_rewrite;backend.plan=recorded_plan
    result=understand('Give the file to Divya.',names,'Bharathwaj',settings={'backend':backend,'mode':'measurement','timeout_s':.01})
    assert result['decision']=='act' and all(t==60 for t in timeouts)
    result=understand('Give the file to Divya.',names,'Bharathwaj',settings={'backend':backend,'timeout_s':.01})
    assert result['decision']=='refuse' and result['refuse']['reason']=='model_timeout'


def test_parallel_draws_and_distinct_plans_preserve_confirmation_contract(names):
    backend=MockOfficeBackend(['Give the file to Divya.'],[FORM]);trace=[]
    result=understand('Give the file to Divya.',names,'Bharathwaj',settings={'backend':backend,'pipeline_parallel':4,'trace':trace})
    assert result['decision']=='act' and 'Shall I go ahead?' in result['readback']
    assert trace[-1]['requested_parallel']==4 and trace[-1]['parallel']==1 and trace[-1]['plans_reused']==3


def test_dead_server_requests_are_missing_not_quality_failures(monkeypatch,names):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    measure=importlib.import_module('measure_office')
    class Runner:
        restarts=0
        def ensure_live(self): return False
    backend=MockOfficeBackend(['Give the file to Divya.'],[FORM])
    row={'id':'STARTER-en-01','speaker':'STARTER','language':'en','text':'Give the file to Divya.','expected_english':'Give the file to Divya.'}
    result=measure.measure_one(row,names,{},Runner(),backend)
    assert result['status']=='not_measured' and result['within_5s'] is None
    assert result['request_wall_ms'] is None and not backend.calls


def test_prepared_artifact_cache_refuses_mutation_without_rehashing(monkeypatch,tmp_path):
    import amr_language.office_backend as module
    weight=tmp_path/'weights';weight.write_bytes(b'weights')
    manifest=tmp_path/'manifest';manifest.write_bytes(b'manifest')
    constructed=[]
    class Local:
        name='fixture';file_sha256='verified'
        def __init__(self,settings): constructed.append(settings)
    monkeypatch.setattr(module,'local_path',lambda value:Path(value))
    monkeypatch.setattr(module,'LocalOfficeBackend',Local)
    settings={'model':'fixture','model_file':str(weight),'model_manifest_file':str(manifest)}
    first=module.make_backend(settings)
    assert module.make_backend(settings) is first and len(constructed)==1
    weight.write_bytes(b'changed-weights')
    with pytest.raises(ValueError,match='artifacts changed'): module.make_backend(settings)
    assert len(constructed)==1


def test_C_lock_has_all_declared_runtime_dependencies_and_no_install(tmp_path,measurement_record):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version
    import json
    root=Path(__file__).resolve().parents[1]
    artifacts=json.loads(measurement_record('records/task3/C_metadata/artifact_manifest.json').read_text())
    pins={a['name']:a['version'] for a in artifacts['artifacts']}
    closure=json.loads(measurement_record('records/task3/C_metadata/dependency_closure.json').read_text())
    assert len(pins)==75 and artifacts['status']=='NOT_INSTALLED_WAITING_FOR_APPROVED_C'
    for package in closure:
        assert pins[package['name']]==package['version']
        for raw in package['requires_dist'] or []:
            requirement=Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({'extra':''}): continue
            assert Version(pins[canonicalize_name(requirement.name)]) in requirement.specifier
    model=json.loads((root/'data/indictrans2_C_model.json').read_text())
    assert len(model['revision'])==40 and 'pytorch_model.bin' not in [f['rfilename'] for f in model['files']]
    # Checking the procedural guard performs no model import, login or download.
    import subprocess,sys
    before=(root/'.runtime/indictrans2-C').exists()
    result=subprocess.run([sys.executable,str(root/'scripts/download_C_model.py')],capture_output=True,text=True)
    assert result.returncode==2 and 'STOP' in result.stderr
    assert (root/'.runtime/indictrans2-C').exists()==before


def test_rejected_rewrite_keeps_actual_model_text_for_human_review(names):
    raw='file-ஐ Divya-கிட்ட கொடுங்க'
    backend=MockOfficeBackend([raw],[FORM]);trace=[]
    result=understand('Give the file to Divya.',names,'Bharathwaj',settings={'backend':backend,'trace':trace})
    assert result['decision']=='refuse'
    assert [p['rewrite_raw'] for p in trace if 'index' in p]==[raw]*4
    assert all(p.get('error')=='invalid_rewrite' for p in trace if 'index' in p)
    assert sum(c[0]=='plan' for c in backend.calls)==0


def test_failed_post_liveness_is_retried_once_and_preserved(monkeypatch,names):
    import types
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    measure=importlib.import_module('measure_office')
    backend=types.SimpleNamespace(stats=[])
    class Runner:
        restarts=0;probes=0
        def ensure_live(self): return True
        def probe(self):
            self.probes+=1
            return {'ok':self.probes>1}
        def restart(self,reason): self.restarts+=1
    runner=Runner()
    result={'decision':'act','refuse':None,'ask':None,'steps':[]}
    monkeypatch.setattr(measure,'understand',lambda *a,**k:deepcopy(result))
    row={'id':'fixture','speaker':'STARTER','language':'en','text':'Go to reception.','expected_english':'Go to reception.'}
    record=measure.measure_one(row,names,{},runner,backend)
    assert record['status']=='measured' and record['runner_restarts']==1
    assert [a['status'] for a in record['attempts']]==['not_measured','measured']
    runner.probe=lambda:{'ok':False}
    record=measure.measure_one(row,names,{},runner,backend)
    assert record['status']=='not_measured' and len(record['attempts'])==2
    assert record['request_wall_ms'] is None and record['within_5s'] is None


def test_rewrite_review_uses_raw_truncated_call_text(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    report=importlib.import_module('report_task3')
    row={'calls':[{'stage':'rewrite','index':2,'output_text':'Partial model text'}]}
    pair={'index':2,'error':'invalid_rewrite'}
    assert report.raw_rewrite(row,pair)=='Partial model text'
    assert report.raw_rewrite(row,{'index':2,'rewrite_raw':'Full response'})=='Full response'
