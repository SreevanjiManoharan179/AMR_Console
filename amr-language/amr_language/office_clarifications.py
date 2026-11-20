"""Grounded slot questions and explicit collection proposals; never authorize acts."""
import re
from .office_schema import walk_steps


def collection_proposal(english,roster):
    # Interpret a possible collect action solely to ask for a new request. Never
    # turn a buy rewrite into an executable plan, and never inspect message text.
    from .office_grammar import _clauses,_simple,_mask_quotes
    for clause in _clauses(english):
        text=re.sub(r'^(?:(?:could|would|can) you\s+(?:please\s+)?|please\s+)','',clause,flags=re.I)
        if re.match(r'^(?:tell|inform|notify|ask|check|find out|see)\b',text,re.I):continue
        if not re.match(r'^buy\b',text,re.I):
            match=re.search(r'[,.;!?]\s*(?=buy\b)',_mask_quotes(text),re.I)
            if not match or _simple(text[:match.start()]) is None:continue
            text=text[match.end():]
        candidate=_simple(re.sub(r'^buy\b','collect',text,count=1,flags=re.I))
        if not candidate or candidate['action']!='retrieve':continue
        matches=roster.match(candidate['from'],'from','retrieve')
        if len(matches)!=1 or matches[0] not in roster.names.groups['people']:continue
        return {'event':'confirm_collect','slot':'action','options':['collect','cancel'],
                'item':candidate['item'],'recipient':matches[0],
                'interpretation':'collect_instead_of_buy','requires_new_request':True}
    return None


def first_difference(plans,roster):
    flattened=[list(walk_steps(p['steps'])) for p in plans]
    if len({len(p) for p in flattened})!=1:return {'event':'clarify_action','slot':'steps','options':[]}
    for index in range(len(flattened[0])):
        steps=[p[index] for p in flattened]
        for slot in ('action','to','from','at','item','if_not_there','question','message'):
            values=list(dict.fromkeys(s.get(slot) for s in steps))
            if len(values)<2:continue
            extra={'step_n':steps[0]['n'],'values':values}
            if slot in ('to','from','at') and all(isinstance(v,str) and roster.known(v) for v in values):
                return {'event':'clarify_person' if all(roster.person(v) for v in values) else 'clarify_place',
                        'slot':slot,'options':values[:3],**extra}
            if slot=='item':return {'event':'clarify_item','slot':slot,'options':values[:3],**extra}
            if slot=='if_not_there':
                target=steps[0].get('from') if steps[0]['action']=='retrieve' else steps[0].get('to') or steps[0].get('at')
                if target and roster.known(target):
                    return {'event':'clarify_absence','slot':slot,'options':values[:3],
                            'recipient':target,'policy':values[0],**extra}
            if slot in ('message','question'):return {'event':'clarify_'+slot,'slot':slot,'options':values[:3],**extra}
            return {'event':'clarify_action','slot':slot,'options':values[:3],**extra}
    return {'event':'clarify_action','slot':'branches','options':[]}
