"""Deterministic English office plans. No model, map lookup or guessed names.

None means the complete request could not be parsed; never return a prefix.
Question/message text is copied from the input, including clocks and Unicode.
"""
import re
import unicodedata

from .office_schema import normalized_plan,TooManySteps,split_items,BRANCH_ACTIONS

COMMAND=r'(?:give|deliver|take|bring|carry|send|hand|pass|drop|leave|retrieve|get|fetch|collect|pick up|grab|tell|inform|notify|check|ask|find out|see|go|visit|head|move|return|come|swing|call)\b'
MARKER=r'(?:if\b|otherwise\b|yes\b|no\b)'


def _strip(value):
    return value.strip(' \t\r\n,;:.!?')


def _payload(value):
    value=_strip(value)
    if len(value)>1 and (value[0],value[-1]) in (('"','"'),("'","'"),('“','”'),('‘','’')):
        return value[1:-1]
    return value


def _item(value):
    return re.sub(r'^(?:this|that|the|a|an)\s+','',_payload(value),flags=re.I)


def _mask_quotes(text):
    out=list(text);quote=None
    for i,char in enumerate(text):
        apostrophe=char in ("'",'’') and i>0 and i+1<len(text) and text[i-1].isalnum() and text[i+1].isalnum()
        if apostrophe:continue
        if quote:
            out[i]=' '
            if char==quote:quote=None
        elif char in ('"',"'",'“','‘'):
            quote={'“':'”','‘':'’'}.get(char,char);out[i]=' '
    return ''.join(out)


def _clauses(text):
    masked=_mask_quotes(text)
    prefix=rf'(?:{COMMAND}|{MARKER})'
    # ", and <action>" and a closing "and come back" start a new step too (0.5.1), so a long
    # request never folds its last action into a question or message.
    boundary=(rf';\s*|(?:[.!?]\s*|,\s*)(?=(?:(?:and then|then|after that|and)\s+)?{prefix})'
              rf'|\s+(?:and then|then|after that)\s+(?={prefix})|,\s*(?={COMMAND})'
              rf'|\s+and\s+(?=(?:come|go)\s+back\b)')
    spans=[];start=0
    for match in re.finditer(boundary,masked,re.I):
        piece=_strip(text[start:match.start()])
        if re.match(r'\s*(?:,|then\b)',match.group(),re.I) and re.match(r'^(?:if\b|yes\b|no\b|otherwise\b)',piece,re.I) and ',' not in piece:
            continue
        if piece:spans.append(piece)
        start=match.end()
    piece=_strip(text[start:])
    if piece:spans.append(piece)
    return [re.sub(r'^(?:and then|then|after that|and)\s+','',s,flags=re.I) for s in spans]


# A noun phrase is opaque content, but may not hide another clause or relation.
# Unknown simple nouns/names remain literal for grounding. Structural words need
# an explicit grammar production; they are never swallowed into an entity slot.
RESERVED=set("or also until because although but without and then after before while when if unless otherwise that which who is are was were be been being has have had do does did not here there to from with for me myself it them he she they you we my your our their its his her only again later tomorrow today tonight now quickly please buy collect bring take get give leave send email".split())


def _noun(value):
    value=_payload(value)
    words=re.sub(r'^(?:the|a|an|this|that)\s+','',value,flags=re.I).split()
    return bool(words) and len(words)<=8 and all(re.fullmatch(r"[\w]+(?:[-'][\w]+)*",w) and w.casefold() not in RESERVED for w in words)


def _step(action,**slots):
    if slots.get('item'):
        hint=re.search(r'\s+(?:I left\s+(?:on|at)|near)\s+.+$',slots['item'],re.I)
        if hint:
            slots['item_hint']=slots['item'][hint.start():].strip()
            slots['item']=slots['item'][:hint.start()]
    for key in ('to','from','at'):
        if slots.get(key) is not None and not (key=='to' and slots[key]=='me') and not _noun(slots[key]):return None
    if slots.get('item') is not None:
        # One item, or a short stack: "the file, the charger and the notebook".
        parts=split_items(slots['item'])
        if not parts or not all(_noun(part) for part in parts):return None
    return {'action':action,'if_not_there':'bring_back',**slots}


def _retrieve(item,tail):
    destination=None
    delivered=re.search(r'\s+and\s+(?:bring|take|carry|give|return)\s+(?:it|them|the item)\s+(here|(?:back\s+)?to\s+.+)$',tail,re.I)
    for_me=re.search(r'\s+for\s+me$',tail,re.I)
    direct=re.search(r'\s+to\s+me$',tail,re.I)
    if delivered:
        source=tail[:delivered.start()];goal=delivered[1]
        destination=None if re.fullmatch(r'here|(?:back\s+)?to\s+me',goal,re.I) else re.sub(r'^(?:back\s+)?to\s+','',goal,flags=re.I)
    elif for_me:source=tail[:for_me.start()]
    elif direct:source=tail[:direct.start()]
    else:source=tail
    return _step('retrieve',item=_item(item),**{'from':_payload(source),'to':destination})


def _simple(text):
    text=_strip(text)
    text=re.sub(r'^(?:(?:could|would|can) you\s+(?:please\s+)?|please\s+|I want you to\s+)','',text,flags=re.I)
    # Closing words that add nothing: "... to Ravi instead", "... please", "... thank you".
    for _ in range(2):text=re.sub(r',?\s+(?:please|instead|thanks|thank you)$','',text,flags=re.I)
    guess=re.fullmatch(r'I think I left\s+(.+?)\s+at\s+(.+)',text,re.I)
    if guess:
        step=_retrieve(*guess.groups())
        if step:step['item_location']='guess'
        return step
    # Calling a person is a message: "Call Ravi", "Call Ravi to the conference hall".
    match=re.fullmatch(r'call\s+(.+?)(?:\s+(?:here|over|to\s+me|to\s+my\s+(?:desk|place|cabin|room|seat)))?',text,re.I)
    if match and _noun(match[1]):return _step('tell',to=_payload(match[1]),message='Please come')
    match=re.fullmatch(r'call\s+(.+?)\s+to\s+(.+)',text,re.I)
    if match and _noun(match[1]) and _noun(match[2]):return _step('tell',to=_payload(match[1]),message='Please come to '+_payload(match[2]))
    # "Ask Ravi to come here", "Tell Meera to bring the report": the instruction is the message.
    match=re.fullmatch(r'(?:tell|ask|inform|notify)\s+(\S+(?:\s+\S+)?)\s+to\s+(.+)',text,re.I)
    if match and _noun(match[1]) and _payload(match[2]):return _step('tell',to=_payload(match[1]),message=_payload(match[2]))
    question=re.fullmatch(r'ask\s+(.+?)\s+((?:whether|where|what time)\b.+)',text,re.I)
    if question:return _step('check',at=_payload(question[1]),question=_payload(question[2]),on_answer=None)
    # Asking at a destination already includes travel; do not add a separate go.
    match=re.fullmatch(r'(?:go to|head (?:over )?to|swing by|visit)\s+(.+?)\s+and\s+(?:ask|check|find out)\s+(?:(?:her|him|them)\s+)?(.+)',text,re.I)
    if match:return _step('check',at=_payload(match[1]),question=_payload(match[2]),on_answer=None)
    # Relative possession and explicit source/return productions consume the whole tail.
    match=re.fullmatch(r'(?:retrieve|get|fetch|collect|pick up|grab|bring|take)\s+(?:me\s+)?(.+?)\s+that\s+is\s+with\s+(.+)',text,re.I)
    if match:return _retrieve(*match.groups())
    match=re.fullmatch(r'(?:retrieve|get|fetch|collect|pick up|grab|bring|take)\s+(?:me\s+)?(.+?)\s+from\s+(.+)',text,re.I)
    if match:return _retrieve(*match.groups())
    # Payload delimiters are explicit; clocks are opaque message text.
    match=re.fullmatch(r'(?:tell|inform|notify)\s+(.+?)(?:\s*:\s*|\s+that\s+|,\s*)(.+)',text,re.I)
    if not match:
        match=re.fullmatch(r'(?:tell|inform|notify)\s+(\S+)\s+((?:the|a|an|I|we|you|he|she|they|it|there|meeting|room|visitors|ready)\b.+)',text,re.I)
    if match and _payload(match[2]):return _step('tell',to=_payload(match[1]),message=_payload(match[2]))
    match=re.fullmatch(r'(?:carry|take|send|get)\s+(?:this |the |a )?message\s+to\s+(.+?)\s*:\s*(.+)',text,re.I)
    if match:return _step('tell',to=_payload(match[1]),message=_payload(match[2]))
    match=re.fullmatch(r'(?:check|ask|find out|see)\s+(?:at|in)\s+(.+?)(?:\s*:\s*|\s+((?:whether|if)\s+.*))(.+)?',text,re.I)
    if match:
        question=match[2] or match[3]
        if question:return _step('check',at=_payload(match[1]),question=_payload(question),on_answer=None)
    match=re.fullmatch(r'(?:check|ask|find out|see)\s+((?:if|whether)\s+.+)',text,re.I)
    if match:
        question=match[1]
        predicate=re.sub(r'^(?:if|whether)\s+','',question,flags=re.I)
        # "the room in X is free" / "a room is free in X" / "X is free".
        place=re.search(r'\b(?:in|at)\s+(.+?)(?=\s+(?:is|are|was|were|has|have)\b|$)',predicate,re.I)
        if place:target=place[1]
        else:
            subject=re.match(r'(.+?)\s+(?:is|are|was|were|has|have)\b',predicate,re.I)
            if not subject:return None
            target=subject[1]
            if re.fullmatch(r'(?:the |a )?(?:room|it|anyone|someone|visitors?)',target,re.I):return None
        return _step('check',at=_payload(target),question=question,on_answer=None)
    match=re.fullmatch(r'(?:ask|check)\s+(?:at\s+)?(.+?)\s*:\s*(.+)',text,re.I)
    if match:return _step('check',at=_payload(match[1]),question=_payload(match[2]),on_answer=None)
    # Explicit main actions only: no implicit final dock return.
    match=re.fullmatch(r'(?:go(?: back| home)? to|head(?: over)? to|move to|return to|come back to|swing by|visit)\s+(.+)',text,re.I)
    if match:return _step('go',to=_payload(match[1]))
    if re.fullmatch(r'(?:go|return|come back|go back)\s+home',text,re.I):return _step('go',to='home')
    # A bare "come back" means back to the person who asked, as "come back to me" does.
    if re.fullmatch(r'(?:come back|come back here|return here)',text,re.I):return _step('go',to='me')
    match=re.fullmatch(r'(?:give|deliver|take|bring|carry|send|hand|pass|drop|drop off|leave)\s+(.+?)\s+(?:off\s+)?(?:to|with|at)\s+(.+)',text,re.I)
    if match:return _step('deliver',item=_item(match[1]),to=_payload(match[2]))
    match=re.fullmatch(r'(?:give|hand|pass|bring)\s+(\S+)\s+((?:this|that|the|a|an)\s+.+)',text,re.I)
    if match:return _step('deliver',to=_payload(match[1]),item=_item(match[2]))
    return None


SEPARATOR=r'\s*,\s*(?:(?:and then|then|after that|and)\s+)?|\s+(?:and then|then|after that|and)\s+'
POLITE=r'(?:(?:could|would|can) you\s+(?:please\s+)?|please\s+|I want you to\s+)?'


def _target(step):
    return step.get('from') if step['action']=='retrieve' else step.get('to') or step.get('at')


def _parse_clause(text,previous=None,previous_text=None):
    """One clause as one or more steps, or None.

    "go to A, then B and C" (the verb carries over), "give X to A and Y to B", "go to Divya and
    tell her ..." (her = Divya), "give the file to Divya and tell Ravi ...". The whole clause is
    tried as one step first, so a message that contains "and" stays one message.
    """
    text=_strip(text)
    if not text:return None
    if previous is not None:
        target=_target(previous)
        if target and target!='me':
            text=re.sub(r'^((?:tell|ask|inform|notify|give|hand|call)\s+)(?:her|him|them)\b',lambda m:m[1]+target,text,flags=re.I)
        if not re.match(POLITE+COMMAND,text,re.I):
            verb=re.match(POLITE+'('+COMMAND+')',previous_text or '',re.I)
            if previous['action']=='go':text='go to '+re.sub(r'^to\s+','',text,flags=re.I)
            elif previous['action'] in ('deliver','retrieve') and verb:text=verb[1]+' '+text
            else:return None
    step=_simple(text)
    if step:return [step]
    masked=_mask_quotes(text)
    for match in re.finditer(SEPARATOR,masked,re.I):
        head=_simple(text[:match.start()])
        if head is None:continue
        rest=_parse_clause(text[match.end():],head,text[:match.start()])
        if rest is None:continue
        # Travelling to someone just before speaking to them is one stop, not two.
        if head['action']=='go' and rest[0]['action'] in ('tell','check','deliver') and str(_target(rest[0])).casefold()==head['to'].casefold():
            return rest
        return [head]+rest
    return None


ABSENT=r'(?:(.+?)\s+)?(?:(?:is not|isn\x27t|are not|aren\x27t)\s+(?:here|there|present|available|around)|(?:is|are)\s+(?:absent|away|unavailable)|not\s+(?:here|there|available)|absent|away)'


def _absence_subject(text,step):
    """True when the clause is about the person of this step being away."""
    text=text.replace('’',"'")
    if re.match(r'^otherwise\b',text,re.I):return True
    conditional=re.match(r'^if\s+(.+?(?:here|there|present|available|around|absent|away|unavailable))(?:\s*[,;:]\s*|\s+)',text,re.I)
    absent=re.fullmatch(ABSENT,conditional[1],re.I) if conditional else None
    if not absent:return False
    who=absent[1]
    return not who or who.casefold() in ('he','she','they','it','the person') or who.casefold()==str(_target(step)).casefold()


def _absence_action(text):
    """The action part of "if she is not there, <action>" / "otherwise <action>", or None."""
    text=text.replace('’',"'")
    conditional=re.match(r'^if\s+(.+?(?:here|there|present|available|around|absent|away|unavailable))(?:\s*[,;:]\s*|\s+)(?:then\s+)?(.+)$',text,re.I)
    if conditional:return _strip(conditional[2])
    if re.match(r'^otherwise\b',text,re.I):return _strip(re.sub(r'^otherwise\s*[,;:]?\s*','',text,flags=re.I))
    return None


def _absence(text,step):
    # Normalize only structural text, preserving message/question payloads verbatim.
    text=text.replace('’',"'")
    text=re.sub(r"\b(he|she|it)'s\b",r'\1 is',text,flags=re.I)
    text=re.sub(r"\b(they|we|you)'re\b",r'\1 are',text,flags=re.I)
    conditional=re.match(r'^if\s+(.+?)(?:\s*[,;:]\s*|\s+(?=(?:then|please|leave|give|bring|return|try|come|ask|skip)\b))(.+)$',text,re.I)
    if conditional:
        subject=conditional[1];action=conditional[2]
        absent=re.fullmatch(r'(?:(.+?)\s+)?(?:(?:is not|isn\x27t|are not|aren\x27t)\s+(?:here|there|present|available|around)|(?:is|are)\s+(?:absent|away|unavailable)|not\s+(?:here|there|available)|absent|away)',subject,re.I)
        if not absent:return None
        who=absent[1]
        if who and who.casefold() not in ('he','she','they','it','the person'):
            recipient=step.get('from') if step['action']=='retrieve' else step.get('to') or step.get('at')
            if who.casefold()!=str(recipient).casefold():return None
    elif re.match(r'^otherwise\b',text,re.I):action=re.sub(r'^otherwise\s*[,;:]?\s*','',text,flags=re.I)
    else:return None
    # "If she is not there, then give it to Priya": the "then" adds nothing.
    action=re.sub(r'^(?:then|please)\s+','',_strip(action),flags=re.I)
    match=re.fullmatch(r'(?:leave|give|hand)\s+(?:it|the item|the file|the message)\s+(?:with|to)\s+(.+)',action,re.I)
    if match:return 'leave_with:'+_payload(match[1]) if _noun(match[1]) else None
    if re.fullmatch(r'(?:bring|take|return)\s+(?:it|the item|the file|the message)\s+back(?: to me)?',action,re.I):return 'bring_back'
    match=re.fullmatch(r'(?:try(?: again| later)?|come back)\s+(?:in\s+)?([1-9][0-9]{0,3})\s+minutes?',action,re.I)
    if match:return 'try_later:'+match[1]
    if re.fullmatch(r'ask\s+anyone(?:\s+there)?',action,re.I):return 'ask_anyone'
    if re.fullmatch(r'skip(?:\s+(?:it|this step|this delivery|the step))?',action,re.I):return 'skip'
    return None


def parse_english(text,requester='requester'):
    """Return the whole v3 plan or None; never ground names or invent payloads."""
    if not isinstance(text,str) or not text.strip() or len(text)>3200:return None
    text=unicodedata.normalize('NFC',text.strip())
    text=re.sub(r'^(?:there is )?no(?: more)? (?:work|jobs?)\s*[,.;]\s*','',text,flags=re.I)
    if re.match(r'^(?:please\s+)?(?:do not|don\x27t|never)\b',text,re.I):return {'steps':[],'issue':'negation'}
    if re.match(r'^(?:please\s+)?(?:make|brew|cook|buy|open|close|turn on|turn off)\b',text,re.I):return {'steps':[],'issue':'out_of_scope'}
    clauses=_clauses(text)
    # An inline absence condition belongs to its action, not to its target text.
    expanded=[]
    for clause in clauses:
        if not re.match(r'^(?:check|ask|find out|see|tell|inform|notify)\b',clause,re.I):
            match=re.search(r'\s+(?=if\b|otherwise\b)',_mask_quotes(clause),re.I)
            if match:
                expanded.extend([clause[:match.start()],clause[match.end():]]);continue
        expanded.append(clause)
    steps=[];current_check=None;previous_text=None
    for clause in expanded:
        branch=re.match(r'^(?:(?:if\s+)?(yes|no|so|not|it is not|it isn\x27t|it is))\s*(?:[,;:]\s*|\s+)(?:then\s+)?(.+)$',clause,re.I)
        otherwise=re.match(r'^otherwise\s*[,;:]?\s*(.+)$',clause,re.I)
        if branch or (otherwise and current_check is not None and current_check.get('on_answer')):
            if current_check is None:return None
            label=branch[1].casefold() if branch else 'no';body=branch[2] if branch else otherwise[1]
            side='no' if label in ('no','not','it is not',"it isn't") else 'yes'
            children=_parse_clause(body,current_check)
            if children is None:return None
            if any(child['action'] not in BRANCH_ACTIONS or child.get('on_answer') for child in children):return {'steps':[],'issue':'unsupported_condition'}
            if current_check.get('on_answer') is None:current_check['on_answer']={'yes':[],'no':[]}
            current_check['on_answer'][side].extend(children);continue
        if re.match(r'^(?:if|otherwise)\b',clause,re.I):
            if not steps:return None
            policy=_absence(clause,steps[-1])
            if policy is None:
                # Any other instruction for "not there" becomes the step's own "otherwise" steps (0.6.0).
                action=_absence_action(clause) if _absence_subject(clause,steps[-1]) else None
                children=_parse_clause(action) if action else None
                if not children or steps[-1]['action']=='go' or any(child['action'] not in BRANCH_ACTIONS or child.get('on_answer') for child in children):
                    return {'steps':[],'issue':'unsupported_condition'}
                steps[-1]['if_not_there']='otherwise';steps[-1]['otherwise']=children;continue
            steps[-1]['if_not_there']=policy;continue
        parsed=_parse_clause(clause,steps[-1] if steps else None,previous_text)
        if parsed is None:return None
        steps.extend(parsed);previous_text=clause;current_check=parsed[-1] if parsed[-1]['action']=='check' else None
    if not steps:return None
    try:return normalized_plan({'steps':steps,'issue':None},requester)
    except TooManySteps:return {'steps':[],'issue':'too_many_steps'}
    except (ValueError,TypeError):return None
