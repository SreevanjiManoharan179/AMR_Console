"""Existing on_answer semantics include direct yes/no referring expressions."""
import pytest
from amr_language.office import _condition_issue,_slot_sources
from amr_language.office_schema import normalized_plan


@pytest.mark.parametrize('yes,no',[('If yes','If no'),('If so','If not'),('If it is','If it is not')])
def test_existing_yes_no_branch_references_are_allowed(yes,no):
    english=f'Check at reception: Is it free? {yes}, tell Ravi: It is free. {no}, give the file to Priya.'
    raw={'steps':[{'action':'check','at':'reception','question':'Is it free?','on_answer':{
        'yes':[{'action':'tell','to':'Ravi','message':'It is free.'}],
        'no':[{'action':'deliver','item':'file','to':'Priya'}]}}],'issue':None}
    plan=normalized_plan(raw,'Bharathwaj')
    _slot_sources(plan,english,'Bharathwaj')
    assert _condition_issue(plan,english) is None


def test_unrelated_condition_is_not_accepted_as_no_branch():
    raw={'steps':[{'action':'check','at':'reception','question':'Is it free?','on_answer':{
        'yes':[{'action':'tell','to':'Ravi','message':'It is free.'}],'no':[]}}],'issue':None}
    plan=normalized_plan(raw,'Bharathwaj')
    assert _condition_issue(plan,'Check at reception: Is it free? If no rain, tell Ravi: It is free.')=='unsupported_condition'


def test_absence_contraction_is_existing_if_not_there_semantics():
    text="Give this file to Divya. If she isn't there, give it to Priya."
    plan=normalized_plan({'steps':[{'action':'deliver','item':'file','to':'Divya','if_not_there':'leave_with:Priya'}],'issue':None},'Bharathwaj')
    _slot_sources(plan,text,'Bharathwaj')
    assert _condition_issue(plan,text) is None
