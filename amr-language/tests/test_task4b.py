"""Review regressions and explicit questions; authored holdout never drives fixes."""
import hashlib,json
from pathlib import Path
import pytest
from amr_language import understand,say
from amr_language.office_grammar import parse_english
from amr_language.office_backend import MockOfficeBackend,settings_for_office
from amr_language.office_names import OfficeNames


@pytest.mark.parametrize('who',["she's","he's","they're","she isn't","he isn't","they aren't"])
@pytest.mark.parametrize('location',['here','there'])
def test_contracted_absence_is_structural_only(who,location,names):
    verb='not ' if who.endswith(("'s","'re")) else ''
    text=f'Give the file to Divya; if {who} {verb}{location}, give it to Priya.'
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='act' and result['steps'][0]['if_not_there']=='leave_with:Priya'


@pytest.mark.parametrize('text',[
 'Get the charger from Arun and bring it here.',
 'Take the charger from Arun and bring it to me.',
 'Get the charger that is with Arun and bring it back to me.',
 'Could you bring me the charger that is with Arun?',
 'Please get the charger from Arun to me.',
])
def test_retrieval_sources_and_requester_returns(text,names):
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='act'
    assert result['steps'][0]['from']=='Arun' and result['steps'][0]['to']=='Bharathwaj'
    assert result['steps'][0]['item']=='charger'


@pytest.mark.parametrize('text',[
 'Get the charger that is from Arun to me.',
 'Take the charger to Arun to me.',
 'Get the charger from Arun and bring it here while opening the door.',
 'Get the charger from Arun quickly.',
 'Give the file to Divya and dance.',
])
def test_unexplained_clause_falls_back_instead_of_becoming_entity(text,names):
    assert parse_english(text,'Bharathwaj') is None
    backend=MockOfficeBackend([text]*4,[{'steps':[],'issue':'out_of_scope'}]*4)
    result=understand(text,names,'Bharathwaj','en',{'backend':backend})
    assert result['planner']=='model' and result['decision']!='act'


@pytest.mark.parametrize('language',['en','ta','ml','te'])
def test_buy_collect_is_question_never_silent_mapping(names,language):
    text='Buy the charger from Arun.'
    result=understand(text,names,'Bharathwaj',language,{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['steps']==[]
    assert result['ask']['event']=='confirm_collect' and result['ask']['recipient']=='Arun'
    assert result['ask']['requires_new_request'] is True
    assert 'buy' not in result['readback'].lower()
    text='Buy the charger from the electronics shop.'
    result=understand(text,names,'Bharathwaj',language,{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='refuse'


@pytest.mark.parametrize('texts,slot,event',[
 (['Give the file to Divya.','Give the file to Priya.'],'to','clarify_person'),
 (['Go to reception.','Go to cafeteria.'],'to','clarify_place'),
 (['Give the file to Divya.','Give the cup to Divya.'],'item','clarify_item'),
 (["Give the file to Divya; if she's away, leave it with Priya.","Give the file to Divya; if she's away, bring it back."],'if_not_there','clarify_absence'),
 (['Tell Ravi: be ready at 3.','Tell Ravi: be ready in 3 hours.'],'message','clarify_message'),
 (['Ask at reception: Is the room free?','Ask at reception: Is the room occupied?'],'question','clarify_question'),
])
def test_question_is_about_first_differing_slot(names,texts,slot,event):
    result=understand(texts[0],names,'Bharathwaj','en',{'backend':MockOfficeBackend(texts*2,[{}]*4)})
    assert result['decision']=='ask' and result['draws']==4
    assert result['ask']['event']==event and result['ask']['slot']==slot
    assert 'plans' not in result['readback']
    if slot=='message':assert 'at 3' not in result['readback'] and 'in 3 hours' not in result['readback']


@pytest.mark.parametrize('query',['doctor','Daqu','dok'])
def test_home_is_an_offered_suggestion_not_a_guess(names,query):
    roster=OfficeNames(names,'Bharathwaj')
    assert 'home' in roster.suggestions(query,action='go')
    text='Go to '+query+'.'
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['steps']==[] and 'home' in result['ask']['options']


def test_contractions_inside_message_and_clocks_remain_literal():
    text="Tell Ravi: she's coming at 3 pm."
    assert parse_english(text)['steps'][0]['message']=="she's coming at 3 pm"


def test_temperature_is_configured_validated_and_traced(names):
    for temperature in (.3,.7):
        text='Give the file to Divya.';backend=MockOfficeBackend([text]*4,[{}]*4);trace=[]
        result=understand(text,names,'Bharathwaj','en',{'backend':backend,'sample_temperature':temperature,'trace':trace})
        assert result['decision']=='act' and backend.calls[1][2]==temperature
        assert trace[1]['rewrite_temperature']==temperature
    for value in (True,-.1,1.1,float('nan')):
        with pytest.raises(ValueError):settings_for_office({'sample_temperature':value})


def test_new_holdout_is_frozen(measurement_record):
    frozen=json.loads(measurement_record('records/task4b/holdout_v2_freeze.json').read_text())
    assert hashlib.sha256(Path(frozen['file']).read_bytes()).hexdigest()==frozen['sha256']


@pytest.mark.parametrize('language',['en','ta','ml','te'])
def test_slot_speech_uses_approved_labels_and_fixed_item_vocabulary(names,language):
    assert say('clarify_person',language,names=names,requester='Bharathwaj',options=['Divya','Priya'])
    assert say('clarify_place',language,names=names,requester='Bharathwaj',options=['home','reception'])
    line=say('clarify_item',language,names=names,requester='Bharathwaj',options=['UNTRUSTED PAYLOAD','file'])
    assert 'UNTRUSTED' not in line
    with pytest.raises(ValueError):say('clarify_person',language,names=names,requester='Bharathwaj',options=['UNTRUSTED PERSON'])
    assert say('clarify_absence',language,names=names,requester='Bharathwaj',recipient='Divya',policy='leave_with:Priya')


def test_invalid_rewrite_can_explain_a_question_but_never_authorize_an_act(names):
    texts=['Give the file to Divya.','Give the file to Priya.']
    trace=[]
    result=understand(texts[0],names,'Bharathwaj','en',{'backend':MockOfficeBackend(texts*2,[{}]*4),'trace':trace})
    assert result['decision']=='ask' and not result['steps'] and result['ask']['slot']=='to'
    rejected=[p for p in trace if p.get('error')=='invalid_rewrite']
    assert rejected and all('clarification_only_plan' in p and 'plan' not in p for p in rejected)


@pytest.mark.parametrize('language',['en','ta','ml','te'])
@pytest.mark.parametrize('text',[
 'Buy the charger from Arun and bring it to me.',
 'Buy the charger from Arun and bring it here.',
 'Give Priya the cup, buy the charger from Arun and bring it to me, then tell Ravi that I am ready.',
])
def test_saved_tamil_buy_return_clauses_ask_to_collect(names,language,text):
    result=understand(text,names,'Bharathwaj',language,{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['steps']==[]
    assert result['ask']['event']=='confirm_collect' and result['ask']['recipient']=='Arun'
    assert result['ask']['item']=='charger' and result['ask']['requires_new_request'] is True


@pytest.mark.parametrize('text',[
 'Tell Ravi: buy the charger from Arun and bring it to me.',
 'Tell Ravi: "Buy the charger from Arun."',
 'Buy the charger from Arun and open the door.',
])
def test_collection_question_never_invents_an_action_from_payload_or_unparsed_tail(names,text):
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert (result['ask'] or {}).get('event')!='confirm_collect'


@pytest.mark.parametrize('query',['the doctor','the Daqu','the dok'])
def test_home_suggestion_handles_articles_in_actual_rewrites(names,query):
    text='No job, go back to '+query+'.'
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['steps']==[]
    assert result['ask']['event']=='unknown_name' and 'home' in result['ask']['options']


@pytest.mark.parametrize('identifier,event',[
 ('STARTER-ta-01',None),('STARTER-ta-02','confirm_collect'),
 ('STARTER-ta-05','unknown_name'),('STARTER-ta-07','confirm_collect'),
])
def test_saved_reviewed_tamil_draws_end_with_usable_question_or_confirmation(identifier,event,measurement_record):
    import yaml
    roster=yaml.safe_load(Path('data/task4_names_example.yaml').read_text())
    roster['robot_capability']=yaml.safe_load(Path('tests/fixtures/robot_capability.yaml').read_text())
    saved=next(json.loads(l) for l in measurement_record('records/task4/B_glossary_off/requests.jsonl').read_text().splitlines() if json.loads(l)['id']==identifier)
    pairs=[p for p in saved['trace'] if 'index' in p]
    backend=MockOfficeBackend([p['rewrite_raw'] for p in pairs],[p.get('raw_plan',{}) for p in pairs])
    result=understand(saved['text'],roster,'Bharathwaj','ta',{'backend':backend})
    if event is None:assert result['decision']=='act'
    else:
        assert result['decision']=='ask' and result['ask']['event']==event
        if identifier.endswith('-05'):assert 'home' in result['ask']['options']
