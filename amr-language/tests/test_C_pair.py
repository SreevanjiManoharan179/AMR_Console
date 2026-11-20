"""Cross-system agreement is two real sources, never duplicated random votes."""
import io
import pytest
from amr_language import understand
from amr_language.office_backend import MockOfficeBackend,LocalOfficeBackend,CrossSystemOfficeBackend
from amr_language.office_c_client import CWorker
from amr_language.backends import BackendError,BackendTimeout


class PairMock(MockOfficeBackend):
    cross_system=True
    rewrite_sources=('C_translation','B_greedy')
    rewrite_temperatures=(None,0.)


def run(names,rewrites,plans=None):
    backend=PairMock(rewrites,plans or [{}]*2);trace=[]
    result=understand('Give the file to Divya.',names,'Bharathwaj','en',
                     {'backend':backend,'draw_policy':'cross_system','trace':trace})
    return result,trace,backend


def test_cross_system_agreement_uses_two_sources_and_preserves_confirmation(names):
    result,trace,backend=run(names,['Give the file to Divya.']*2)
    assert result['decision']=='act' and result['draws']==2
    assert result['readback'].endswith('Shall I go ahead?')
    pairs=[p for p in trace if 'index' in p]
    assert [p['rewrite_source'] for p in pairs]==['C_translation','B_greedy']
    assert [p['rewrite_temperature'] for p in pairs]==[None,0.]
    assert len([c for c in backend.calls if c[0]=='rewrite'])==2


def test_cross_system_disagreement_never_adds_random_samples(names):
    result,trace,backend=run(names,['Give the file to Divya.','Give the cup to Divya.'])
    assert result['decision']=='ask' and result['draws']==2 and result['ask']['slot']=='item'
    assert len([c for c in backend.calls if c[0]=='rewrite'])==2


def test_cross_system_missing_translation_cannot_authorize_act(names):
    result,trace,backend=run(names,[ValueError('C input_too_long'),'Give the file to Divya.'])
    assert result['decision']=='ask' and result['steps']==[] and result['draws']==2
    assert trace[0]['error']=='invalid_rewrite'


def test_cross_system_model_fallback_is_allowed_only_for_unparsed_rewrite(names):
    english='Could you get the file over to Divya?'
    plan={'steps':[{'n':1,'action':'deliver','item':'file','to':'Divya','if_not_there':'bring_back'}],'issue':None}
    result,trace,backend=run(names,[english]*2,[plan]*2)
    assert result['decision']=='act' and result['planner']=='model' and result['draws']==2
    assert len([c for c in backend.calls if c[0]=='plan'])==1


def test_ordinary_backend_cannot_claim_cross_system_agreement(names):
    text='Give the file to Divya.'
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4),'draw_policy':'cross_system'})
    assert result['decision']=='refuse' and result['draws']==0 and result['steps']==[]


def test_C_client_uses_fixed_isolated_command_and_drops_ros_environment(tmp_path,monkeypatch):
    import amr_language.office_c_client as module
    captured={}
    class FakeProcess:
        def __init__(self,*args,**kwargs):
            captured.update(command=args[0],env=kwargs['env'])
            self.stdout=io.StringIO('{"kind":"ready","isolated":true,"ros_paths":[]}\n')
            self.stdin=io.StringIO()
        def poll(self):return 0
    monkeypatch.setenv('PYTHONPATH','/opt/ros/jazzy/lib/python3.12/site-packages')
    monkeypatch.setenv('LD_LIBRARY_PATH','/opt/ros/jazzy/lib')
    monkeypatch.setattr(module.subprocess,'Popen',FakeProcess)
    worker=CWorker('cpu',tmp_path)
    assert captured['command'][0].endswith('.runtime/indictrans2-C/venv/bin/python')
    assert captured['command'][1]=='-I'
    assert 'PYTHONPATH' not in captured['env'] and 'LD_LIBRARY_PATH' not in captured['env']
    worker.close()


def test_C_safety_timeout_and_dead_process_fail_closed(monkeypatch):
    import queue
    worker=object.__new__(CWorker);worker._responses=queue.Queue()
    with pytest.raises(BackendTimeout):worker._read(.001)
    worker._responses.put(None)
    with pytest.raises(BackendError):worker._read(.001)


def test_B_comparison_is_greedy_with_original_seed(monkeypatch):
    local=object.__new__(LocalOfficeBackend)
    local.name='test-B';local.file_sha256='checked';local.client=object();local.stats=[];local.stats_lock=object()
    translator=object.__new__(CWorker);translator.ready={'isolated':True}
    translator.translate=lambda text,language,timeout:'C English'
    observed=[]
    monkeypatch.setattr(LocalOfficeBackend,'rewrite',lambda self,text,roster,language,index,timeout,settings:observed.append(index) or 'B English')
    backend=CrossSystemOfficeBackend(local,translator)
    assert backend.rewrite('original',None,'ta',0,60,{})=='C English'
    assert backend.rewrite('original',None,'ta',1,60,{})=='B English'
    assert observed==[0]
    with pytest.raises(ValueError):backend.rewrite('original',None,'ta',2,60,{})


def test_closed_C_worker_fails_as_transport_error_not_an_unhandled_exception():
    worker=object.__new__(CWorker);worker.process=None
    with pytest.raises(BackendError):worker.request({'op':'translate'},60.)
