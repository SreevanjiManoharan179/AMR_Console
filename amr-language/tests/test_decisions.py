import hashlib
import json
from pathlib import Path
import time

import pytest

from amr_language import calibration
from amr_language.core import understand
from amr_language.backends import MockBackend
from amr_language.core import _settings
from amr_language.grounding import Names


@pytest.fixture
def configured(tmp_path,monkeypatch,names):
    # Artificial provenance fixtures for branch testing; never real-world results.
    monkeypatch.setattr(calibration,'ROOT',tmp_path)
    def build(forms,q=.2,language=None):
        backend=MockBackend(forms)
        config=_settings({'backend':backend,'calibration_file':str(tmp_path/'threshold.json')})
        splits={}
        for name in ('calibration','test'):
            rows=[{'source':'real','speaker':name+'-fixture','text':'unit test fixture',
                   'probabilities':{'place':{'a':1-q,'b':q}},'truth':{'place':'a'}} for _ in range(10)]
            path=tmp_path/(name+'.jsonl')
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            splits[name]={'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                          'speakers':[name+'-fixture']}
        artifact={'source':'real','alpha':.1,'aggregation':'max','q_hat':q,
                  'pipeline_signature':calibration.signature(Names(names),config,backend,language),**splits}
        (tmp_path/'threshold.json').write_text(json.dumps(artifact))
        return config
    return build


def test_act_ask_refuse_and_approved_options(names,form,configured):
    result=understand('go to reception',names,settings=configured([form(place='reception')]))
    assert result['decision']=='act'
    assert result['steps'][0]['slots']['place']['prediction_set']==['reception']
    result=understand('go to office',names,settings=configured([form(place='office')],q=.6))
    assert result['decision']=='ask'
    assert result['question']['options']==['open_office','team_office']
    result=understand('go to server room',names,settings=configured([form(place='server room')]))
    assert result['refusal']['template']=='unknown_name'


def test_no_guarantee_from_tiny_calibration(names,form,configured):
    config=configured([form(place='reception')],q=1.)
    result=understand('go to reception',names,settings=config)
    assert result['decision']=='refuse' # <none> is in the complete set at q=1.


def test_weakest_slot_question(names,form,configured):
    names['people'][0]['aliases']=['colleague']
    names['people'][1]['aliases']=['colleague']
    # Roster changes before signature generation; exact room plus ambiguous person.
    samples=[form('deliver',person='colleague',place='open office',item='file')]
    result=understand('deliver file to colleague in open office',names,settings=configured(samples,q=.6))
    assert result['decision']=='ask'
    assert result['question']['options']==['Neha','Priya']


def test_incompatible_destinations(names,form,configured):
    samples=[form('deliver',person='Priya',place='team office',item='file')]
    result=understand('deliver file to Priya in team office',names,settings=configured(samples))
    assert result['refusal']['template']=='conflicting_targets'
    samples=[form('deliver',person='Priya',object='printer',item='file')]
    result=understand('deliver file to Priya at printer',names,settings=configured(samples))
    assert result['refusal']['template']=='conflicting_targets'


def test_multistep_and_context_actions(names,form,configured):
    sample=form('deliver',person='Priya',item='file')
    sample['steps'].append(form('return')['steps'][0])
    result=understand('take file to Priya then come back',names,settings=configured([sample]))
    assert result['decision']=='act'
    assert [s['action'] for s in result['steps']]==['deliver','return']
    for action,text in [('retry','try the delivery again'),('wait_ack','wait for acknowledgement')]:
        result=understand(text,names,settings=configured([form(action)]))
        assert result['decision']=='act' # Parser eligibility only; console owns state check.


def test_language_templates(names,form,configured):
    result=understand('office போ',names,language='ta',settings=configured([form(place='office',language='ta')],q=.6,language='ta'))
    assert result['question']['language']=='ta'
    result=understand('office പോകൂ',names,language='ml',settings=configured([form(place='office',language='ml')],q=.6,language='ml'))
    assert result['language']=='ml' and result['question']['language']=='en'


def test_changed_roster_pipeline_or_speaker_split_rejects(names,form,configured):
    config=configured([form(place='reception')])
    result=understand('go to reception',names,settings=dict(config,temperature=.8))
    assert result['refusal']['template']=='uncalibrated'
    names['rooms'][0]['aliases'].append('new approved alias')
    result=understand('go to reception',names,settings=config)
    assert result['refusal']['template']=='uncalibrated'


def test_tampered_threshold_and_synthetic_calibration_refused(names,form,configured):
    config=configured([form(place='reception')])
    path=Path(config['calibration_file'])
    value=json.loads(path.read_text())
    value['q_hat']=.3
    path.write_text(json.dumps(value))
    assert understand('go to reception',names,settings=config)['refusal']['template']=='uncalibrated'
    value['source']='SYNTHETIC'
    path.write_text(json.dumps(value))
    assert understand('go to reception',names,settings=config)['refusal']['template']=='uncalibrated'


def test_split_tamper_refused(names,form,configured):
    config=configured([form(place='reception')])
    meta=json.loads(Path(config['calibration_file']).read_text())
    Path(meta['test']['file']).write_text('{}\n')
    assert understand('go to reception',names,settings=config)['refusal']['template']=='uncalibrated'


def test_timeout_counts_full_sampling_operation(names,form,configured,monkeypatch):
    config=configured([form(place='reception')])
    backend=config['backend']
    original=backend.sample
    def slow(*args,**kwargs):
        time.sleep(.02)
        return original(*args,**kwargs)
    monkeypatch.setattr(backend,'sample',slow)
    config['timeout_s']=.01
    result=understand('go to reception',names,settings=config)
    assert result['refusal']['template']=='model_timeout'
    assert result['samples']==1
