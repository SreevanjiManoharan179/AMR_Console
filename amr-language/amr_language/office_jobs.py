"""Whole-request grouping: never split a check's branches or hide an omission."""
import re
from .office_grammar import _clauses,parse_english,COMMAND
from .office_capabilities import scope_reason
from .office_schema import walk_steps,normalized_plan


def grammar_job(english,requester,maximum=6):
    # Extend segmentation for unsupported independent tasks, keeping quoted text opaque.
    from .office_grammar import _mask_quotes
    masked=_mask_quotes(english)
    boundaries=list(re.finditer(r'\s+(?:and then|then|after that)\s+(?=(?:make|brew|cook|clean|wash|vacuum|search|open|close|unlock|climb)\b)',masked,re.I))
    spans=[];start=0
    for m in boundaries:spans.extend(_clauses(english[start:m.start()]));start=m.end()
    spans.extend(_clauses(english[start:]))
    groups=[]
    for clause in spans:
        if re.match(r'^(?:if\b|otherwise\b|yes\b|no\b)',clause,re.I) and groups:
            groups[-1]+='; '+clause
        else:groups.append(clause)
    parsed=[];omissions=[]
    for group in groups:
        reason=scope_reason(group)
        if reason:
            # Physical operations may be prerequisites; never drop and continue.
            if reason!='unsupported_job':return {'issue':reason,'chunks':[],'omissions':[]}
            omissions.append({'text':group,'reason':reason});continue
        raw=parse_english(group,requester)
        if raw is None:return None
        if raw['issue']:return {'issue':raw['issue'],'chunks':[],'omissions':[]}
        parsed.append((group,raw['steps']))
    if not parsed:return {'issue':'unsupported_job','chunks':[],'omissions':omissions}
    chunks=[];steps=[];texts=[]
    for text,part in parsed:
        count=len(list(walk_steps(part)))
        if count>maximum:return {'issue':'too_many_steps','chunks':[],'omissions':omissions}
        # Travelling to a place immediately before asking there is redundant.
        if steps and steps[-1]['action']=='go' and part[0]['action'] in ('check','tell','deliver') and steps[-1]['to']==(part[0].get('at') or part[0].get('to')):
            steps.pop()
        if len(list(walk_steps(steps)))+count>maximum:
            chunks.append({'english':'; '.join(texts),'plan':normalized_plan({'steps':steps,'issue':None},requester)})
            steps=[];texts=[]
        steps.extend(part);texts.append(text)
    if steps:chunks.append({'english':'; '.join(texts),'plan':normalized_plan({'steps':steps,'issue':None},requester)})
    return {'issue':None,'chunks':chunks,'omissions':omissions}


def agreement_job(job):
    # Rewrites are evidence, not meaning: alternate equivalent English may agree.
    return {'chunks':[chunk['plan'] for chunk in job['chunks']], 'omissions':job['omissions'],'issue':job['issue']}
