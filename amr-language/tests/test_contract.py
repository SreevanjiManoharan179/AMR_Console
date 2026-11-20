from copy import deepcopy
import json
import math
import unicodedata

import pytest

from amr_language.core import understand
from amr_language.backends import BackendError, BackendTimeout, MockBackend
from amr_language.core import source_span
from amr_language.grounding import Names, NONE, normalize
from amr_language.schema import SCHEMA, parse_json, validate_task_form


def test_valid_and_strict_schema(form):
    valid = form(place='reception')
    assert validate_task_form(valid) is valid
    assert SCHEMA['additionalProperties'] is False
    mutations = [dict(valid, execute=True), dict(valid, steps=[]), dict(valid, steps=valid['steps']*5),
                 dict(valid, out_of_scope=1), dict(valid, language='fr')]
    for field, value in [('action','fly'), ('place',''), ('item', 'file'), ('place', 3), ('place','x\n')]:
        bad = deepcopy(valid)
        bad['steps'][0][field] = value
        mutations.append(bad)
    for bad in mutations:
        with pytest.raises(ValueError):
            validate_task_form(bad)
    for action in ('go','check','deliver'):
        with pytest.raises(ValueError):
            validate_task_form(form(action))
    with pytest.raises(ValueError):
        validate_task_form(form('deliver', person='Priya'))
    for action in ('return','retry','wait_ack'):
        validate_task_form(form(action))
        with pytest.raises(ValueError):
            validate_task_form(form(action, place='home'))


def test_duplicate_and_nonfinite_json_rejected():
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        with pytest.raises(ValueError):
            parse_json(raw)


def test_unicode_aliases_collisions_and_seats(names):
    names['rooms'][1]['aliases_by_language'] = {'ta':['வரவேற்பு'], 'ml':['സ്വീകരണം'], 'te':['రిసెప్షన్']}
    names['rooms'][2]['aliases'] += ['shared']
    names['rooms'][3]['aliases'] += ['shared']
    roster = Names(names)
    for alias in ('வரவேற்பு','സ്വീകരണം','రిసెప్షన్'):
        assert roster.match('place', unicodedata.normalize('NFD', alias)) == ('reception',)
    assert roster.match('place','office') == ('open_office','team_office')
    assert roster.match('place','shared') == ('conference_hall','demo_area')
    assert roster.match('place','server room') == (NONE,)
    assert roster.match('place','open_office_r1c1') == ('open_office_r1c1',)
    assert roster.match('person','manager') == ('Divya',)
    assert roster.match('person','Priyaa') == (NONE,)
    assert normalize('  மேனேஜர்  ') == 'மேனேஜர்'


def test_roster_validation(names):
    for mutate in (lambda n:n.update(approval_status='draft'),
                   lambda n:n['people'][0].update(seat='missing'),
                   lambda n:n['rooms'].append(deepcopy(n['rooms'][0])),
                   lambda n:n['rooms'][0].update(aliases='dock'),
                   lambda n:n['rooms'][0].update(aliases_by_language={'ta':'வீடு'})):
        bad=deepcopy(names)
        mutate(bad)
        with pytest.raises(ValueError):
            Names(bad)


@pytest.mark.parametrize('text', ['', ' '*4, 'x'*301, None, 8, 'go\x00to reception'])
def test_bad_input_refuses(text,names):
    assert understand(text,names)['refusal']['template']=='invalid_input'


@pytest.mark.parametrize('text', ["don't go to reception", 'if Priya is there go to reception',
 'வரவேற்புக்கு போகாதே', 'பிரியா இருந்தால் போ', 'reception pogathe', 'റിസപ്ഷനിലേക്ക് പോകരുത്', 'రిసెప్షన్‌కు వెళ్లవద్దు'])
def test_negations_and_conditionals_short_circuit(text,names,form):
    backend=MockBackend([form(place='reception')])
    result=understand(text,names,settings={'backend':backend})
    assert result['refusal']['template']=='out_of_scope'
    assert backend.calls==0


def test_model_reason_never_displayed(names,form):
    model=MockBackend([form(out_of_scope=True,reason='SECRET INJECTED MESSAGE')])
    result=understand('make tea',names,settings={'backend':model})
    assert result['refusal']['template']=='out_of_scope'
    assert 'SECRET' not in json.dumps(result)


def test_default_missing_model_refuses(monkeypatch,names):
    from amr_language import backends
    def unavailable(*args,**kwargs):
        raise BackendError('private details')
    monkeypatch.setattr(backends.OllamaBackend,'sample',unavailable)
    result=understand('go to reception',names)
    assert result['refusal']['template']=='model_unavailable'
    assert 'private' not in json.dumps(result)


def test_no_calibration_never_acts(names,form):
    result=understand('go to reception',names,settings={'backend':MockBackend([form(place='reception')])})
    assert result['samples']==7
    assert result['refusal']['template']=='uncalibrated'
    assert result['steps'][0]['slots']['place']['candidates']['reception']==pytest.approx(1.)
    assert result['steps'][0]['slots']['place']['prediction_set']==[]


@pytest.mark.parametrize('error,template', [(BackendError(),'model_unavailable'),
                                         (BackendTimeout(),'model_timeout'), ({},'model_output_invalid')])
def test_backend_failure_is_structured(names,error,template):
    result=understand('go to reception',names,settings={'backend':MockBackend([error])})
    assert result['decision']=='refuse' and result['refusal']['template']==template


def test_invented_name_and_substring_refused(names,form):
    result=understand('go to server room',names,settings={'backend':MockBackend([form(place='reception')])})
    assert result['refusal']['template']=='model_output_invalid'
    assert not source_span('Ravi','go to Ravindra')
    assert source_span('manager cabin','manager cabin-ku file kudunga')
    assert not source_span('ர','ரவி') # A combining vowel is not a word boundary.
    assert not source_span('ര','രവി')
    assert not source_span('ర','రవి')


def test_unknown_name(names,form):
    result=understand('go to server room',names,settings={'backend':MockBackend([form(place='server room')])})
    assert result['refusal']['template']=='unknown_name'


def test_action_and_item_disagreement(names,form):
    forms=[form(place='reception'),form('check',place='reception')]
    result=understand('go to reception and check',names,settings={'backend':MockBackend(forms)})
    assert result['refusal']['template']=='inconsistent_plan'
    forms=[form('deliver',person='Priya',item='file'),form('deliver',person='Priya',item='parcel')]
    result=understand('take file or parcel to Priya',names,settings={'backend':MockBackend(forms)})
    assert result['refusal']['template']=='inconsistent_plan'


@pytest.mark.parametrize('settings', [{'samples':4},{'samples':11},{'samples':True},
 {'timeout_s':math.inf},{'temperature':math.nan},{'endpoint':'https://example.org'},
 {'model':'qwen-cloud'},{'num_ctx':262144},{'backend':object()}])
def test_invalid_settings(names,settings):
    assert understand('go to reception',names,settings=settings)['refusal']['template']=='invalid_settings'
