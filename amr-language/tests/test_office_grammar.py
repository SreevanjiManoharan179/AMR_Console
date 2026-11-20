"""English office grammar contract; held-out file is not used to tune these tests."""
import csv
import hashlib
import json
from pathlib import Path

import pytest
from amr_language.office_grammar import parse_english
from amr_language.office_schema import validate_plan,walk_steps
from amr_language.office import _slot_sources


@pytest.mark.parametrize('text,action',[('Give the file to Divya.','deliver'),
 ('Get the charger from Arun and bring it to me.','retrieve'),
 ('Ask at reception: Did any visitors come today?','check'),
 ('Tell Ravi: the meeting is at 3.','tell'),('Go home to the dock.','go')])
def test_actions_preserve_source_and_schema(text,action):
    plan=parse_english(text,'Bharathwaj')
    assert plan is not None
    assert plan['steps'][0]['action']==action
    validate_plan(plan);_slot_sources(plan,text,'Bharathwaj')


def test_nested_yes_no_actions_are_not_unconditional():
    text='Check at the conference hall: Is the room free? If yes, tell Ravi: the room is free. If no, deliver the file to Priya.'
    plan=parse_english(text,'Bharathwaj')
    assert len(plan['steps'])==1
    branch=plan['steps'][0]['on_answer']
    assert branch['yes'][0]['action']=='tell' and branch['no'][0]['action']=='deliver'
    assert [s['n'] for s in walk_steps(plan['steps'])]==[1,2,3]
    _slot_sources(plan,text,'Bharathwaj')


@pytest.mark.parametrize('suffix,policy',[("if she isn't there, leave it with Priya",'leave_with:Priya'),
 ('if she is absent, try again in 15 minutes','try_later:15'),
 ('otherwise bring it back','bring_back'),('if she is not there, ask anyone there','ask_anyone'),
 ('if she is away, skip it','skip')])
def test_absence_policies(suffix,policy):
    plan=parse_english('Give the file to Divya; '+suffix+'.','Bharathwaj')
    assert plan is not None and plan['issue'] is None
    assert plan['steps'][0]['if_not_there']==policy


@pytest.mark.parametrize('clock',['at 3','at 3 pm','மூணு மணிக்கு','at three oclock','for 3 hours'])
def test_clock_and_duration_words_remain_message_text(clock):
    text='Tell Ravi: the meeting is '+clock+'.'
    plan=parse_english(text,'Bharathwaj')
    assert plan['steps'][0]['message']=='the meeting is '+clock
    assert len(plan['steps'])==1 and plan['steps'][0]['action']=='tell'


def test_no_payload_or_unknown_destination_is_guessed():
    assert parse_english('Tell Ravi.','Bharathwaj') is None
    result=parse_english('Give the parcel to Aruni.','Bharathwaj')
    assert result['steps'][0]['to']=='Aruni'
    assert parse_english('Give the file to Divya; dance on the desk.','Bharathwaj') is None
    assert parse_english('Buy the charger for Arun.','Bharathwaj')['issue']=='out_of_scope'


def test_multi_steps_explicit_return_and_limit():
    p=parse_english('Give the file to Priya; then get the charger from Arun; after that tell Ravi: I am ready.','Bharathwaj')
    assert [s['action'] for s in p['steps']]==['deliver','retrieve','tell']
    p=parse_english('Give the file to Divya then come back to me.','Bharathwaj')
    assert p['steps'][1]['to']=='me'
    assert len(parse_english('Give the file to Divya.','Bharathwaj')['steps'])==1
    text='; '.join(['Go to reception']*7)
    assert parse_english(text,'Bharathwaj')['issue']=='too_many_steps'


def test_all_authored_starter_english_and_saved_rewrites_are_safe_to_parse(measurement_record):
    texts=[]
    with Path('data/owner_check.csv').open(newline='') as stream:
        rows=[r for r in csv.DictReader(stream) if r['speaker'].startswith('STARTER')]
    assert len(rows)==70
    texts.extend(r['expected_english'] for r in rows)
    for contestant in ('A','B'):
        for line in measurement_record(f'records/task3/final_{contestant}/requests.jsonl').read_text().splitlines():
            texts.extend(p.get('rewrite_raw','') for p in json.loads(line)['trace'] if 'index' in p)
    assert len(texts)==630
    for text in texts:
        result=parse_english(text,'Bharathwaj')
        if result is not None:validate_plan(result)


def test_heldout_bytes_remain_frozen(measurement_record):
    manifest=json.loads(measurement_record('records/task4/grammar_holdout_freeze.json').read_text())
    assert hashlib.sha256(Path(manifest['file']).read_bytes()).hexdigest()==manifest['sha256']
