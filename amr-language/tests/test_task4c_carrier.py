from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import pytest
import yaml
from amr_language.office import understand
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_capabilities import item_assessment,validate_capability
from amr_language.office_names import OfficeNames
from amr_language.office_schema import walk_steps
from amr_language.office_speech import say,LAB_ITEMS,OK_QUESTION
from amr_language.rehearsal import Rehearsal

spec=importlib.util.spec_from_file_location('task4c_rehearsals',Path('scripts/rehearse_task4c.py'))
scenarios=importlib.util.module_from_spec(spec);spec.loader.exec_module(scenarios)


def run(names,text,rewrites=None):
    return understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend(rewrites or [text]*4,[{}]*4)})


@pytest.mark.parametrize('case',scenarios.SCENARIOS,ids=lambda c:c[0])
@pytest.mark.parametrize('language',['en','ta'])
def test_scripted_conversations(case,language):
    row=scenarios.run_scenario(case,language)
    key=case[0];events=row['events'];jobs=row['result']['jobs']
    if key=='place':assert any(r.get('exact_answer')=='  Two visitors came.  ' and r['confirmed'] for r in events)
    if key=='corrected':
        ver=[r for r in events if r['kind']=='answer_verification']
        assert [r['confirmed'] for r in ver]==[False,True]
        assert not any('three' in r['text'] or 'four' in r['text'] for r in events if r['kind']=='robot')
    if key=='position':assert not any(r['kind']=='confirmed_hint_step' for r in events)
    if key=='queue':
        assert len(jobs)==2 and all(len(list(walk_steps(j['result']['steps'])))<=6 for j in jobs)
        assert sum(r['kind']=='confirmation' for r in events)==2
        assert sum(o['status']=='checked' for j in jobs for o in j['outcomes'])==1
    if key.startswith('helper_') and key!='helper_denied':
        outcome=jobs[0]['outcomes'][0];assert outcome.get('provenance')=='helper' and outcome.get('helper_name')=='Visitor'
    if key=='helper_denied':assert not any(r.get('event')=='helper_deliver' for r in events)
    if key=='new_person':assert any(r['kind']=='learning_suggestion' and r['kind_name']=='person_place' for r in events)
    if key=='guess_not_found':assert jobs[0]['outcomes'][0]['not_found_at']=='reception'
    if key=='hint':assert jobs[0]['result']['steps'][0]['item_hint']=='I left on the bench'
    if key=='omitted':assert jobs[0]['result']['omissions'][0]['text']=='make tea'


@pytest.mark.parametrize('metadata,reason',[({'weight_class':'heavy'},'too_heavy'),({'fits_tray':False},'does_not_fit')])
def test_named_payload_limits_prevent_act(names,metadata,reason):
    names['objects'].append({'name':'payload','room':'storage','aliases':['motor'],**metadata})
    result=run(names,'Deliver the motor to Divya.')
    assert result['decision']=='refuse' and result['refuse']['reason']==reason and 'collect small items' in result['readback']


@pytest.mark.parametrize('item',['cash','knife','soldering iron','hot tool','open hot drink','liquid without a lid','oversized item'])
def test_class_refusals(names,item):
    result=run(names,'Deliver the '+item+' to Divya.')
    # Grammar may decline a structural noun phrase; model fallback must also be safe.
    if item=='liquid without a lid':
        form={'issue':None,'steps':[{'action':'deliver','item':item,'to':'Divya'}]}
        result=understand('Deliver the '+item+' to Divya.',names,'Bharathwaj',settings={'backend':MockOfficeBackend(['Deliver the '+item+' to Divya.']*4,[form]*4)})
    assert result['decision']=='refuse' and result['steps']==[]


@pytest.mark.parametrize('text,reason',[('Open the door; then go to reception.','requires_door_operation'),('Take the stairs.','requires_stairs'),('Use the lift.','requires_lift'),('Clean the floor.','unsupported_job'),('Search the whole floor.','unsupported_job')])
def test_physical_scope(names,text,reason):
    result=run(names,text);assert result['decision']=='refuse' and result['refuse']['reason']==reason


@pytest.mark.parametrize('requirement',['requires_arm','requires_door_operation','requires_lift','requires_stairs'])
def test_route_requirements(names,requirement):
    names['rooms'][1][requirement]=True
    result=run(names,'Go to reception.');assert result['refuse']['reason']==requirement


def test_capability_example_has_no_real_numbers_and_is_not_executable(names):
    example=yaml.safe_load(Path('data/robot_capability.example.yaml').read_text())
    def visit(value):
        if isinstance(value,dict):return all(visit(v) for v in value.values())
        if isinstance(value,list):return all(visit(v) for v in value)
        return type(value) not in (int,float)
    assert visit(example) and validate_capability(example) is None
    names['robot_capability']=example
    assert run(names,'Go to reception.')['refuse']['reason']=='capability_unset'
    names.pop('robot_capability');assert run(names,'Go to reception.')['refuse']['reason']=='capability_unset'


@pytest.mark.parametrize('bad',[0,7,True,'owner_to_set'])
def test_step_limit_configuration(names,bad):
    profile=deepcopy(names['robot_capability']);profile['maximum_steps_per_job']=bad
    with pytest.raises(ValueError):validate_capability(profile)


@pytest.mark.parametrize('flag,value',[('weight_class','enormous'),('fits_tray','yes'),('fragile',1),('personal',1),('restricted','true')])
def test_item_metadata_validated(names,flag,value):
    names['objects'][0][flag]=value
    with pytest.raises(ValueError):OfficeNames(names,'Bharathwaj')


def test_known_weight_and_fit_motor_fragile_bag(names):
    names['objects'].append({'name':'motor_payload','room':'storage','aliases':['motor'],'weight_class':'light','fits_tray':True})
    roster=OfficeNames(names,'Bharathwaj');cap=validate_capability(names['robot_capability'])
    assert item_assessment('motor',roster,cap)['rule']=='ok'
    names['objects'][-1]['fragile']=True
    assert item_assessment('motor',OfficeNames(names,'Bharathwaj'),cap)['rule']=='ask_at_pickup'
    assert item_assessment('battery pack',roster,cap)['requires_bag'] is True


@pytest.mark.parametrize('flag',['personal','restricted'])
def test_helper_cannot_receive_personal_or_restricted(names,flag):
    names['objects'].append({'name':'private_charger','room':'storage','aliases':['charger'],flag:True})
    result=run(names,'Deliver the charger to Divya; if she is not there, ask anyone there.')
    assert result['decision']=='refuse' and result['refuse']['reason']=='helper_restricted'


def test_item_hints_and_guess_are_copied_without_map_invention(names):
    result=run(names,'Get the charger near the 3D printer from Ravi.')
    assert result['decision']=='act' and result['steps'][0]['from']=='Ravi'
    assert result['steps'][0]['item_hint']=='near the 3D printer'
    assert '3D printer' not in [s.get('to') for s in result['steps']]
    assert run(names,'I think I left the encoder at reception.')['steps'][0]['item_location']=='guess'


@pytest.mark.parametrize('language',['en','ta','ml','te'])
def test_fixed_speech_preserves_technical_terms_and_short_readback(names,language):
    for word in LAB_ITEMS:
        if word=='file':continue
        text=say('confirm_plan',language,names=names,requester='Bharathwaj',steps=[{'n':1,'action':'deliver','item':word,'to':'Divya'}])
        assert word in text and text.endswith(OK_QUESTION[language])
    with pytest.raises(ValueError):say('verify_answer',language,answer='arbitrary answer')
    with pytest.raises(ValueError):say('carrier_refusal',language,reason='model free text')


def test_rewrite_cannot_change_english_encoder_in_mixed_input(names):
    result=understand('encoder ரவியிடம் கொடு',names,'Bharathwaj','ta',{'backend':MockOfficeBackend(['Deliver the decoder to Ravi.']*4,[{}]*4)})
    assert result['decision']!='act'


def test_disagreement_in_queue_never_authorizes_prefix(names):
    prefix='; then '.join(['Go to reception']*6)
    result=run(names,prefix,rewrites=[prefix+'; then go to cafeteria.',prefix+'; then go to storage.']*2)
    assert result['decision']=='ask' and result['steps']==[]


def test_failed_collection_skips_dependent_item_keeps_independent_steps(names):
    text='Get the widget from reception; then deliver the widget to Ravi; then go home.'
    replies=iter(['yes','done','no'])
    session=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(replies),output_fn=lambda _:None,source='SCRIPTED')
    result=session.run(text,'Bharathwaj','en')
    assert [o['status'] for o in result['jobs'][0]['outcomes']]==['failed','skipped','went']
    assert not any(r.get('event')=='arrive_deliver' for r in session.records)


def test_queued_part_needs_another_yes(names):
    text='; then '.join(['Go to reception']*7)
    replies=iter(['yes','no']);s=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(replies),output_fn=lambda _:None,source='SCRIPTED')
    result=s.run(text,'Bharathwaj','en')
    assert result['jobs'][1]['decision']=='cancelled'
    assert len([r for r in s.records if r.get('event')=='start_step'])==6


def test_helper_wrong_item_never_returns_it(names):
    text='Get the encoder from Karthik.';replies=iter(['yes','reception','yes','no','done','no'])
    session=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(replies),output_fn=lambda _:None,source='SCRIPTED')
    result=session.run(text,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['status']=='failed'
    assert not any(r.get('event')=='arrive_deliver' for r in session.records)


def test_stop_during_exact_answer_bypasses_backend(names):
    text='Ask at reception: Is it free?';backend=MockOfficeBackend([text]*4,[{}]*4);replies=iter(['yes','நிறுத்து'])
    s=Rehearsal(names,{'backend':backend},input_fn=lambda _:next(replies),output_fn=lambda _:None,source='SCRIPTED')
    assert s.run(text,'Bharathwaj','ta')['status']=='stopped'
    assert not any(c[0]=='answer' for c in backend.calls)


def test_personal_delivery_requires_named_person_not_place_or_alternate(names):
    names['objects'].append({'name':'private_encoder','room':'storage','aliases':['encoder'],'personal':True})
    for text in ('Deliver the encoder to reception.','Deliver the encoder to Divya; if she is not there, leave it with Priya.'):
        assert run(names,text)['refuse']['reason']=='helper_restricted'
    assert run(names,'Deliver the encoder to Divya.')['decision']=='act'


def test_public_free_answer_is_exact_and_does_not_use_model(monkeypatch):
    import amr_language.office_replies as replies
    monkeypatch.setattr(replies,'make_backend',lambda *a:(_ for _ in ()).throw(AssertionError('Must not rewrite answers')))
    for text in ('  Two visitors came.  ','இரண்டு பேர் வந்தார்கள்'):
        assert replies.hear_reply(text,{'answer'},'ta')['answer_original']==text


def test_alias_clarification_suggested_only_after_new_yes(names):
    text='Go to doctor.';answers=iter(['home','yes'])
    s=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(answers),output_fn=lambda _:None,source='SCRIPTED')
    assert s.run(text,'Bharathwaj','en')['jobs'][0]['outcomes'][0]['target']=='home'
    assert any(r['kind']=='learning_suggestion' and r['kind_name']=='alias' for r in s.records)


def test_confirmed_correction_only_appends_suggestion(names):
    text='Go home.';candidate={'language':'en','word':'adapter','meaning':'small cable adapter'}
    answers=iter(['/correction '+json.dumps(candidate),'yes','no'])
    s=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(answers),output_fn=lambda _:None,source='SCRIPTED')
    assert s.run(text,'Bharathwaj','en')['jobs'][0]['decision']=='cancelled'
    assert any(r['kind']=='learning_suggestion' and r['kind_name']=='office_meaning' for r in s.records)


def test_new_person_is_discarded_for_next_requester_job(names):
    text='Deliver the charger to Karthik.';answers=iter(['yes','reception','yes','yes','taken','no'])
    s=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(answers),output_fn=lambda _:None,source='SCRIPTED')
    s.queue.append({'text':text,'requester':'Ravi','language':'en'})
    result=s.run(text,'Bharathwaj','en')
    assert result['jobs'][0]['decision']=='rehearsed' and result['jobs'][1]['decision']=='ask'
    assert not s.roster.temporary and not names.get('temporary_people')


def test_configured_smaller_limit_partitions_before_act(names):
    names['robot_capability']['maximum_steps_per_job']=2
    text='Go to reception; then go home; then go to cafeteria.'
    result=run(names,text)
    assert result['decision']=='act' and len(result['steps'])==2 and len(result['queued_parts'][0]['plan']['steps'])==1


def test_alternate_and_requester_routes_get_physical_checks(names):
    roster=OfficeNames(names,'Bharathwaj')
    priya=next(p for p in names['people'] if p['name']=='Priya')
    seat=next(p for p in names['seats'] if p['id']==priya['seat']);seat['requires_door_operation']=True
    result=run(names,'Deliver the charger to Divya; if she is not there, leave it with Priya.')
    assert result['refuse']['reason']=='requires_door_operation'
    names['requester_place']='reception';names['rooms'][1]['requires_stairs']=True
    assert run(names,'Get the charger from Ravi.')['refuse']['reason']=='requires_stairs'


def test_transient_and_helper_names_are_screen_data_only():
    row=scenarios.run_scenario(next(c for c in scenarios.SCENARIOS if c[0]=='helper_allowed'),'en')
    spoken=[r['text'] for r in row['events'] if r['kind']=='robot']
    assert not any('Karthik' in text or 'Visitor' in text for text in spoken)
    assert any('a helper received' in text for text in spoken)


def test_position_new_step_requires_requester_yes_after_answer_verification(names):
    text='Ask Divya where she is now.';answers=iter(['yes','conference hall','yes','yes'])
    session=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(answers),output_fn=lambda _:None,source='SCRIPTED')
    result=session.run(text,'Bharathwaj','en')
    assert result['status']=='finished'
    assert any(r['kind']=='requester_position_confirmation' and r['confirmed'] for r in session.records)
    assert any(o['status']=='went' and o['target']=='conference_hall' for o in result['jobs'][0]['outcomes'])


def test_unclear_pickup_cancels_and_never_delivers(names):
    text='Get the widget from reception.';answers=iter(['yes','done','maybe','perhaps','not sure'])
    session=Rehearsal(names,{'backend':MockOfficeBackend([text]*4,[{}]*4)},input_fn=lambda _:next(answers),output_fn=lambda _:None,source='SCRIPTED')
    result=session.run(text,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['reason']=='pickup_declined'
    assert not any(r.get('event')=='arrive_deliver' for r in session.records)


def test_conditional_group_cannot_be_split_across_parts(names):
    names['robot_capability']['maximum_steps_per_job']=2
    text='Ask at reception: Is it free?; if yes, tell Ravi: ready; if no, tell Priya: busy.'
    result=run(names,text)
    assert result['decision']=='ask' and result['ask']['event']=='split_job' and result['steps']==[]


def test_invalid_physical_route_flags_fail_closed(names):
    names['rooms'][1]['requires_stairs']='yes'
    result=run(names,'Go to reception.')
    assert result['decision']=='refuse' and result['refuse']['reason']=='invalid_context'
