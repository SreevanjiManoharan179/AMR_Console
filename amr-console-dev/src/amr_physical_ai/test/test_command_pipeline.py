"""Fail-closed gates for typed commands to named places: pure tests, no ROS import/init or external API request."""
import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml

from amr_physical_ai.command_pipeline import (
    ClarificationRequired,CommandParser,Grid,Locations,validate_parsed)

CONFIG=Path(__file__).resolve().parent/'data/stations/named_locations.yaml'   # test-only stations map


@pytest.fixture
def locations():
    return Locations(CONFIG)


def test_five_demo_commands_dispatch_only_three(locations,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    parser=CommandParser(locations,'auto')
    assert parser.mode=='fallback' and 'FALLBACK' in parser.label
    # Unit-level approval is independent of the human-maintained source YAML.
    locations.approved=True
    commands=yaml.safe_load(CONFIG.with_name('demo_commands.yaml').read_text())['commands']
    dispatched=[];clarifications=[]
    for entry in commands:
        parsed=parser.parse(entry['text'])
        try:
            loc,result=validate_parsed(parsed,locations,entry['text'],locations.static_grid)
            dispatched.append(loc.name)
            assert result['free'] and entry['expected']=='SUCCEEDED'
        except ClarificationRequired:
            clarifications.append(entry['text'])
            assert entry['expected']=='CLARIFICATION'
    assert dispatched==['north_station','east_station','home']
    assert len(clarifications)==2


@pytest.mark.parametrize('text',[
    'Go to north or east.', 'Go home then the kitchen.', 'Do not go home.',
    "Don't go north.", 'Go north if the door is open.', 'Where is home?',
    'Go to the kitchen.', 'Go to a station.', 'Go to northeast_station.',
    'Go north and stop.', 'Go home before south.', 'Tell me how to go north.',
    'Take me to the kitchen near north.', 'Go to the moon next to home.',
    'Go north except if something is wrong.'
])
def test_ambiguous_unknown_negated_or_multistep_blocked(locations,text):
    result=CommandParser(locations,'fallback').parse(text)
    with pytest.raises(ClarificationRequired):
        validate_parsed(result,locations,text,locations.static_grid,require_approval=False)


def test_coordinate_approval_is_required(tmp_path):
    config=yaml.safe_load(CONFIG.read_text())
    config['approval_status']='proposed'
    config['map_yaml']=str((CONFIG.parent/config['map_yaml']).resolve())
    unapproved=tmp_path/'named_locations.yaml'
    unapproved.write_text(yaml.safe_dump(config))
    locations=Locations(unapproved)
    assert not locations.approved
    parsed={'location':'home','confidence':1,'clarification_needed':False}
    with pytest.raises(ClarificationRequired,match='confirmation'):
        validate_parsed(parsed,locations,'Go home',locations.static_grid)


@pytest.mark.parametrize('parsed',[
    {'location':'kitchen','confidence':1,'clarification_needed':False},
    {'location':'home','confidence':.2,'clarification_needed':False},
    {'location':'home','confidence':True,'clarification_needed':False},
    {'location':'home','confidence':math.nan,'clarification_needed':False},
    {'location':'home','confidence':1.5,'clarification_needed':False},
    {'location':'home','confidence':1,'clarification_needed':'false'},
    {'location':'home','confidence':1,'clarification_needed':False,'x':999},
    {'location':'home','confidence':1}, [],
])
def test_bad_model_output_never_validates(locations,parsed):
    with pytest.raises(ClarificationRequired):
        validate_parsed(parsed,locations,'Go home',locations.static_grid,require_approval=False)


def test_model_disagreement_with_alias_blocked(locations):
    parsed={'location':'north_station','confidence':1,'clarification_needed':False}
    with pytest.raises(ClarificationRequired,match='does not match'):
        validate_parsed(parsed,locations,'Go east',locations.static_grid,require_approval=False)


def test_map_unavailable_or_wrong_map_blocked(locations):
    parsed={'location':'home','confidence':1,'clarification_needed':False}
    for grid in (None,Grid(np.zeros((10,10)),.05,(0,0,0))):
        with pytest.raises(ClarificationRequired):
            validate_parsed(parsed,locations,'Go home',grid,require_approval=False)


def test_grid_bounds_unknown_occupied_and_clearance():
    data=np.zeros((30,30),dtype=int);data[10,10]=-1;data[15,15]=100
    grid=Grid(data,.05,(0,0,0))
    for x,y in ((-.0001,.5),(1.5,.5),(.525,.525),(.775,.775),(.01,.01)):
        with pytest.raises(ClarificationRequired):grid.validate_point(x,y,.28)
    rotated=Grid(np.zeros((30,30)),.05,(2,3,math.pi/2))
    assert rotated.validate_point(1.25,3.75,.28)['free']


def test_mock_gpt41_response_schema_and_no_real_request(locations,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder')
    def transport(payload):
        assert payload['model']=='gpt-4.1'
        assert payload['text']['format']['strict'] is True
        schema=payload['text']['format']['schema']
        assert schema['additionalProperties'] is False
        assert 'north_station' in schema['properties']['location']['enum']
        return {'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'location':'north_station',
                'confidence':.95,'clarification_needed':False})}]}]}
    parser=CommandParser(locations,'llm',transport=transport)
    parsed=parser.parse('Go north')
    loc,check=validate_parsed(parsed,locations,'Go north',locations.static_grid,require_approval=False)
    assert loc.name=='north_station' and check['valid']


@pytest.mark.parametrize('response',[
    {'status':'incomplete'}, {'status':'completed','output':[]},
    {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'No'}]}]},
    {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'bad json'}]}]},
    [],
])
def test_model_incomplete_refusal_or_malformed_response_blocked(locations,monkeypatch,response):
    monkeypatch.setenv('OPENAI_API_KEY','unit-test-placeholder')
    parser=CommandParser(locations,'llm',transport=lambda _:response)
    with pytest.raises(ClarificationRequired):parser.parse('Go home')


@pytest.mark.parametrize('count,accepted',[(3,False),(4,True),(9,True),(20,True),(21,False)])
def test_place_count_limits(tmp_path,count,accepted):
    # Since 18 Oct 2026: 4 to 20 places per map (was 4 to 6); proposed_system names nine.
    config=yaml.safe_load(CONFIG.read_text())
    config['map_yaml']=str((CONFIG.parent/config['map_yaml']).resolve())
    home=config['locations'][0]
    config['locations']=[dict(home,name=f'place_{k}',aliases=[]) for k in range(count)]
    places=tmp_path/'named_locations.yaml'
    places.write_text(yaml.safe_dump(config))
    if accepted:
        assert len(Locations(places).by_name)==count
    else:
        with pytest.raises(ValueError,match='4–20'):Locations(places)


def test_the_parser_stays_rule_based_by_default_even_with_an_api_key(locations,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','sk-test')
    def no_network(_):
        raise AssertionError('a default parser must not call the network')
    parser=CommandParser(locations,transport=no_network)
    assert parser.mode=='fallback' and 'FALLBACK' in parser.label
    parser.parse('go to the lobby')
