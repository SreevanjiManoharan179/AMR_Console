"""Strict plan schema v3, including one-level branches and six-action limit."""
from copy import deepcopy
import json
from pathlib import Path
import re

from .schema import valid_string

ACTIONS=('deliver','retrieve','check','tell','go')
# Steps allowed inside "if yes / if no" and "if not there" (0.6.0); one level deep only.
BRANCH_ACTIONS=('tell','deliver','go','check','retrieve')
MAXIMUM_ITEMS=4
ISSUES=(None,'out_of_scope','unsupported_condition','negation','too_many_steps')
SCHEMA_V3=json.loads(Path(__file__).with_name('plan_v3.schema.json').read_text())
FIELDS={
    'deliver':{'n','action','item','to','if_not_there','item_hint','otherwise'},
    'retrieve':{'n','action','item','from','to','if_not_there','item_hint','item_location','otherwise'},
    'check':{'n','action','at','question','on_answer','if_not_there','otherwise'},
    'tell':{'n','action','to','message','if_not_there','otherwise'},
    'go':{'n','action','to','if_not_there'},
}


class TooManySteps(ValueError):
    pass


def split_items(value):
    """'file, the charger and the notebook' -> ['file','charger','notebook'] (one stack on the tray)."""
    parts=re.split(r'\s*,\s*(?:and\s+)?|\s+and\s+',(value or '').strip())
    return [re.sub(r'^(?:this|that|the|a|an|some)\s+','',part.strip(),flags=re.I) for part in parts if part.strip()]


def walk_steps(steps):
    for step in steps:
        yield step
        if step.get('on_answer'):
            for answer in ('yes','no'):
                yield from walk_steps(step['on_answer'][answer])
        if step.get('otherwise'):
            yield from walk_steps(step['otherwise'])


def validate_plan(value):
    if not isinstance(value,dict) or set(value)!={'steps','issue'} or value['issue'] not in ISSUES:
        raise ValueError('Invalid v3 plan root')
    if not isinstance(value['steps'],list):
        raise ValueError('Invalid steps')
    if value['issue'] is not None:
        if value['steps']:
            raise ValueError('Issue must not include an executable prefix')
        return value
    if not value['steps']:
        raise ValueError('Empty plan')
    total=0
    def visit(steps,branch=False):
        nonlocal total
        for step in steps:
            total+=1
            if total>6:
                raise TooManySteps('At most six total actions')
            if not isinstance(step,dict) or step.get('action') not in ACTIONS:
                raise ValueError('Invalid action')
            action=step['action']
            if branch and action not in BRANCH_ACTIONS:
                raise ValueError('Unsupported branch action')
            required=FIELDS[action]-{'n','if_not_there','on_answer','item_hint','item_location','otherwise'}
            if action=='retrieve': required-= {'to'}
            if not required<=set(step) or set(step)-FIELDS[action]:
                raise ValueError('Unexpected or missing step fields')
            if 'n' in step and (type(step['n']) is not int or not 1<=step['n']<=6):
                raise ValueError('Invalid step number')
            for key in ('item','to','from','at','question','message','item_hint'):
                if key in step:
                    if action=='retrieve' and key=='to' and step[key] is None:
                        continue
                    if not valid_string(step[key],600 if key in ('question','message') else 200):
                        raise ValueError('Invalid step text')
            if step.get('item_location') not in (None,'known','guess'):raise ValueError('Invalid item location certainty')
            policy=step.get('if_not_there','bring_back')
            if not isinstance(policy,str):
                raise ValueError('Invalid absence policy')
            other=step.get('otherwise')
            if (policy=='otherwise')!=(other is not None):
                raise ValueError('"otherwise" steps and the otherwise rule go together')
            if other is not None:
                if branch or not isinstance(other,list) or not other:
                    raise ValueError('Nested or empty otherwise steps')
                visit(other,True)
            if policy not in ('bring_back','ask_anyone','skip','otherwise'):
                if policy.startswith('leave_with:'):
                    if action=='retrieve':
                        raise ValueError('Cannot leave an item that has not been retrieved')
                    if not valid_string(policy.split(':',1)[1],100):
                        raise ValueError('Invalid alternate recipient')
                elif re.fullmatch(r'try_later:[1-9][0-9]{0,3}',policy):
                    if int(policy.split(':')[1])>1440:
                        raise ValueError('Retry delay too large')
                else:
                    raise ValueError('Unsupported absence condition')
            answer=step.get('on_answer')
            if answer is not None:
                if branch or action!='check' or not isinstance(answer,dict) or set(answer)!={'yes','no'}:
                    raise ValueError('Nested or unsupported condition')
                for key in ('yes','no'):
                    if not isinstance(answer[key],list):
                        raise ValueError('Invalid answer branch')
                    visit(answer[key],True)
    visit(value['steps'])
    return value


def normalized_plan(value,requester):
    validate_plan(value)
    result=deepcopy(value)
    for index,step in enumerate(walk_steps(result['steps']),1):
        step['n']=index
        step.setdefault('if_not_there','bring_back')
        if step['action']=='retrieve' and not step.get('to'):
            step['to']=requester
        if step['action']=='check':
            step.setdefault('on_answer',None)
    return result
