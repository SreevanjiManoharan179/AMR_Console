"""Owner-supplied tray capabilities; no robot/map writes or inferred capacities."""
import math
import re
from .grounding import normalize
from .office_schema import walk_steps,split_items,MAXIMUM_ITEMS

ROUTE_REQUIREMENTS=('requires_arm','requires_door_operation','requires_lift','requires_stairs')
SCOPE_ACTIONS=r'(?:make|brew|cook|clean|wash|vacuum|search|open|close|operate|climb|press|unlock|lift)'


def scope_reason(text):
    text=text.strip().strip(' .!?')
    text=re.sub(r'^(?:(?:could|would|can) you\s+(?:please\s+)?|please\s+)','',text,flags=re.I)
    if re.match(r'^(?:tell|inform|notify|ask|check|find out|see)\b',text,re.I):return None
    if re.search(r'\b(?:use|take|climb)\s+(?:the\s+)?stairs\b',text,re.I):return 'requires_stairs'
    if re.search(r'\b(?:use|take|operate)\s+(?:the\s+)?(?:lift|elevator)\b',text,re.I):return 'requires_lift'
    if re.match(r'^(?:open|close|unlock)\b',text,re.I) and re.search(r'\bdoor\b',text,re.I):return 'requires_door_operation'
    if re.match(r'^'+SCOPE_ACTIONS+r'\b',text,re.I):return 'unsupported_job'
    return None


def validate_capability(value):
    if value is None:return None
    if not isinstance(value,dict):raise ValueError('Capability profile must be a dictionary')
    if value.get('approval_status')!='confirmed':return None
    dims=value.get('tray_inner_cm',{})
    if not isinstance(dims,dict) or not isinstance(value.get('item_classes',{}),dict):raise ValueError('Invalid capability structure')
    numbers=[dims.get(k) for k in ('length','width','height')]+[value.get('maximum_payload_kg'),value.get('maximum_wait_at_stop_s')]
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in numbers):raise ValueError('Owner must configure positive tray, payload and wait values')
    maximum=value.get('maximum_steps_per_job')
    if type(maximum) is not int or not 1<=maximum<=6:raise ValueError('Maximum steps must be 1 through 6')
    if value.get('reachable_areas')!='from_map_facts':raise ValueError('Reachability must come from caller map facts')
    for row in value.get('item_classes',{}).values():
        if not isinstance(row,dict) or row.get('rule') not in ('ok','ask_at_pickup','not_allowed'):raise ValueError('Invalid item class rule')
        if any(flag in row and type(row[flag]) is not bool for flag in ('fragile','requires_bag','by_weight_class')):raise ValueError('Invalid capability class flag')
        if not isinstance(row.get('terms'),list) or not all(isinstance(s,str) and 0<len(s)<=100 for s in row['terms']):raise ValueError('Invalid item class terms')
    return value


def item_assessment(item,roster,capability):
    parts=split_items(item)
    if len(parts)>1:
        # A stack: every item must be allowed; any doubt is asked once at pickup.
        results=[item_assessment(part,roster,capability) for part in parts]
        refused=next((r for r in results if r['rule']=='not_allowed'),None)
        if refused:return refused
        return {'rule':'ask_at_pickup' if any(r['rule']=='ask_at_pickup' for r in results) else 'ok',
                'reason':next((r['reason'] for r in results if r.get('reason')),None),'requires_bag':any(r.get('requires_bag') for r in results)}
    key=normalize(item)
    for prefix in ('the ','a ','an ','this ','some '):
        if key.startswith(prefix):key=key[len(prefix):]
    matching=[row for ident,row in roster.names.entries.items() if key in {normalize(ident),*(normalize(a) for a in row.get('aliases',[])),*(normalize(a) for group in row.get('aliases_by_language',{}).values() for a in group)}]
    for row in matching:
        if row.get('weight_class')=='heavy':return {'rule':'not_allowed','reason':'too_heavy'}
        if row.get('fits_tray') is False:return {'rule':'not_allowed','reason':'does_not_fit'}
    if capability is None:return {'rule':'not_allowed','reason':'capability_unset'}
    class_rows=[row for row in capability.get('item_classes',{}).values() if key in {normalize(t) for t in row['terms']}]
    classes=[row['rule'] for row in class_rows]
    bag=any(row.get('requires_bag') for row in class_rows)
    if 'not_allowed' in classes:return {'rule':'not_allowed','reason':'item_not_allowed'}
    if any(row.get('fragile') is True for row in matching+class_rows):return {'rule':'ask_at_pickup','reason':'fragile','requires_bag':bag}
    if matching and any(row.get('weight_class') in ('light','medium') and row.get('fits_tray') is True for row in matching):
        return {'rule':'ask_at_pickup' if any(r['rule']=='ask_at_pickup' and not r.get('by_weight_class') for r in class_rows) else 'ok','reason':None,'requires_bag':bag}
    return {'rule':'ok' if classes and all(v=='ok' for v in classes) else 'ask_at_pickup','reason':None,'requires_bag':bag}


def feasibility(steps,roster,capability,person_locations=None):
    person_locations=person_locations or {}
    if capability is None:return {'reason':'capability_unset'}
    if len(list(walk_steps(steps)))>capability['maximum_steps_per_job']:return {'reason':'step_limit'}
    checks=[]
    limit=capability.get('maximum_items_on_tray',MAXIMUM_ITEMS)
    for step in walk_steps(steps):
        if step.get('item') and len(split_items(step['item']))>limit:return {'reason':'too_many_items','step_n':step['n'],'item':step['item']}
        targets=[step[slot] for slot in ('to','from','at') if slot in step]
        if step.get('if_not_there','').startswith('leave_with:'):targets.append(step['if_not_there'].split(':',1)[1])
        for target in targets:
            person=getattr(roster,'temporary',{}).get(target) or roster.names.groups['people'].get(target,{})
            place=person_locations.get(target,person.get('usual_place',roster.names.raw.get('requester_place',target) if target==roster.requester_id else target))
            row=roster.names.entries.get(place,{})
            rows=[row,roster.names.entries.get(row.get('room'),{})]
            for field in ROUTE_REQUIREMENTS:
                if any(r.get(field) is True for r in rows):return {'reason':field,'step_n':step['n'],'item':step.get('item')}
        if step['action'] in ('deliver','retrieve'):
            result=item_assessment(step['item'],roster,capability)
            if result['rule']=='not_allowed':return {**result,'step_n':step['n'],'item':step['item']}
            key=normalize(step['item'])
            restricted=any((r.get('personal') or r.get('restricted')) and key in {normalize(k),*(normalize(a) for a in r.get('aliases',[]))} for k,r in roster.names.entries.items())
            if restricted and (step.get('if_not_there')=='ask_anyone' or step.get('if_not_there','').startswith('leave_with:') or step['action']=='deliver' and not roster.person(step['to'])):
                return {'reason':'helper_restricted','item':step['item'],'step_n':step['n']}
            if result['rule']=='ask_at_pickup':checks.append({'step_n':step['n'],'item':step['item'],'at':'pickup','fragile':result.get('reason')=='fragile','requires_bag':result.get('requires_bag',False)})
    return {'pickup_checks':checks}
