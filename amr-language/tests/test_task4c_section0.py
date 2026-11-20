"""No C model execution: exercise real IPC framing and product-deadline recovery."""
import subprocess
import sys
import pytest
from amr_language.office_backend import load_office_profile
from amr_language.office_c_client import CWorker
from amr_language.backends import BackendTimeout


def test_office_B_is_an_explicit_pinned_profile():
    value=load_office_profile('office_B')
    assert value['model']=='qwen3.5:4b-q4_K_M'
    assert value['planner_mode']=='hybrid' and value['draw_policy']=='adaptive'
    assert value['sample_temperature']==.7 and value['office_meanings'] is False
    assert value['follow_person_hint']=='ask' and value['timeout_s']==5
    assert (value['rewrite_tokens'],value['plan_tokens'])==(384,768)
    with pytest.raises(ValueError):load_office_profile('unapproved-model')


def test_timed_out_response_cannot_answer_next_request(tmp_path,monkeypatch):
    import amr_language.office_c_client as module
    echo=tmp_path/'echo.py'
    echo.write_text('''import json,sys,time
print(json.dumps({'kind':'ready','isolated':bool(sys.flags.isolated),'ros_paths':[]}),flush=True)
for line in sys.stdin:
    r=json.loads(line)
    if r['op']=='close':
        print(json.dumps({'request_id':r['request_id'],'ok':True}),flush=True);break
    if r.get('text')=='old':time.sleep(.10)
    print(json.dumps({'request_id':r['request_id'],'ok':True,'english':r.get('text')}),flush=True)
''')
    original=subprocess.Popen;commands=[]
    def fake_translator(command,**kwargs):
        assert command[0].endswith('/indictrans2-C/venv/bin/python') and command[1]=='-I'
        commands.append(command)
        # The stand-in runs on this interpreter: the translation venv need not be installed.
        return original([sys.executable,'-I',str(echo)],**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',fake_translator)
    with CWorker('cpu',tmp_path/'logs') as worker:
        with pytest.raises(BackendTimeout):worker.request({'op':'translate','text':'old'},.01)
        response=worker.request({'op':'translate','text':'new'},1.)
        assert response['english']=='new' and response['request_id']
        assert worker.request({'op':'translate','text':'third'},1.)['english']=='third'
    assert len(commands)==1  # Old response was drained, not reused or treated as a vote.
