"""Office v3: provisional four-pair agreement, grounding and fixed readback."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import re
import time
import unicodedata

from .backends import BackendError,BackendTimeout
from .core import _MODEL_LOCK,source_span
from .grounding import normalize
from .office_backend import LocalOfficeBackend,make_backend,settings_for_office
from .office_names import LANGUAGES,OfficeNames
from .office_schema import TooManySteps,normalized_plan,validate_plan,walk_steps,split_items
from .office_speech import say
from .schema import valid_string

AGREEMENT_NOTICE='Provisional adaptive agreement: two agreeing grammar-led draws are weaker than four; otherwise use four. Readback and yes remain mandatory. Agreement is uncalibrated.'


# Tamil typed in English letters ("file-a Divya kitta kudunga"): words that do not occur in English requests.
ROMAN_TAMIL_STRONG=r"kitta|kitte|kudunga|kodunga|kelunga|sollunga|eduthuttu|eduthutu|irunthu|irundhu|vaanga|vaangi|ponga|kondu"
ROMAN_TAMIL_WEAK=r"kudu|kodu|kelu|sollu|vaa|po|illa|enna|inga|anga|konjam|seekiram|intha|antha|oru|nu|aa|ah"


def romanised_tamil(text):
    if not text.isascii():return False
    words=re.findall(r"[a-z]+",text.casefold())
    strong=sum(1 for w in words if re.fullmatch(ROMAN_TAMIL_STRONG,w))
    weak=sum(1 for w in words if re.fullmatch(ROMAN_TAMIL_WEAK,w))+len(re.findall(r"[a-z]-(?:ku|kku|la|a|ah|ai)\b",text.casefold()))
    return strong>=1 and strong+weak>=2 or strong>=2


def detect_language(text):
    counts=Counter()
    for char in text:
        for start,end,language in ((0x0b80,0x0bff,'ta'),(0x0d00,0x0d7f,'ml'),(0x0c00,0x0c7f,'te')):
            if start<=ord(char)<=end: counts[language]+=1
    if counts:return counts.most_common(1)[0][0]
    return 'ta' if romanised_tamil(text) else 'en'


def substitute_aliases(text,roster):
    """Write approved native-script names and places in English letters before the model translates.

    "மீராவிடம்" becomes "Meera-விடம்": small models otherwise respell or translate names (Meera as
    "Mira", Divya as "the deity", reception as "the welcome"). Only aliases that belong to one entry.
    """
    owners={}
    for aliases in roster.names.aliases.values():
        for alias,ids in aliases.items():owners.setdefault(alias,set()).update(ids)
    pairs=[]
    for entries in roster.names.groups.values():
        for key,row in entries.items():
            for values in row.get('aliases_by_language',{}).values():
                for alias in values:
                    if not alias.isascii() and len(alias)>=3 and owners.get(normalize(alias))=={key}:pairs.append((alias,key))
    for alias,key in sorted(set(pairs),key=lambda pair:(-len(pair[0]),pair[0])):
        shown=key.replace('_',' ')
        text=re.sub(re.escape(alias)+r'(?=[^\W\d_]|[\u0b80-\u0bff\u0c00-\u0c7f\u0d00-\u0d7f])',shown+'-',text)
        text=text.replace(alias,shown)
    return text


def ask_whether(english):
    """"Ask Arun if the encoder is ready" is a question, not a condition: say "whether"."""
    return re.sub(r"\b(ask\s+(?:at\s+)?(?:the\s+)?[\w'-]+(?:\s[\w'-]+)?)\s+if\s+(?!(?:s?he|they|it)\s+(?:is|are)\s+not\b|not\b)",
                  lambda m:m.group(1)+' whether ',english,flags=re.I)


def validate_english(value):
    if (not isinstance(value,str) or not 1<=len(value)<=3200 or not value.strip()
            or any(unicodedata.category(c)=='Cs' or (unicodedata.category(c)=='Cc' and c not in '\n\r\t') for c in value)
            or value.lstrip().startswith(('{','[','```'))):
        raise ValueError('Rewrite must be plain English, not JSON')
    if re.search('[\u0b80-\u0bff\u0c00-\u0c7f\u0d00-\u0d7f]',value):
        raise ValueError('Rewrite must retain names in English letters')
    return unicodedata.normalize('NFC',value.strip())


def _slot_sources(plan,english,requester):
    for step in walk_steps(plan['steps']):
        for slot in ('item','to','from','at','question','message','item_hint'):
            value=step.get(slot)
            if value is None: continue
            # "come back" names nobody: the grammar writes "me", grounded later to the requester.
            if slot=='to' and (value==requester or value in ('me','myself','requester')):
                if step['action']=='retrieve' or re.search(r'\b(?:me|myself|requester)\b',english,re.I):
                    continue
                if step['action']=='go' and re.search(r'\b(?:come|go|return)\s+back\b',english,re.I):
                    continue
            # "Call Ravi to the conference hall" carries the fixed message "Please come to <place>".
            if slot=='message' and re.search(r'\bcall\b',english,re.I):
                called=re.fullmatch(r'Please come(?: to (.+))?',value)
                if called and (called[1] is None or source_span(called[1],english)):continue
            if slot=='item' and all(source_span(part,english) for part in split_items(value)):continue
            if not source_span(value,english): raise ValueError('Plan slot absent from English rewrite')
        policy=step.get('if_not_there','bring_back')
        if policy.startswith('leave_with:') and not source_span(policy.split(':',1)[1],english):
            raise ValueError('Alternate recipient absent from rewrite')
        if policy.startswith('try_later:') and not source_span(policy.split(':')[1],english):
            raise ValueError('Retry delay absent from rewrite')


def _condition_issue(plan,english):
    text=english
    questions=[]
    for step in walk_steps(plan['steps']):
        for key in ('question','message'):
            if step.get(key):
                text=text.replace(step[key],'')
                if key=='question': questions.append(step[key].casefold())
    text=text.casefold().replace('’',"'")
    text=text.replace("isn't","is not").replace("aren't","are not")
    if re.search(r"\b(?:do not|don't|never)\s+(?:give|deliver|retrieve|bring|get|take|check|ask|tell|go)\b",text):
        return 'negation'
    if re.search(r'\b(?:unless|provided that|as long as|depending on)\b',text):
        return 'unsupported_condition'
    branches=any(step.get('on_answer') for step in walk_steps(plan['steps']))
    for match in re.finditer(r'\bif\b([^.;\n]*)',text):
        clause=match.group(1).strip(' ,:;"')
        if not clause: continue # An embedded check question was removed above.
        if re.search(r'\b(?:not (?:here|there|present|available)|absent|away|unavailable)\b',clause):
            continue
        if branches and re.match(r"(?:(?:the )?answer (?:is )?)?(?:yes|no|so|not|it is not|it isn't|it is)"
                                 r"(?:\s*[,;:]|\s+(?:then\s+)?(?:tell|give|deliver|carry|go|ask|check|call|collect|get|bring|take)\b|$)",clause):
            continue
        if branches:
            head=re.split(r',|\b(?:then|tell|deliver|give|go|retrieve|bring|ask)\b',clause,maxsplit=1)[0]
            words=set(re.findall(r'[a-z0-9]+',head))-{'it','is','the','a','an','that','this','there'}
            if words and any(words<=set(re.findall(r'[a-z0-9]+',question)) for question in questions):
                continue
        return 'unsupported_condition'
    return None


def _literal_choices(plan,english,roster):
    text=english
    steps=list(walk_steps(plan['steps']))
    for step in steps:
        for key in ('question','message'):
            if step.get(key): text=text.replace(step[key],'')
    if not re.search(r'\bor\b',text,re.I): return []
    if any(step.get('on_answer') or step['if_not_there']!='bring_back' for step in steps): return []
    query=normalize(text)
    aliases={alias for group in roster.names.aliases.values() for alias in group if alias.isascii()}
    variants='|'.join(re.escape(alias) for alias in sorted(aliases,key=lambda item:(-len(item),item)))
    match=re.search(r'(?<!\w)('+variants+r')\s+or\s+('+variants+r')(?!\w)',query)
    if match:
        action=steps[0]['action']
        options=sorted(set(roster.match(match[1],action=action))|set(roster.match(match[2],action=action)))
        return [{'event':'ask_which','slot':'to','options':options}]
    return [{'event':'clarify_request','slot':'plan','options':[]}]


def _ground(plan,roster):
    result=deepcopy(plan)
    uncertain=[]
    for step in walk_steps(result['steps']):
        for slot in ('to','from','at'):
            if slot not in step: continue
            said=step[slot]
            matches=roster.match(said,slot,step['action'])
            if len(matches)==1:
                step[slot]=matches[0]
            else:
                uncertain.append({'event':'ask_which' if matches else 'unknown_name','slot':slot,
                                  'options':list(matches) if matches else roster.suggestions(said,action=step['action']),'query':said,'action':step['action'],'step_n':step['n']})
        policy=step['if_not_there']
        if policy.startswith('leave_with:'):
            said=policy.split(':',1)[1]
            matches=tuple(key for key in roster.match(said) if roster.person(key))
            if len(matches)==1: step['if_not_there']='leave_with:'+matches[0]
            else: uncertain.append({'event':'ask_which' if matches else 'unknown_name','slot':'if_not_there',
                                    'options':list(matches) if matches else roster.suggestions(said,person_only=True)})
        if step.get('item'):
            item=step['item'].strip()
            step['item']=re.sub(r'^(?:a|an|the|this)\s+','',item,flags=re.I)
    return result,uncertain


NUMBER_WORDS=['one','two','three','four','five','six','seven','eight','nine','ten','eleven','twelve']


def _agreement_key(plan):
    def canonical(value):
        if isinstance(value,str):
            value=' '.join(unicodedata.normalize('NFC',value).casefold().split()).rstrip(' .?!')
            # "three o'clock" and "3 o'clock" are the same message.
            return re.sub(r'\b('+'|'.join(NUMBER_WORDS)+r')\b',lambda m:str(NUMBER_WORDS.index(m.group(1))+1),value)
        if isinstance(value,list): return [canonical(v) for v in value]
        if isinstance(value,dict): return {key:canonical(v) for key,v in value.items()}
        return value
    return json.dumps(canonical(plan),sort_keys=True,ensure_ascii=False)


def understand(text,names,requester,language=None,settings=None):
    from .office_pipeline import run
    return run(text,names,requester,language,settings)
