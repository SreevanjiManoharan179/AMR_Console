"""Map facts and location hints are caller data; suggestions never update maps."""
from copy import deepcopy
from datetime import datetime,timezone
import json

import pytest

from amr_language import understand,say,hear_reply
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_map import MapFacts,MapRequests
from amr_language.office_names import OfficeNames
from amr_language.rehearsal import Rehearsal


@pytest.fixture
def map_names(names,tmp_path,monkeypatch):
    import amr_language.office_map as module
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'LOG_ROOT',tmp_path/'records/map_requests')
    value=deepcopy(names)
    value['rooms'].append({'name':'new_pantry','aliases':['new pantry'],'on_map':False,'reachable':False,'nearest_mapped_place':'cafeteria'})
    value['requester_place']='home';value['map_id']='scripted-map'
    return value


def make_session(names,text,replies,language='en',**settings):
    iterator=iter(replies)
    backend=MockOfficeBackend([text]*4,[{}]*4)
    session=Rehearsal(names,{'backend':backend,**settings},input_fn=lambda _:next(iterator),output_fn=lambda _:None,source='SCRIPTED')
    return session,lambda:session.run(text,'Bharathwaj',language)


def test_targets_require_known_mapped_reachable_facts(map_names):
    text='Go to new pantry.';mock=MockOfficeBackend([text]*4,[{}]*4)
    result=understand(text,map_names,'Bharathwaj','en',{'backend':mock,'input_source':'SCRIPTED'})
    assert result['decision']=='ask' and result['ask']['event']=='place_not_on_map'
    assert result['ask']['options']==['cafeteria']
    map_names['rooms'][1].update(reachable=False,unreachable_reason='blocked')
    text='Go to reception.'
    result=understand(text,map_names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='refuse' and result['feasibility']['reason']=='blocked'
    next(p for p in map_names['people'] if p['name']=='Arun').pop('usual_place')
    text='Give the file to Arun.'
    result=understand(text,map_names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['ask']['event']=='ask_where_person'


def test_unmapped_room_asks_for_a_place_without_creating_a_person(map_names):
    session, run = make_session(map_names, 'Go to the server room.', [])
    outcome = run()
    assert outcome['status'] == 'finished'
    job = outcome['jobs'][0]
    assert job['decision'] == 'ask' and job['outcomes'] == []
    assert job['result']['steps'] == []
    question = job['result']['ask']
    assert question['event'] == 'unknown_place'
    assert question['query'] == 'the server room'
    mapped = MapFacts(OfficeNames(map_names, 'Bharathwaj')).mapped(reachable=True)
    assert set(question['options']) <= set(mapped)
    assert 'not on my map' in job['result']['readback']
    assert not any(row.get('event') in ('new_person', 'start_step') for row in session.records)


@pytest.mark.parametrize('language',['en','ta','ml','te'])
def test_map_templates_are_fixed_in_all_languages(map_names,language):
    for event,slots in [('place_not_on_map',{'place':'new_pantry','nearest':'cafeteria'}),
        ('place_unreachable',{'place':'storage','reason':'blocked'}),
        ('ask_where_person',{'recipient':'Arun','options':['open_office','team_office']}),
        ('ask_people_where',{'recipient':'Divya'}),('person_seen_elsewhere',{'recipient':'Divya','place':'conference_hall'}),
        ('map_request_logged',{})]:
        line=say(event,language,names=map_names,requester='Bharathwaj',**slots)
        assert line and '{' not in line and '}' not in line
    with pytest.raises(ValueError):say('place_unreachable',language,names=map_names,requester='Bharathwaj',place='storage',reason='MODEL TEXT')
    assert hear_reply('மாநாட்டு அறை',{'location','not_here'},'ta')['answer_original']=='மாநாட்டு அறை'
    assert hear_reply('ఆపండి',{'location'},'en')['kind']=='stop'


def test_off_map_alternative_needs_new_yes_and_never_changes_names(map_names):
    before=deepcopy(map_names)
    session,run=make_session(map_names,'Go to new pantry.',['cafeteria','no'])
    result=run()
    assert not any(r.get('event')=='start_step' for r in session.records)
    assert map_names==before
    session,run=make_session(map_names,'Go to new pantry.',['cafeteria','yes'])
    result=run()
    assert result['jobs'][0]['outcomes'][0]['target']=='cafeteria'
    assert map_names==before


def test_unreachable_later_and_missing_person_location(map_names):
    next(p for p in map_names['rooms'] if p['name']=='storage').update(reachable=False,unreachable_reason='no_route')
    session,run=make_session(map_names,'Go to storage.',['later'])
    assert run()['jobs'][0]['decision']=='refuse'
    next(p for p in map_names['people'] if p['name']=='Arun').pop('usual_place')
    session,run=make_session(map_names,'Give the file to Arun.',['open office','yes','taken'])
    result=run()
    assert result['jobs'][0]['outcomes'][0]['status']=='delivered'
    assert 'usual_place' not in next(p for p in map_names['people'] if p['name']=='Arun')


def test_person_hint_is_confirmed_before_any_new_step(map_names):
    text='Give the file to Divya.'
    session,run=make_session(map_names,text,['yes','not here','conference hall','no'])
    result=run()
    assert not any(r['kind']=='confirmed_hint_step' for r in session.records)
    session,run=make_session(map_names,text,['yes','not here','conference hall','yes','taken'])
    result=run()
    assert result['jobs'][0]['outcomes'][0]['at']=='conference_hall'
    assert any(r['kind']=='hint_confirmation' and r['confirmed'] for r in session.records)


def test_hint_never_or_unmapped_follows_absence_policy(map_names):
    text='Give the file to Divya.'
    for hint,settings in [('conference hall',{'follow_person_hint':'never'}),('new pantry',{})]:
        session,run=make_session(map_names,text,['yes','not here',hint],**settings)
        result=run()
        assert not any(r['kind']=='confirmed_hint_step' for r in session.records)
        assert result['jobs'][0]['outcomes'][0]['status']=='failed'


def test_three_confirmed_findings_log_suggestion_only(map_names):
    before=deepcopy(map_names)
    for _ in range(3):
        session,run=make_session(map_names,'Give the file to Divya.',['yes','not here','conference hall','yes','taken'],language='ta')
        assert run()['jobs'][0]['outcomes'][0]['status']=='delivered'
    import amr_language.office_map as module
    rows=[json.loads(l) for p in module.LOG_ROOT.glob('*.jsonl') for l in p.read_text().splitlines()]
    changes=[r for r in rows if r['kind']=='person_location_update_request']
    assert len(changes)==1 and changes[0]['observations']==3 and changes[0]['applied'] is False
    assert map_names==before


def test_log_cannot_write_through_names_or_map_links(map_names,tmp_path):
    import amr_language.office_map as module
    names_file=tmp_path/'names.yaml';map_file=tmp_path/'map.yaml'
    names_file.write_text('original names');map_file.write_text('original map')
    module.LOG_ROOT.mkdir(parents=True)
    path=module.LOG_ROOT/(datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d')+'.jsonl')
    path.symlink_to(names_file)
    with pytest.raises(OSError):MapRequests('test','SCRIPTED').append('place_update_request','Bharathwaj','new pantry','en','missing')
    assert names_file.read_text()=='original names' and map_file.read_text()=='original map'
    assert MapFacts(OfficeNames(map_names,'Bharathwaj')).nearest('new_pantry')=='cafeteria'


def test_log_rejects_hardlink_and_reports_failed_append(map_names,tmp_path):
    import os
    import amr_language.office_map as module
    names_file=tmp_path/'names.yaml';names_file.write_text('owner names sentinel')
    module.LOG_ROOT.mkdir(parents=True)
    path=module.LOG_ROOT/(datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d')+'.jsonl')
    os.link(names_file,path)
    with pytest.raises(ValueError):MapRequests('test','SCRIPTED').append('place_update_request','Bharathwaj','new pantry','en','missing')
    text='Go to new pantry.'
    result=understand(text,map_names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['steps']==[]
    assert result['ask']['log_status']=='failed'
    assert result['readback']==say('map_request_failed','en')
    assert names_file.read_text()=='owner names sentinel'


def test_unverified_reachability_and_missing_usual_never_act(map_names):
    reception=next(p for p in map_names['rooms'] if p['name']=='reception')
    reception.pop('reachable')
    text='Go to reception.'
    result=understand(text,map_names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='refuse' and result['feasibility']['reason']=='not_verified'
    person=next(p for p in map_names['people'] if p['name']=='Divya')
    assert person.get('seat')  # legacy seat alone is not an approved usual_place
    person.pop('usual_place')
    text='Give the file to Divya.'
    result=understand(text,map_names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['ask']['event']=='ask_where_person'


def test_an_unmapped_room_names_a_few_mapped_places_to_choose_from(map_names):
    text = 'Go to the server room.'
    result = understand(text, map_names, 'Bharathwaj', 'en', {'backend': MockOfficeBackend([text] * 4, [{}] * 4)})
    assert result['ask']['event'] == 'unknown_place'
    if result['ask']['options']:
        assert 'Which one do you mean:' in result['readback']
        mapped = MapFacts(OfficeNames(map_names, 'Bharathwaj')).mapped(reachable=True)
        assert set(result['ask']['options']) <= set(mapped)
