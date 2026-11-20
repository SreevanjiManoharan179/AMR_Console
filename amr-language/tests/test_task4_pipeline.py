"""Adaptive grammar-first intent extraction keeps confirmation and disagreement guards."""
from amr_language import understand
from amr_language.office_backend import MockOfficeBackend

FORM={'steps':[{'action':'deliver','item':'file','to':'Divya'}],'issue':None}


def test_two_agreeing_grammar_led_draws_skip_model_planner(names):
    text='Give the file to Divya.';backend=MockOfficeBackend([text]*4,[{}]*4);trace=[]
    result=understand(text,names,'Bharathwaj','en',{'backend':backend,'trace':trace})
    assert result['decision']=='act' and result['draws']==2 and result['planner']=='grammar'
    assert sum(c[0]=='rewrite' for c in backend.calls)==2
    assert not any(c[0]=='plan' for c in backend.calls)
    assert trace[-1]['distinct_pairs']==1 and trace[-1]['plans_reused']==1
    assert 'Shall I go ahead?' in result['readback']


def test_model_fallback_is_marked_and_requires_four_draws(names):
    text='Transport the file to Divya.';backend=MockOfficeBackend([text]*4,[FORM]*4)
    result=understand(text,names,'Bharathwaj','en',{'backend':backend})
    assert result['decision']=='act' and result['planner']=='model' and result['draws']==4
    assert sum(c[0]=='plan' for c in backend.calls)==1


def test_first_two_disagree_all_four_must_agree_not_majority(names):
    backend=MockOfficeBackend(['Give the file to open office.','Give the file to team office.','Give the file to open office.','Give the file to open office.'],[{}]*4)
    result=understand('Give the file to office.',names,'Bharathwaj','en',{'backend':backend})
    assert result['draws']==4 and result['decision']=='ask'
    assert result['ask']['options']==['open_office','team_office']


def test_branch_grammar_reaches_act_without_relaxing_schema_or_source_checks(names):
    text='Check at the conference hall: Is the room free? If yes, tell Ravi: the room is free. If no, deliver the file to Priya.'
    backend=MockOfficeBackend([text]*4,[{}]*4)
    result=understand(text,names,'Bharathwaj','en',{'backend':backend})
    assert result['decision']=='act' and result['planner']=='grammar'
    assert result['steps'][0]['on_answer']['yes'][0]['to']=='Ravi'
    assert result['steps'][0]['on_answer']['no'][0]['to']=='Priya'
    assert len(result['steps'])==1 and not any(c[0]=='plan' for c in backend.calls)


def test_every_branch_target_is_map_checked_before_act(names):
    next(p for p in names['people'] if p['name']=='Ravi')['usual_place']=None
    text='Check at the conference hall: Is the room free? If yes, tell Ravi: the room is free.'
    result=understand(text,names,'Bharathwaj','en',{'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='ask' and result['ask']['event']=='ask_where_person'
    assert result['ask']['target']=='Ravi'
