"""Text-only office conversation rehearsal; no ROS, navigation or real timers."""
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import shlex
from copy import deepcopy
from zoneinfo import ZoneInfo

from .calibration import ROOT
from .office import AGREEMENT_NOTICE,detect_language,understand
from .office_names import LANGUAGES,OfficeNames
from .office_replies import _hear_reply,short_reply
from .office_speech import say
from .office_map import MapFacts,MapRequests
from .office_schema import walk_steps
from .office_capabilities import feasibility


class RehearsalStopped(Exception):
    pass


from .office_carrier import CarrierFlows

class Rehearsal(CarrierFlows):
    drives_robot=False

    def __init__(self,names,settings=None,input_fn=input,output_fn=print,log_path=None,source='UNREVIEWED_TYPED'):
        self.names=names
        self.settings=dict(settings or {})
        if self.settings.get('review_only'):raise ValueError('Review-only plans cannot be rehearsed as jobs')
        self.settings.setdefault('input_source','SCRIPTED' if source in ('SCRIPTED','STARTER_SCRIPTED') else 'UNREVIEWED_TYPED')
        self.input=input_fn
        self.output=output_fn
        self.records=[]
        self.queue=deque()
        self.current=None
        self.roster=None
        self.language='en'
        self.outcomes=[]
        self.payloads=[]
        self.source=source
        self.last_arrival=None
        self.transient_places={}
        self.person_places={}
        self.position=None
        self.failed_items=set()
        self.loaded=set();self.declined=set()
        self.base_names=deepcopy(names)
        self.log_path=Path(log_path).resolve() if log_path else None
        allowed=(ROOT/'records/rehearsals').resolve()
        if self.log_path is not None and not self.log_path.is_relative_to(allowed):
            raise ValueError('Rehearsal log must be under records/rehearsals')

    def emit(self,kind,**data):
        row={'time':datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),'kind':kind,**data}
        if self.current:row['requester_id']=OfficeNames(self.names,self.current['requester']).requester_id
        self.records.append(row)
        if self.log_path and (self.source in ('SCRIPTED','STARTER_SCRIPTED') or kind in ('input','robot','confirmation','outcome')):
            self.log_path.parent.mkdir(parents=True,exist_ok=True)
            with self.log_path.open('a') as stream: stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        if kind=='robot': self.output(f"[robot {data['language']}] {data['text']}")
        elif kind=='screen': self.output('[screen] '+data['text'])
        elif kind=='move': self.output(f"[robot moves to {data['place']}]")

    # Robot movement hooks. A text rehearsal only records each move. A robot
    # caller overrides move_to() to drive to an approved place key and returns
    # True on arrival or False when the stop cannot be reached; raising
    # RehearsalStopped aborts the job. The conversation never moves the robot
    # by itself: every move goes through arrive().
    def move_to(self,place,purpose,target=None):
        self.emit('move',place=place,purpose=purpose,target=target,simulated=True)
        return True

    def place_for(self,target):
        """Resolve a person, the requester or a destination to an approved place key, or None."""
        if target is None:return None
        override=self.person_places.get(target) or self.transient_places.get(target)
        if override:return override
        if self.roster and target==self.roster.requester_id:return self.names.get('requester_place')
        facts=MapFacts(self.roster)
        if self.roster.person(target) or target in getattr(self.roster,'temporary',{}):return facts.usual(target)
        return target if target in facts.places else None

    def arrive(self,target,purpose='step'):
        place=self.place_for(target)
        if place is not None and place==self.position:return True
        if not self.move_to(place,purpose,target):
            # A failed move can stop anywhere on the way: the position is unknown.
            self.position=None
            return False
        if place is not None:self.position=place
        return True

    def go_home(self):
        # Idle robots wait at the dock. A map without a 'home' place keeps the robot where it is.
        if self.roster is not None and 'home' in MapFacts(self.roster).places:
            if not self.arrive('home','home'):self.speak('job_failed',self.language,reason='unreachable')

    def _unreachable(self,step,target):
        self.speak('job_failed',self.language,reason='unreachable')
        outcome={'status':'failed','target':target,'item':step.get('item'),'reason':'unreachable'}
        if step.get('action')=='deliver' and step.get('item') and target!=self.roster.requester_id:
            # The item is still on board: bring it back to the requester.
            self.speak('return_to_requester',self.language,recipient=self.roster.requester_id,item=step['item'])
            outcome['returned_to_requester']=self.arrive(self.roster.requester_id,'return')
        return outcome

    def speak(self,event,language=None,**slots):
        lang=language or self.language
        line=say(event,lang,names=self.names,requester=self.current['requester'],**slots)
        self.emit('robot',event=event,language=lang,text=line)
        if event.startswith('arrive_') or event=='ask_people_where': self.last_arrival=(event,dict(slots))
        return line

    def read(self,prompt,language=None):
        while True:
            raw=self.input(prompt)
            self.emit('input',text=raw,language=language or self.language)
            if raw.startswith('/queue '):
                try:
                    parts=shlex.split(raw[len('/queue '):])
                    if len(parts)<3 or parts[1] not in LANGUAGES: raise ValueError()
                    requester,lang=parts[:2]
                    text=' '.join(parts[2:])
                    OfficeNames(self.names,requester)
                    self.queue.append({'requester':requester,'language':lang,'text':text})
                    self.speak('queued',lang,current=self.roster.requester_id,position=len(self.queue)+1,minutes=4)
                    self.emit('queue',requester=requester,language=lang,position=len(self.queue)+1,
                              estimate_minutes=4,estimate_note='fixed rehearsal illustration, not a measured waiting-time estimate')
                except (ValueError,TypeError):
                    self.emit('screen',text='Use /queue <requester> <en|ta|ml|te> <typed request>.')
                continue
            if raw.startswith('/correction '):
                from .office_learning import validate_candidate
                try:
                    candidate=json.loads(raw[len('/correction '):]);validate_candidate('office_meaning',candidate)
                    self.emit('screen',text='CORRECTION: '+json.dumps(candidate,ensure_ascii=False))
                    self.speak('verify_answer',language or self.language)
                    answer=self.reply({'yes','no'},language or self.language)
                    if answer['kind']=='yes':self.suggest('office_meaning',candidate,typed_text=raw,decisions={'correction_confirmed':True})
                except (ValueError,TypeError):self.emit('screen',text='Use /correction with language, word and meaning JSON.')
                continue
            if raw.startswith('/helper_name '):
                self.current['helper_name']=raw[len('/helper_name '):].strip()[:100]
                continue
            if raw.startswith('/person '):
                target=raw[len('/person '):].strip()
                if target in self.roster.names.groups['people']:
                    self.current['interlocutor']=target
                    self.emit('screen',text='Interlocutor selected: '+target)
                    if self.last_arrival:
                        event,values=self.last_arrival
                        self.speak(event,self.roster.language_for(target,self.language),**values)
                else: self.emit('screen',text='Choose an approved person ID.')
                continue
            return raw

    def reply(self,expect,language=None,recipient=None,item=None):
        lang=language or self.language
        for attempt in range(3):
            raw=self.read('reply> ',lang)
            lang=self.roster.language_for(self.current.get('interlocutor'),lang)
            result=short_reply(raw,expect,lang)
            if result is None:result={'kind':'unclear','answer_english':None}
            other=bool(self.settings.get('english_only')) and bool(raw.strip()) and detect_language(raw)!='en'
            if result['kind']=='unclear' and raw.strip() and not other and self.settings.get('reply_model'):
                kind=self._model_reply(raw,expect)
                if kind:result={'kind':kind,'answer_english':None,'source':'model'}
            self.emit('heard',language=lang,expect=sorted(expect),result=result)
            if other and result['kind']!='stop':
                # Another language: say so, and ask again. It counts as one of the three tries.
                if attempt<2:self.speak('english_only',lang)
                continue
            if result['kind']=='stop':
                self.speak('stopped',lang)
                raise RehearsalStopped()
            if result['kind']!='unclear': return result
            if attempt<2:
                if recipient:
                    # With the search switched on, a silence is answered by calling the person by name.
                    if self.settings.get('search_for_person') and self.roster.person(recipient):self.speak('call_person',lang,recipient=recipient)
                    else:self.speak('reminder',lang,recipient=recipient,item=item)
                    limit=self.capability()['maximum_wait_at_stop_s']
                    elapsed=min(15*(attempt+1),limit)
                    self.emit('timer',simulated_seconds=elapsed,note='illustrated reminder boundary; console owns actual deadline')
                    # A robot that will go and look for the person always calls three times first.
                    if elapsed>=limit and not self._searching(recipient) and not self.settings.get('conversational'):return {'kind':'unclear','answer_english':None}
                else: self.speak('say_slowly' if attempt==1 and self.settings.get('conversational') else 'reply_prompt',lang)
        return {'kind':'unclear','answer_english':None}

    def _model_reply(self,raw,expect):
        """Ask the language model what a free reply means (setting reply_model). Never guesses a yes
        out of words that say no; any fault means "unclear" and the robot simply asks again."""
        import re
        from .office_backend import make_backend,settings_for_office
        from .core import _MODEL_LOCK
        allowed=set(expect)&{'yes','no','taken','done','not_here','later'}
        if not allowed or len(raw)>300:return None
        try:
            config=settings_for_office({k:v for k,v in self.settings.items() if k!='trace'})
            backend=make_backend(config)
            if not hasattr(backend,'classify') or not _MODEL_LOCK.acquire(timeout=5):return None
            try:kind=backend.classify(raw.strip(),allowed,min(8.,config['timeout_s']),config)
            finally:_MODEL_LOCK.release()
        except Exception:
            return None
        if kind not in allowed:return None
        if kind in ('yes','taken','done') and re.search(r"\b(?:no|not|don'?t|do not|wait|stop|cancel|never|wrong)\b",raw,re.I):return None
        self.emit('reply_read_by_model',text=raw,reading=kind)
        return kind

    def _language_at(self,target):
        actor=self.current.get('interlocutor')
        return self.roster.language_for(target,self.roster.language_for(actor,self.language))

    def _nearby(self,person,limit=2):
        """The rooms nearest to a person's usual place, nearest first (not their own room, not the dock)."""
        import math
        facts=MapFacts(self.roster);origin=facts.usual(person)
        if origin not in facts.places:return []
        point=facts._xy(origin);own=facts.places[origin].get('room',origin)
        if point is None:return []
        found=[]
        for key in self.roster.names.groups['rooms']:
            if key in (own,origin,'home') or facts.check(key) or facts._xy(key) is None:continue
            if feasibility([{'n':1,'action':'go','to':key}],self.roster,self.capability()).get('reason'):continue
            found.append((math.dist(point,facts._xy(key)),key))
        return [key for _,key in sorted(found)[:limit]]

    def _search(self,step,person,retry,ask_here=True):
        """Look for a person who is away from their place (0.7.0).

        Ask whoever is at their place (unless nobody answered there), then go to the two nearest rooms,
        then the common area (names['common_area'], or the open office), asking at each stop whether the
        person is there or where they are. One named place is followed once. Found: the step carries on
        there. Not found: None, and the step's own "if not there" rule applies.
        """
        facts=MapFacts(self.roster);visited={self.position};done=('delivered','retrieved','checked','told','went')
        self.emit('search',person=person,stage='started',place=self.position)
        def attempt(place):
            self.person_places[person]=place
            outcome=retry(person) if retry else self._deliver(step,person,fallback=True)
            if outcome.get('status') not in done:
                self.person_places.pop(person,None);return None
            outcome['at']=place;outcome['found_by_search']=True
            self.emit('search',person=person,stage='found',place=place)
            if place!=facts.usual(person):self.suggest('person_place',{'person':person,'place':place},decisions={'found_by_search':True},outcome=outcome)
            return outcome
        def ask(place,event):
            lang=self.roster.language_for(self.current.get('interlocutor'),self.language)
            self.speak(event,lang,recipient=person)
            try:raw=self.read('where is the person?> ',lang)
            except EOFError:return None
            heard=_hear_reply(raw,{'location','yes','no','not_here','later'},lang,self.settings)
            self.emit('heard_location',language=lang,result=heard,exact_words=raw,place=place)
            if heard['kind']=='stop':
                self.speak('stopped',lang);raise RehearsalStopped()
            if heard['kind']=='yes':return place
            if heard['kind']=='answer':
                places=facts.location_hint(heard.get('answer_original') or heard.get('answer_english') or '',person)
                if len(places)==1 and not facts.check(places[0]) and not feasibility([{'n':step['n'],'action':'go','to':places[0]}],self.roster,self.capability()).get('reason'):
                    return places[0]
            return None
        def follow(place):
            if place is None or place in visited:return None
            visited.add(place)
            return attempt(place) if self.arrive(place,'search') else None
        hint=ask(self.position,'ask_people_where') if ask_here else None
        self.speak('searching',self.language,recipient=person)
        outcome=follow(hint)
        if outcome:return outcome
        common=self.names.get('common_area') or 'open_office'
        stops=self._nearby(person)+([common] if common in facts.places and not facts.check(common) else [])
        for place in dict.fromkeys(stops):
            if place in visited or not self.arrive(place,'search'):continue
            visited.add(place)
            self.current.pop('interlocutor',None)
            told=ask(place,'search_ask')
            outcome=attempt(place) if told==place else follow(told)
            if outcome:return outcome
        self.emit('search',person=person,stage='not_found',visited=sorted(p for p in visited if p))
        self.speak('search_failed',self.language,recipient=person)
        return None

    def _absence_policy(self,step,target,retry=None,seek_hint=True,nobody=False):
        if seek_hint and self.roster.person(target):
            if self.settings.get('search_for_person') and self.settings.get('follow_person_hint','ask')!='never':
                followed=self._search(step,target,retry,ask_here=not nobody)
            else:followed=self._person_hint(step,target,retry)
            if followed is not None:return followed
        policy=step['if_not_there']
        lang=self._language_at(target)
        # Nothing is carried for a message or a question, so there is nothing to "bring back".
        if policy=='bring_back' and not step.get('item'):self.speak('not_there_tell',lang,recipient=target)
        else:self.speak('not_there',lang,recipient=target,policy=policy)
        if policy.startswith('leave_with:'):
            alternate=policy.split(':',1)[1]
            return retry(alternate) if retry else self._deliver(step,alternate,fallback=True)
        if policy=='ask_anyone':
            self.emit('screen',text='Enter the approved ID of someone available there, or leave blank to bring it back.')
            raw=self.read('available person> ',lang)
            if short_reply(raw,{'yes','no'},None)['kind']=='stop':
                self.speak('stopped',lang)
                raise RehearsalStopped()
            candidates=tuple(key for key in self.roster.match(raw) if self.roster.person(key)) if raw.strip() else ()
            if len(candidates)==1:
                return retry(candidates[0]) if retry else self._deliver(step,candidates[0],fallback=True)
        if policy=='otherwise':
            # The requester said what to do instead; a carried item stays on board and comes back afterwards.
            return {'status':'failed','target':target,'item':step.get('item'),'reason':'not_here','otherwise_used':True,
                    '_branches':step['otherwise'],'_return_item':step['action']=='deliver'}
        if policy=='skip': return {'status':'skipped','target':target,'item':step.get('item')}
        if policy.startswith('try_later:'):
            minutes=int(policy.split(':')[1])
            self.emit('screen',text=f'Deferred for {minutes} minutes. The console owns scheduling; no real timer runs here.')
            return {'status':'deferred','target':target,'item':step.get('item'),'retry_minutes':minutes}
        if step['action']=='deliver' and step.get('item'):
            self.speak('return_to_requester',self.language,recipient=self.roster.requester_id,item=step['item'])
            returned=self.arrive(self.roster.requester_id,'return')
            return {'status':'failed','target':target,'item':step.get('item'),'reason':'not_here','returned_to_requester':returned}
        return {'status':'failed','target':target,'item':step.get('item'),'reason':'not_here'}

    def _person_hint(self,step,person,retry):
        facts=MapFacts(self.roster);lang=self.roster.language_for(self.current.get('interlocutor'),self.language)
        self.speak('ask_people_where',lang,recipient=person)
        try:raw=self.read('where is the person?> ',lang)
        except EOFError:return None
        lang=self.roster.language_for(self.current.get('interlocutor'),lang)
        heard=_hear_reply(raw,{'location','not_here','no','later'},lang,self.settings)
        self.emit('heard_location',language=lang,result=heard,exact_words=raw)
        if heard['kind']=='stop':
            self.speak('stopped',lang);raise RehearsalStopped()
        if heard['kind']!='answer':return None
        places=facts.location_hint(heard.get('answer_original') or heard.get('answer_english') or '',person)
        log=MapRequests(self.names.get('map_id'),self.source)
        if len(places)!=1:
            log.append('place_update_request',self.roster.requester_id,raw,lang,'Unrecognized person-location hint; no destination guessed',person=person)
            self.speak('map_request_logged',lang);return None
        place=places[0];problem=facts.check(place)
        physical=feasibility([{'n':step['n'],'action':'go','to':place}],self.roster,self.capability())
        if physical.get('reason'):
            self.speak('carrier_refusal',self.language,reason=physical['reason']);return None
        if problem:
            if problem['event']=='place_not_on_map':
                log.append('place_update_request',self.roster.requester_id,raw,lang,'Person-location hint is not on map',problem.get('nearest'),person=person,target=place)
                self.speak('map_request_logged',lang)
            else:self.speak('place_unreachable',self.language,place=place,reason=problem.get('reason','unknown'))
            return None
        if self.settings.get('follow_person_hint','ask')=='never':
            self.emit('screen',text='Location hint recorded as data; follow_person_hint=never. Applying the original absence policy.')
            return None
        self.emit('screen',text='PROPOSED NEW STEP (not yet confirmed): '+json.dumps({'go':place,'continue_action':step['action'],'person':person},ensure_ascii=False))
        self.speak('person_seen_elsewhere',self.language,recipient=person,place=place)
        actor=self.current.pop('interlocutor',None)
        try:
            try:reply=self.reply({'yes','no'},self.language)
            except EOFError:return None
        finally:
            if actor is not None:self.current['interlocutor']=actor
        confirmed=reply['kind']=='yes';self.emit('hint_confirmation',confirmed=confirmed,place=place,person=person)
        if not confirmed:return None
        go={'n':step['n'],'action':'go','to':place,'if_not_there':'bring_back'}
        self.speak('start_step',self.language,step=go)
        self.emit('confirmed_hint_step',place=place,person=person,action=step['action'])
        self.person_places[person]=place
        if not self.arrive(place,'hint'):return self._unreachable(step,person)
        outcome=retry(person) if retry else self._deliver(step,person,fallback=True)
        outcome['at']=place;outcome['followed_location_hint']=True
        usual=facts.usual(person)
        if outcome['status'] in ('delivered','retrieved','checked','told') and place!=usual:
            self.suggest('person_place',{'person':person,'place':place},typed_text=raw,decisions={'location_follow_confirmed':True},outcome=outcome)
            event=log.seen_elsewhere(person,place,usual,self.roster.requester_id,raw,lang)
            if event['kind']=='person_location_update_request':self.speak('map_request_logged',self.language)
        return outcome

    def _deliver(self,step,target,fallback=False):
        if not self.arrive(target):return self._unreachable(step,target)
        lang=self._language_at(target)
        self.speak('arrive_deliver',lang,recipient=target,item=step['item'])
        talk=bool(self.settings.get('conversational'))
        reply=self.reply({'taken','not_here','later','no'} if talk else {'taken','not_here','later'},lang,target,step['item'])
        if reply['kind']=='no':
            # The person does not want it: no argument, it goes back to whoever sent it.
            self.speak('declined',lang)
            return {'status':'failed','target':target,'item':step['item'],'reason':'declined','returned_to_requester':self.arrive(self.roster.requester_id,'return')}
        if reply['kind']=='taken':
            self.speak('got_reply',lang)
            return {'status':'delivered','target':target,'item':step['item'],'alternate_used':fallback}
        if reply['kind']=='not_here' and not fallback: return self._absence(step,target)
        if reply['kind']=='unclear' and not fallback and self._searching(target):return self._absence(step,target,nobody=True)
        if reply['kind']=='later':
            self.emit('screen',text='Recipient asked for later. No delay was invented; console/requester scheduling is required.')
            return {'status':'deferred','target':target,'item':step['item']}
        self.speak('job_failed',self.language,reason='not_here' if reply['kind']=='not_here' else 'no_reply')
        return {'status':'failed','target':target,'item':step['item']}

    def _searching(self,target):
        return bool(self.settings.get('search_for_person')) and bool(self.roster.person(target)) and target not in getattr(self.roster,'temporary',{})

    def _tell(self,step,target,fallback=False):
        if not self.arrive(target):return self._unreachable(step,target)
        lang=self._language_at(target)
        self.speak('arrive_tell',lang,recipient=target)
        self.emit('screen',text='MESSAGE (English plan data): '+step['message'])
        result=self.reply({'done','yes','not_here','later'},lang,*((target,None) if self._searching(target) and not fallback else ()))
        if result['kind']=='unclear' and not fallback and self._searching(target):
            return self._absence(step,target,retry=lambda person:self._tell(step,person,True),nobody=True)
        if result['kind'] in ('done','yes'):
            self.speak('got_reply',lang)
            return {'status':'told','target':target}
        if result['kind']=='not_here' and not fallback:
            return self._absence(step,target,retry=lambda person:self._tell(step,person,True))
        return {'status':'deferred' if result['kind']=='later' else 'failed','target':target}

    def _load(self,steps,first):
        """One visit to the requester loads every item of the coming deliveries, up to the tray limit."""
        from .office_schema import split_items,MAXIMUM_ITEMS
        limit=(self.capability() or {}).get('maximum_items_on_tray',MAXIMUM_ITEMS)
        batch=[];count=0
        for step in steps[steps.index(first):]:
            if step['action']!='deliver' or step['n'] in self.loaded or step['n'] in self.declined:continue
            size=len(split_items(step['item']))
            if batch and count+size>limit:break
            batch.append(step);count+=size
        if not self.arrive(self.roster.requester_id,'pickup'):return self._unreachable(first,self.roster.requester_id)
        for step in batch:
            if self.pickup_safe(step,self.language):self.loaded.add(step['n'])
            else:self.declined.add(step['n'])
        self.emit('load',items=[step['item'] for step in batch if step['n'] in self.loaded],count=count,limit=limit,simulated=not self.drives_robot)
        if len(batch)>1:self.emit('screen',text='ON THE TRAY: '+', '.join(step['item'] for step in batch if step['n'] in self.loaded))
        return None

    def perform(self,steps,top=True):
        if top:self.loaded=set();self.declined=set()
        for step in steps:
            import re
            self.current.pop('interlocutor',None)
            self.last_arrival=None
            action=step['action']
            target=step.get('from') if action=='retrieve' else step.get('at') or step.get('to')
            lang=self._language_at(target)
            # Said before leaving, to the person who asked: in their language, not the next stop's.
            self.speak('start_step',self.language,step=step)
            depends=step.get('item') in self.failed_items or bool(self.failed_items) and (any(item.casefold() in (step.get('message','')+' '+step.get('question','')).casefold() for item in self.failed_items) or bool(re.search(r'\b(?:done|finished|collected|retrieved|ready|it|them|received|delivered)\b',step.get('message',''),re.I)))
            if depends:
                outcome={'status':'skipped','target':target,'item':step.get('item'),'reason':'failed_dependency'}
                self.outcomes.append(outcome);self.emit('outcome',step=step['n'],outcome=outcome);continue
            picked=None
            if action=='deliver':
                # The requester loads the items first, at the requester's place: all of this trip's at once.
                if step['n'] not in self.loaded and step['n'] not in self.declined:picked=self._load(steps,step)
                if picked is None and step['n'] in self.declined:
                    picked={'status':'failed','target':target,'item':step['item'],'reason':'pickup_declined'}
            if picked is None and target in getattr(self.roster,'temporary',{}):
                if not self.arrive(target):
                    outcome=self._unreachable(step,target)
                    if step['action']=='retrieve':self.failed_items.add(step['item'])
                    self.outcomes.append(outcome);self.emit('outcome',step=step['n'],outcome=outcome);continue
                self.emit('screen',text='PERSON: '+self.roster.temporary[target]['name'])
                self.speak('person_present',lang)
                if self.reply({'yes','no'},lang)['kind']!='yes':
                    outcome=self._absence(step,target)
                    if step['action']=='retrieve' and outcome['status']!='retrieved':self.failed_items.add(step['item'])
                    self.outcomes.append(outcome);self.emit('outcome',step=step['n'],outcome=outcome);continue
            if picked is not None: outcome=picked
            elif action=='deliver': outcome=self._deliver(step,target)
            elif action=='retrieve': outcome=self._retrieve(step,target)
            elif action=='check': outcome=self._check(step,target)
            elif action=='tell': outcome=self._tell(step,target)
            elif self.settings.get('conversational') and self.roster.person(target) and target!=self.roster.requester_id and target not in getattr(self.roster,'temporary',{}):
                outcome=self._visit(step,target)
            else: outcome={'status':'went','target':target} if self.arrive(target) else self._unreachable(step,target)
            if step['action']=='retrieve' and outcome['status']!='retrieved':self.failed_items.add(step['item'])
            branches=outcome.pop('_branches',[]);bring_back=outcome.pop('_return_item',False)
            self.outcomes.append(outcome)
            self.emit('outcome',step=step['n'],outcome=outcome)
            if outcome['status'] in ('delivered','retrieved','checked','told','went'):
                self.speak('step_done',self._language_at(outcome['target']))
            if branches: self.perform(branches,top=False)
            if bring_back:
                self.speak('return_to_requester',self.language,recipient=self.roster.requester_id,item=step['item'])
                outcome['returned_to_requester']=self.arrive(self.roster.requester_id,'return')

    def _confirm(self,result):
        """The yes before moving, as a conversation (0.8.0): "no" is asked what to change, and a
        new sentence instead of yes or no is taken as the corrected request. A corrected request is
        understood and read back again; nothing starts without a yes. Two corrections at most."""
        import re
        lang=result['language'];corrections=self.current.get('corrections',0)
        correction=None
        for attempt in range(3):
            raw=self.read('reply> ',lang)
            heard=short_reply(raw,{'yes','no'},lang) or {'kind':'unclear'}
            if heard['kind']=='unclear' and raw.strip() and self.settings.get('reply_model') and detect_language(raw)=='en' and len(raw.split())<3:
                kind=self._model_reply(raw,{'yes','no'})
                if kind:heard={'kind':kind}
            self.emit('heard',language=lang,expect=['no','yes'],result=heard)
            if heard['kind']=='stop':
                self.speak('stopped',lang);raise RehearsalStopped()
            if heard['kind']=='yes':return heard
            if self.settings.get('english_only') and raw.strip() and detect_language(raw)!='en':
                if attempt<2:self.speak('english_only',lang)
                continue
            if heard['kind']=='no':
                if corrections>=2:break
                rest=re.sub(r'^(?:no|nope)\b[\s,.:;-]*','',raw.strip(),flags=re.I)
                if len(rest.split())>=3 and (short_reply(rest,{'yes','no'},lang) or {'kind':'unclear'})['kind']=='unclear':
                    correction=rest;break   # "No, give it to Ravi instead": the rest is the new request
                self.speak('what_change',lang);raw=self.read('reply> ',lang)
                again=short_reply(raw,{'yes','no'},lang) or {'kind':'unclear'}
                if again['kind']=='stop':
                    self.speak('stopped',lang);raise RehearsalStopped()
                if again['kind']=='unclear' and len(raw.split())>=2:correction=raw.strip()
                break
            if len(raw.split())>=3 and corrections<2:
                correction=re.sub(r'^(?:no|nope|wait|sorry|actually|not that)\b[\s,.:;-]*','',raw.strip(),flags=re.I) or None
                break
            if attempt<2:self.speak('say_slowly' if attempt==1 else 'reply_prompt',lang)
        if correction:
            entry={k:v for k,v in self.current.items() if k not in ('continuation','interlocutor','alias_candidate')}
            entry.update(text=correction,corrections=corrections+1)
            self.emit('correction',text=correction,number=corrections+1)
            self.queue.appendleft(entry)
        else:self.speak('dropped',lang)
        return {'kind':'no','corrected':bool(correction)}

    def _visit(self,step,target,fallback=False):
        """"Go to Divya": greet her, say who sent the robot, and wait for what she needs (0.8.0)."""
        if not self.arrive(target):return self._unreachable(step,target)
        lang=self._language_at(target)
        self.current['interlocutor']=target
        self.speak('arrive_visit',lang,recipient=target)
        raw=''
        for call in range(3):
            raw=self.read('anything else> ',lang)
            if raw.strip():break
            if call<2:self.speak('call_person',lang,recipient=target)
        self.current.pop('interlocutor',None)
        if not raw.strip():
            if not fallback and self._searching(target):
                return self._absence(step,target,retry=lambda person:self._visit(step,person,True),nobody=True)
            return {'status':'failed','target':target,'reason':'no_reply'}
        if short_reply(raw,{'not_here'},lang)['kind']=='not_here' and not fallback:
            return self._absence(step,target,retry=lambda person:self._visit(step,person,True))
        asked=self._take_request(target,raw,lang)
        return {'status':'went','target':target,'asked_for_more':asked}

    def _take_request(self,person,raw,lang,asked_what=False):
        """What a person says when the robot offers help: nothing, or a new request of their own.

        True when a request was queued. It runs next, with that person as the requester at this place,
        and is read back for their yes like any request. At most follow_up_limit follow one another.
        """
        import re
        import uuid
        from .office_replies import _contains_stop
        if _contains_stop(raw):
            self.speak('stopped',lang);raise RehearsalStopped()
        heard=short_reply(raw,{'yes','no','later'},lang) or {'kind':'unclear'}
        if heard['kind']=='yes' and not asked_what:
            self.speak('anything_else_what',lang)
            return self._take_request(person,self.read('anything else> ',lang),lang,True)
        text=raw.strip()
        plain=' '.join(re.sub(r"[.,!?;:'’]",' ',text.casefold()).split())
        nothing=re.fullmatch(r"(?:(?:no+|nope|nothing|none|not now|not right now|no need|thats all|that is all|im (?:fine|good)|i am (?:fine|good)|all good|bye|goodbye)(?: (?:thanks|thank you|for now|else|at all))*|(?:no )?(?:thanks|thank you)|(?:no )?(?:you can |please )?go (?:back|home)(?: to (?:the |your )?(?:dock|home|base|station))?|illa|vendam|venda)",plain)
        depth=self.current.get('follow_up',0)
        accepted=bool(text) and len(text)<=2000 and heard['kind']=='unclear' and not nothing and depth<int(self.settings.get('follow_up_limit',3))
        self.emit('closing_question',person=person,place=self.position,follow_up=accepted,depth=depth)
        if not accepted:return False
        names=deepcopy(self.base_names if 'temporary_people' in self.names else self.names)
        names['requester_place']=self.position
        entry={'text':text,'requester':person,'language':None,'follow_up':depth+1,'runtime_names':names,
               'job_id':'follow_'+uuid.uuid4().hex[:12],'asked_after':self.current.get('job_id')}
        self.emit('follow_up_request',job_id=entry['job_id'],text=text,requester=person,place=self.position,depth=depth+1,after=self.current.get('job_id'))
        self.queue.appendleft(entry)
        return True

    def _closing_question(self,result):
        """At the last stop, ask the listed person there for more work before going back to the dock.

        Only a person on the office list is asked, never the requester, a visitor or an empty place;
        not while other requests wait. What they say becomes a new request of their own, with its own
        read-back and yes. At most follow_up_limit requests follow one another (default 3).
        """
        import re
        from .office_replies import _contains_stop
        if not self.settings.get('closing_question') or result.get('queued_parts') or self.queue or not self.outcomes:return
        last=self.outcomes[-1];person=last.get('target')
        if last['status'] not in ('delivered','told','checked') or last.get('provenance')=='helper' or last.get('answered_by_someone_else'):return
        if not self.roster.person(person) or person==self.roster.requester_id or person in getattr(self.roster,'temporary',{}):return
        if self.position is None or self.position!=self.place_for(person):return
        depth=self.current.get('follow_up',0)
        if depth>=int(self.settings.get('follow_up_limit',3)):return
        lang=self.roster.language_for(person,'en')
        self.current['interlocutor']=person
        self.speak('anything_else',lang,recipient=person)
        raw=self.read('anything else> ',lang)
        self.current.pop('interlocutor',None)
        self._take_request(person,raw,lang)

    def _resolve_map_question(self,result):
        question=result['ask'];facts=MapFacts(self.roster)
        proposed=deepcopy(question.get('proposed_steps',[]))
        try:raw=self.read('mapped place, later, or cancel> ',self.language)
        except EOFError:return None
        heard=_hear_reply(raw,{'location','yes','no','later'},self.language,self.settings)
        if heard['kind']=='stop':self.speak('stopped',self.language);raise RehearsalStopped()
        if heard['kind']=='later':return {'deferred':True,'steps':None}
        if heard['kind']=='no':return None
        if heard['kind']=='yes' and len(question.get('options',[]))==1:places=tuple(question['options'])
        elif heard['kind']=='answer':places=facts.location_hint(heard.get('answer_original') or heard.get('answer_english') or '')
        else:places=()
        if len(places)!=1 or facts.check(places[0]):
            self.emit('screen',text='No verified mapped replacement selected. Nothing was changed.')
            return None
        place=places[0];target=question['target']
        if question['event']=='ask_where_person':
            self.transient_places[target]=place
            self.emit('screen',text='One-job person-location context only; names remain unchanged: '+json.dumps({target:place}))
        else:
            for step in walk_steps(proposed):
                for key in ('to','from','at'):
                    if key not in step:continue
                    if step[key]==target:step[key]=place
                    elif self.roster.person(step[key]) and facts.usual(step[key])==target:self.transient_places[step[key]]=place
        remaining=facts.check_steps(proposed,self.transient_places)
        if remaining or feasibility(proposed,self.roster,self.capability(),self.transient_places).get('reason'):
            self.emit('screen',text='Another map fact still needs clarification; no step was accepted: '+json.dumps(remaining))
            return None
        self.emit('map_choice',original_target=target,selected_place=place,confirmed=False,names_changed=False)
        self.emit('screen',text='REVISED PLAN DATA (requires a new yes): '+json.dumps(proposed,ensure_ascii=False))
        self.emit('screen',text='TEMPORARY LOCATION CONTEXT: '+json.dumps(self.transient_places,ensure_ascii=False))
        return {'steps':proposed,'deferred':False}

    def run(self,text=None,requester=None,language=None):
        # A robot caller may fill self.queue itself (entries may carry runtime_names) and pass no text.
        self.emit('session_metadata',source=self.source,owner_meaning_confirmed=False,
                  native_templates_review='needs native-speaker check',typed_input=True,robot=self.drives_robot)
        self.emit('screen',text=('' if self.drives_robot else 'TEXT REHEARSAL ONLY — no robot. ')+AGREEMENT_NOTICE)
        if text is not None:self.queue.append({'text':text,'requester':requester,'language':language})
        jobs=[]
        try:
            while self.queue:
                self.current=self.queue.popleft()
                import uuid
                self.current.setdefault('job_id',uuid.uuid4().hex)
                self.names=deepcopy(self.current.get('runtime_names',self.base_names))
                if not self.current.get('continuation'):self.failed_items=set()
                self.roster=OfficeNames(self.names,self.current['requester'])
                written=detect_language(self.current['text']) if isinstance(self.current.get('text'),str) else 'en'
                self.language=self.current['language'] or (written if written!='en' else self.roster.language_for(self.roster.requester_id,'en'))
                if self.settings.get('english_only'):self.language='en'
                self.outcomes=[]
                self.payloads=[]
                self.transient_places={}
                self.person_places={}
                self.emit('screen',text='Thinking…')
                trace=[]
                result=deepcopy(self.current.get('continuation'))
                if result is None:result=understand(self.current['text'],self.names,self.current['requester'],self.current['language'],{**self.settings,'trace':trace})
                else:
                    problem=MapFacts(self.roster).check_steps(result['steps'])
                    if problem or feasibility(result['steps'],self.roster,self.capability()).get('reason'):
                        result.update(decision='refuse',refuse={'event':'cannot_do','reason':'context_changed'},readback=say('cannot_do',self.language))
                self.emit('understand',result=result,trace=trace,agreement_provisional=True)
                self.emit('screen',text='ENGLISH REWRITE: '+result['english'])
                self.emit('screen',text='PLAN DATA: '+json.dumps(result['steps'],ensure_ascii=False))
                if result.get('omissions'):self.emit('screen',text='OMITTED WITH NOTICE: '+json.dumps(result['omissions'],ensure_ascii=False))
                if result.get('queued_parts'):self.emit('screen',text='QUEUED PLAN DATA: '+json.dumps(result['queued_parts'],ensure_ascii=False))
                self.emit('screen',text='DECISION: '+result['decision'])
                self.emit('robot',event='confirm_plan' if result['decision']=='act' else
                          (result['ask'] or result['refuse'])['event'],language=result['language'],text=result['readback'])
                if result['decision']=='ask' and result['ask']['event']=='unknown_name':
                    resolved=self.resolve_new_person(result)
                    if resolved:
                        result=resolved
                        self.emit('robot',event='confirm_plan',language=self.language,text=result['readback'])
                if result['decision']=='ask' and result['ask']['event'] in ('place_not_on_map','place_unreachable','ask_where_person'):
                    resolution=self._resolve_map_question(result)
                    if resolution and resolution['deferred']:
                        jobs.append({'decision':'deferred','result':result,'outcomes':[{'status':'deferred','target':result['ask']['target']}]})
                        continue
                    if resolution and resolution['steps']:
                        result=deepcopy(result);result.update(decision='act',steps=resolution['steps'],ask=None,refuse=None)
                        result['readback']=say('confirm_plan',self.language,names=self.names,requester=self.current['requester'],steps=result['steps'])
                        self.emit('robot',event='confirm_plan',language=self.language,text=result['readback'])
                if result['decision']!='act':
                    self.record_candidate(result,'not_asked',outcome=result['decision'])
                    jobs.append({'decision':result['decision'],'result':result,'outcomes':[]})
                    continue
                self.current.pop('interlocutor',None)
                confirmation=self._confirm(result) if self.settings.get('conversational') else self.reply({'yes','no'},result['language'])
                self.emit('confirmation',confirmed=confirmation['kind']=='yes')
                if confirmation['kind']!='yes':self.record_candidate(result,confirmation['kind'],outcome='cancelled')
                if confirmation['kind']!='yes':
                    jobs.append({'decision':'cancelled','result':result,'outcomes':[]})
                    continue
                if self.current.get('alias_candidate'):self.suggest('alias',self.current['alias_candidate'],typed_text=self.current['text'],readback=result['readback'],decisions={'readback_yes':True})
                if self.transient_places:
                    for person,place in self.transient_places.items():self.suggest('person_place',{'person':person,'place':place},typed_text=self.current['text'],decisions={'readback_yes':True})
                # The person who asked hears the rest of the job in the language they used.
                self.language=result.get('language') or self.language
                self.perform(result['steps'])
                self.record_candidate(result,'yes',outcome={'steps':self.outcomes,'answers':self.payloads})
                pending=result.get('queued_parts',[])
                if pending:
                    next_result=deepcopy(result);next_result.update(steps=pending[0]['plan']['steps'],queued_parts=pending[1:])
                    next_result['readback']=say('confirm_plan',self.language,names=self.names,requester=self.current['requester'],steps=next_result['steps'])
                    self.queue.appendleft({**self.current,'continuation':next_result,'runtime_names':deepcopy(self.names)})
                self.emit('screen',text='REPORT DATA: '+json.dumps({'outcomes':self.outcomes,'answers':self.payloads},ensure_ascii=False))
                self.speak('report',result['language'],outcomes=self.outcomes)
                jobs.append({'decision':'rehearsed','result':result,'outcomes':list(self.outcomes)})
                self._closing_question(result)
            if any(outcome['status']=='deferred' for job in jobs for outcome in job.get('outcomes',[])):
                self.speak('waiting_later',self.language)
                self.go_home()
                self.emit('session_deferred',note='console/requester must schedule pending retries')
                return {'status':'deferred','jobs':jobs}
            # After small talk or a question nothing was done: go home quietly, without a farewell.
            quiet=bool(self.settings.get('conversational')) and not any(job['decision']=='rehearsed' for job in jobs)
            if not quiet:self.speak('returning_home',self.language)
            self.go_home()
            if not quiet:self.speak('idle',self.language)
        except RehearsalStopped:
            if self.current:self.record_candidate(locals().get('result') or {'readback':''},'unclear',outcome='stopped')
            self.emit('session_stopped',queued_jobs=len(self.queue),outcomes=self.outcomes)
            return {'status':'stopped','jobs':jobs,'outcomes':self.outcomes}
        except (EOFError,StopIteration,KeyboardInterrupt):
            if self.current:
                self.speak('stopped',self.language)
                self.record_candidate(locals().get('result') or {'readback':''},'unclear',outcome='input ended')
            self.emit('session_stopped',reason='input ended; no confirmation inferred')
            return {'status':'stopped','jobs':jobs,'outcomes':self.outcomes}
        self.emit('session_finished',jobs=len(jobs))
        return {'status':'finished','jobs':jobs}
