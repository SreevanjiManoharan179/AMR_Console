import csv
from pathlib import Path
import time

import pytest

from amr_language import understand as public_understand,say,hear_reply
from amr_language.backends import BackendTimeout
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_names import OfficeNames
from amr_language.office_schema import validate_plan,normalized_plan,TooManySteps
from amr_language.office_speech import REQUIRED_EVENTS,TEMPLATES,REVIEW_STATUS
from amr_language.rehearsal import Rehearsal

def understand(text,names,requester,language=None,settings=None):
    return public_understand(text,names,requester,language,{**(settings or {}),'planner_mode':'model','draw_policy':'four'})



def step(action='deliver',**slots):
    return {'action':action,'if_not_there':'bring_back',**slots}


def plan(*steps,issue=None): return {'steps':list(steps),'issue':issue}


def backend(english,form): return MockOfficeBackend([english]*4,[form]*4)


def test_schema_v3_defaults_fields_and_six_total_actions():
    value=normalized_plan(plan(step('retrieve',item='charger',**{'from':'Arun'})),'Bharathwaj')
    assert value['steps'][0]['to']=='Bharathwaj'
    assert value['steps'][0]['n']==1
    for bad in (plan(step('fly',to='reception')),plan(step(to='Priya')),
                plan(step(item='file',to='Priya',coordinates=[1,2])),
                plan(step(item='file',to='Priya',if_not_there='try_later:0')),
                plan(step(item='file',to='Priya',if_not_there='try_later:1441'))):
        with pytest.raises(ValueError): validate_plan(bad)
    branch=step('check',at='reception',question='Is it free?',on_answer={
        'yes':[step('tell',to='Ravi',message='It is free.')],'no':[step(item='file',to='Priya')]})
    validate_plan(plan(branch,*[step('go',to='home') for _ in range(3)]))
    with pytest.raises(TooManySteps): validate_plan(plan(branch,*[step('go',to='home') for _ in range(4)]))
    # 0.6.0: a branch may ask a further question, but never branch again.
    branch['on_answer']['yes']=[step('check',at='reception',question='Why?',on_answer=None)]
    validate_plan(plan(branch))
    branch['on_answer']['yes']=[step('check',at='reception',question='Why?',on_answer={'yes':[],'no':[]})]
    with pytest.raises(ValueError): validate_plan(plan(branch))


def test_one_greedy_three_sampled_rewrites_act_only_after_all_agree(names):
    english='Deliver the file to Divya.'
    mock=backend(english,plan(step(item='file',to='Divya')))
    result=understand('இந்த கோப்பை திவ்யாவிடம் கொடுங்க.',names,'Bharathwaj','ta',{'backend':mock})
    assert result['decision']=='act' and result['steps'][0]['to']=='Divya'
    assert [call for call in mock.calls if call[0]=='rewrite']==[
        ('rewrite',0,0.),('rewrite',1,.7),('rewrite',2,.7),('rewrite',3,.7)]
    assert 'file' not in result['readback'] # Fixed Tamil item lexicon, not model prose.


def test_disagreement_unknown_and_ambiguous_ask(names):
    english='Deliver the file to Divya or Priya.'
    forms=[plan(step(item='file',to=name)) for name in ('Divya','Divya','Priya','Divya')]
    result=understand(english,names,'Bharathwaj',settings={'backend':MockOfficeBackend([english]*4,forms),'deduplicate_plans':False})
    assert result['decision']=='ask' and result['ask']['options']==['Divya','Priya']
    for text,options in [('Go to office.',['open_office','team_office']),('Go to server room.',None)]:
        target='office' if options else 'server room'
        result=understand(text,names,'Bharathwaj',settings={'backend':backend(text,plan(step('go',to=target)))})
        assert result['decision']=='ask'
        if options: assert result['ask']['options']==options


def test_failed_pairs_never_make_surviving_plan_act(names):
    english='Deliver the file to Divya.'
    good=plan(step(item='file',to='Divya'))
    forms=[good,{},good,good]
    result=understand(english,names,'Bharathwaj',settings={'backend':MockOfficeBackend([english]*4,forms),'deduplicate_plans':False})
    assert result['decision']=='ask'
    assert result['ask']['event']=='clarify_request'


def test_sources_compare_with_english_rewrite_and_reject_invented_slots(names):
    english='Deliver the file to Divya.'
    result=understand('திவ்யாவிடம் கோப்பை கொடு',names,'Bharathwaj','ta',
                       {'backend':backend(english,plan(step(item='file',to='Divya')))})
    assert result['decision']=='act'
    result=understand('திவ்யாவிடம் கோப்பை கொடு',names,'Bharathwaj','ta',
                       {'backend':backend(english,plan(step(item='file',to='Priya')))})
    assert result['decision']=='refuse' and result['steps']==[]


def test_conditions_and_unsupported_requests(names):
    english='Check at reception: "Is it free?" If yes, tell Ravi: "It is free."'
    form=plan(step('check',at='reception',question='Is it free?',on_answer={
        'yes':[step('tell',to='Ravi',message='It is free.')],'no':[]}))
    result=understand(english,names,'Bharathwaj',settings={'backend':backend(english,form)})
    assert result['decision']=='act' and result['steps'][0]['on_answer']['yes'][0]['n']==2
    for issue in ('out_of_scope','unsupported_condition','negation'):
        result=understand('Make tea if the lights are on.',names,'Bharathwaj',
                           settings={'backend':backend('Make tea if the lights are on.',plan(issue=issue))})
        assert result['decision']=='refuse' and result['refuse']['reason']==issue
    english='Deliver the file to Divya if it rains.'
    result=understand(english,names,'Bharathwaj',settings={'backend':backend(english,plan(step(item='file',to='Divya')))})
    assert result['decision']=='refuse' and result['refuse']['reason']=='unsupported_condition'


def test_dropped_alternate_and_uncollected_retrieval_cannot_pass(names):
    english='Deliver the file to Divya; if not there leave it with Priya.'
    result=understand(english,names,'Bharathwaj',settings={'backend':backend(english,plan(step(item='file',to='Divya')))})
    assert result['decision']=='refuse'
    with pytest.raises(ValueError):
        validate_plan(plan(step('retrieve',item='charger',if_not_there='leave_with:Priya',**{'from':'Arun'})))


def test_no_unrequested_return_and_requester_is_not_a_room(names):
    english='Give the file to Divya.'
    form=plan(step(item='file',to='Divya'),step('go',to='Bharathwaj'))
    result=understand(english,names,'Bharathwaj',settings={'backend':backend(english,form)})
    assert result['decision']=='refuse'
    english='Give the file to Divya, then come back to me.'
    result=understand(english,names,'Bharathwaj',settings={'backend':backend(english,form)})
    assert result['decision']=='act'
    assert OfficeNames(names,'Bharathwaj').match('me')==('Bharathwaj',)
    with pytest.raises(ValueError): OfficeNames(names,'home')


def test_seven_actions_ask_split_and_deadline_refuses(names):
    english='\n'.join(f'{i}. Go to reception.' for i in range(1,8))
    result=understand('Go to seven places.',names,'Bharathwaj',settings={'backend':backend(english,plan())})
    assert result['decision']=='ask' and result['ask']['event']=='split_job'
    result=understand('Go to reception.',names,'Bharathwaj',settings={'backend':MockOfficeBackend([BackendTimeout()],[])})
    assert result['refuse']['reason']=='model_timeout'


@pytest.mark.parametrize('language',['en','ta','ml','te'])
def test_every_template_and_placeholder_is_fixed_and_fillable(language,names):
    assert set(REQUIRED_EVENTS)<=set(TEMPLATES)
    if language!='en': assert REVIEW_STATUS[language]=='needs native-speaker check'
    delivery=normalized_plan(plan(step(item='file',to='Divya')),'Bharathwaj')['steps'][0]
    slots={
      'confirm_plan':{'steps':[delivery]},'queued':{'current':'Ravi','position':2,'minutes':4},
      'start_step':{'step':delivery},'arrive_deliver':{'recipient':'Divya','item':'file'},
      'arrive_retrieve':{'item':'charger'},'arrive_check':{},'arrive_tell':{'recipient':'Ravi'},
      'reminder':{'recipient':'Divya','item':'file'},'got_reply':{},
      'not_there':{'recipient':'Divya','policy':'leave_with:Priya'},'step_done':{},
      'report':{'outcomes':[{'status':'delivered','target':'Priya','item':'file'}]},
      'job_failed':{'reason':'no_reply'},'returning_home':{},'idle':{},
      'ask_which':{'options':['open_office','team_office']},'unknown_name':{},'cannot_do':{},'stopped':{},
    }
    for event,values in slots.items():
        result=say(event,language,names=names,requester='Bharathwaj',**values)
        assert isinstance(result,str) and result and '{' not in result and '}' not in result
    for key,translations in TEMPLATES.items(): assert set(translations)=={'en','ta','ml','te'}


def test_model_payloads_and_unknown_names_never_enter_speech(names):
    for payload in ('message','question','answer','english','text'):
        with pytest.raises(ValueError): say('arrive_check','en',names=names,requester='Bharathwaj',**{payload:'MODEL INJECTION'})
    with pytest.raises(ValueError): say('arrive_tell','en',names=names,requester='Bharathwaj',recipient='UNAPPROVED')
    form=step('tell',to='Ravi',message='MODEL INJECTION')
    form['n']=1
    spoken=say('confirm_plan','en',names=names,requester='Bharathwaj',steps=[form])
    assert 'MODEL INJECTION' not in spoken
    assert 'pass on your message' in spoken
    assert 'MODEL INJECTION' not in say('arrive_deliver','en',names=names,requester='Bharathwaj',recipient='Priya',item='MODEL INJECTION')


def test_reply_lists_context_stop_and_no_model_for_short_replies(monkeypatch):
    import amr_language.office_replies as replies
    monkeypatch.setattr(replies,'make_backend',lambda config:(_ for _ in ()).throw(AssertionError('No model for short replies')))
    started=time.monotonic()
    with Path('data/replies_check.csv').open(newline='') as stream:
        rows=list(csv.DictReader(stream))
    for language in ('en','ta','ml','te'):
        assert sum(row['language']==language for row in rows)>=15
    for row in rows:
        result=hear_reply(row['text'],set(row['expect'].split('|')),row['language'])
        assert result['kind']==row['expected_kind'],row
    assert hear_reply('இல்ல',{'not_here'},'ta')['kind']=='not_here'
    assert hear_reply('இல்ல',{'no'},'ta')['kind']=='no'
    assert hear_reply('இல்ல',{'not_here','no'},'ta')['kind']=='unclear'
    assert hear_reply('please ஆபండి',{'taken'},'en')['kind']=='unclear' # Not the approved Telugu spelling.
    assert hear_reply('please ఆపండి',{'answer'},'en')['kind']=='stop'
    assert hear_reply('unstoppable',{'taken'},'en')['kind']=='unclear'
    assert time.monotonic()-started<1.


def session_for(names,english,form,replies):
    iterator=iter(replies)
    mock=backend(english,form)
    return Rehearsal(names,{'backend':mock,'planner_mode':'model','draw_policy':'four'},input_fn=lambda prompt:next(iterator),output_fn=lambda text:None)


def test_rehearsal_requires_yes_and_handles_alternate_person_languages(names):
    names['people'][-1]['language']='en'
    names['people'][0]['language']='ta'
    english='Deliver the file to Divya; if absent, leave it with Priya.'
    form=plan(step(item='file',to='Divya',if_not_there='leave_with:Priya'))
    session=session_for(names,english,form,['yes','not here','not here','எடுத்துக்கிட்டேன்'])
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['target']=='Priya'
    assert any(r['kind']=='robot' and r['event']=='arrive_deliver' and r['language']=='ta' for r in session.records)
    session=session_for(names,english,form,['no'])
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['decision']=='cancelled'
    assert not any(r.get('event')=='start_step' for r in session.records)


def test_retrieve_answer_branch_queue_and_stop(names):
    english='Retrieve the charger from Arun and bring it to Bharathwaj.'
    form=plan(step('retrieve',item='charger',**{'from':'Arun','to':'Bharathwaj'}))
    session=session_for(names,english,form,['yes','done','taken'])
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['status']=='retrieved'
    english='Check at reception: "Is it free?" If yes, tell Ravi: "It is free."'
    form=plan(step('check',at='reception',question='Is it free?',on_answer={
        'yes':[step('tell',to='Ravi',message='It is free.')],'no':[]}))
    session=session_for(names,english,form,['yes','yes','yes','done'])
    result=session.run(english,'Bharathwaj','en')
    assert any(o['status']=='told' for o in result['jobs'][0]['outcomes'])
    assert [o['status'] for o in result['jobs'][0]['outcomes']]==['checked','told']
    assert not any('It is free.' in r['text'] for r in session.records if r['kind']=='robot')
    english='Deliver the file to Divya.'
    form=plan(step(item='file',to='Divya'))
    session=session_for(names,english,form,['/queue Ravi ta Give the file to Divya.','yes','taken','no'])
    result=session.run(english,'Bharathwaj','en')
    assert len(result['jobs'])==2
    assert any(r.get('event')=='queued' and r['language']=='ta' for r in session.records)
    session=session_for(names,english,form,['yes','ఆపండి'])
    result=session.run(english,'Bharathwaj','en')
    assert result['status']=='stopped'
    assert not any(r.get('event')=='returning_home' for r in session.records)


@pytest.mark.parametrize('policy,reply,status',[('skip','not here','skipped'),('try_later:15','not here','deferred'),
                                             ('bring_back','later','deferred')])
def test_absence_and_later_rehearsal_do_not_claim_finished_delivery(names,policy,reply,status):
    english='Deliver the file to Divya.'+(' Try later in 15 minutes.' if policy.startswith('try_later') else '')
    form=plan(step(item='file',to='Divya',if_not_there=policy))
    session=session_for(names,english,form,['yes',reply]+(['not here'] if reply=='not here' else []))
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['status']==status
    if status=='deferred':
        assert result['status']=='deferred'
        assert not any(r.get('event')=='idle' for r in session.records)


def test_ask_anyone_and_tell_alternate_use_the_right_action(names):
    english='Deliver the file to Divya; if not there ask anyone there.'
    form=plan(step(item='file',to='Divya',if_not_there='ask_anyone'))
    session=session_for(names,english,form,['yes','not here','/helper_name Priya','taken'])
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['helper_name']=='Priya' and result['jobs'][0]['outcomes'][0]['provenance']=='helper'
    english='Tell Divya: "ready"; if not there leave the message with Priya.'
    form=plan(step('tell',to='Divya',message='ready',if_not_there='leave_with:Priya'))
    session=session_for(names,english,form,['yes','not here','not here','done'])
    result=session.run(english,'Bharathwaj','en')
    assert result['jobs'][0]['outcomes'][0]['status']=='told'
    assert not any(r.get('event')=='arrive_deliver' for r in session.records)
