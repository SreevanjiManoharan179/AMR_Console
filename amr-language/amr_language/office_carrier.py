"""Human-confirmed carrier/mediator flows. All side effects are rehearsal events."""
from copy import deepcopy
import json
import uuid
from .office_capabilities import item_assessment,validate_capability,feasibility
from .office_names import OfficeNames
from .office_map import MapFacts
from .office_replies import short_reply,_contains_stop
from .office_schema import walk_steps
from .office_speech import say


class CarrierFlows:
    def capability(self):
        return validate_capability(self.settings.get('capability_profile') or self.names.get('robot_capability'))

    def pickup_safe(self,step,lang):
        check=item_assessment(step['item'],self.roster,self.capability())
        if check['rule']=='not_allowed':return False
        if check['rule']=='ask_at_pickup':
            self.speak('pickup_check',lang)
            if self.reply({'yes','no'},lang)['kind']!='yes':return False
        if check.get('requires_bag'):
            self.speak('bag_check',lang)
            if self.reply({'yes','no'},lang)['kind']!='yes':return False
        return True

    def verbatim_answer(self,lang):
        # No language backend is ever called for a human answer.
        raw=self.read('exact answer> ',lang)
        if _contains_stop(raw):
            from .rehearsal import RehearsalStopped
            self.speak('stopped',lang);raise RehearsalStopped()
        status=short_reply(raw,{'not_here','later'},lang)
        if status and status['kind'] in ('not_here','later'):return status
        if not raw.strip() or len(raw)>2000:return {'kind':'unclear'}
        return {'kind':'answer','answer_original':raw}

    def _check(self,step,target,fallback=False,helper=False):
        if not self.arrive(target):return self._unreachable(step,target)
        lang=self._language_at(target)
        self.speak('helper_check' if helper else 'arrive_check',lang)
        self.emit('screen',text='QUESTION: '+step['question'])
        result=self.verbatim_answer(lang)
        if not fallback and not helper and self._searching(target):
            # Nobody answers: call the person by name twice more, then look for them.
            for _ in range(2):
                if result['kind']!='unclear':break
                self.speak('call_person',lang,recipient=target);result=self.verbatim_answer(lang)
            if result['kind']=='unclear':return self._absence(step,target,retry=lambda p:self._check(step,p,True),nobody=True)
        if result['kind']=='not_here' and not fallback:return self._absence(step,target,retry=lambda p:self._check(step,p,True))
        for attempt in range(2):
            if result['kind']!='answer':return {'status':'failed','target':target,'reason':'no_verified_answer'}
            exact=result['answer_original'];self.emit('screen',text='EXACT ANSWER: '+exact)
            self.speak('verify_answer',lang)
            confirmation=self.reply({'yes','no'},lang)
            self.emit('answer_verification',confirmed=confirmation['kind']=='yes',exact_answer=exact,attempt=attempt+1)
            if confirmation['kind']=='yes':break
            if confirmation['kind']!='no' or attempt==1:return {'status':'failed','target':target,'reason':'answer_not_verified'}
            self.speak('correct_answer',lang);result=self.verbatim_answer(lang)
        payload={'at':target,'answer_original':exact,'verified':True,'answered_by_someone_else':helper,'helper_name':self.current.get('helper_name') if helper else None}
        self.payloads.append(payload)
        self.speak('received_answer',self.language);self.emit('screen',text='RETURNED EXACT ANSWER: '+exact)
        outcome={'status':'checked','target':target,'answered_by_someone_else':helper}
        if helper:outcome.update(provenance='helper',helper_name=self.current.get('helper_name'))
        if any(w in step['question'].casefold() for w in ('where','at her desk','at his desk','position')):
            places=MapFacts(self.roster).location_hint(exact,target)
            if len(places)==1 and not MapFacts(self.roster).check(places[0]):self.suggest('person_place',{'person':target,'place':places[0]},typed_text=exact,decisions={'answer_verified':True})
            if len(places)==1 and not MapFacts(self.roster).check(places[0]) and not feasibility([{'n':1,'action':'go','to':places[0]}],self.roster,self.capability()).get('reason') and self.settings.get('follow_person_hint','ask')=='ask':
                self.emit('screen',text='PROPOSED NEW STEP: '+json.dumps({'action':'go','to':places[0]}))
                self.speak('person_seen_elsewhere',self.language,recipient=target,place=places[0])
                actor=self.current.pop('interlocutor',None)
                try:accepted=self.reply({'yes','no'},self.language)['kind']=='yes'
                finally:
                    if actor is not None:self.current['interlocutor']=actor
                self.emit('requester_position_confirmation',requester=self.roster.requester_id,confirmed=accepted)
                if accepted:
                    self.emit('confirmed_hint_step',place=places[0],person=target,action='go')
                    self.person_places[target]=places[0]
                    if self.arrive(places[0],'hint'):self.outcomes.append({'status':'went','target':places[0]})
                    else:self.outcomes.append(self._unreachable({'n':step['n'],'action':'go','to':places[0]},places[0]))
        if step.get('on_answer'):
            branch=short_reply(exact,{'yes','no'},lang)
            if branch and branch['kind'] in ('yes','no'):outcome['_branches']=step['on_answer'][branch['kind']]
            else:outcome['branch_status']='not_run_answer_not_yes_no'
        return outcome

    def _retrieve(self,step,target,fallback=False,helper=False):
        if not self.arrive(target):return self._unreachable(step,target)
        lang=self._language_at(target)
        self.emit('screen',text='ITEM: '+step['item']+'; HINT: '+step.get('item_hint',''))
        if step.get('item_location')=='guess':self.speak('guess_location',lang)
        self.speak('helper_retrieve' if helper else 'arrive_retrieve',lang,item=step['item'])
        result=self.reply({'done','not_here','later'},lang,target,step['item'])
        if result['kind']=='done':
            if helper:
                self.speak('verify_item',lang)
                if self.reply({'yes','no'},lang)['kind']!='yes':return {'status':'failed','target':target,'item':step['item'],'reason':'wrong_item','provenance':'helper'}
            if not self.pickup_safe(step,lang):return {'status':'failed','target':target,'item':step['item'],'reason':'pickup_declined'}
            self.speak('return_to_requester',self._language_at(step['to']),recipient=step['to'],item=step['item'])
            outcome=self._deliver(step,step['to'],fallback=True)
            if outcome['status']=='delivered':outcome['status']='retrieved'
            outcome['collected_from']=target
            if helper:outcome.update(provenance='helper',helper_name=self.current.get('helper_name'))
            return outcome
        if result['kind']=='not_here' and step.get('item_location')=='guess':
            self.speak('not_found',self.language)
            return {'status':'failed','target':target,'item':step['item'],'reason':'not_found','not_found_at':target}
        if result['kind']=='not_here' and not fallback:return self._absence(step,target,retry=lambda p:self._retrieve(step,p,True))
        if result['kind']=='unclear' and not fallback and not helper and self._searching(target):
            return self._absence(step,target,retry=lambda p:self._retrieve(step,p,True),nobody=True)
        return {'status':'deferred' if result['kind']=='later' else 'failed','target':target,'item':step['item'],'reason':'not_here'}

    def helper_allowed(self,step):
        if step['action']=='retrieve':return True
        if step.get('if_not_there')!='ask_anyone':return False
        if step['action'] not in ('deliver','check'):return False
        return not feasibility([step],self.roster,self.capability()).get('reason')

    def _absence(self,step,target,retry=None,nobody=False):
        # Nobody answered at all: there is no helper to ask here, so look for the person.
        if nobody:return self._absence_policy(step,target,retry=retry,seek_hint=True,nobody=True)
        if self.roster.person(target) and self.helper_allowed(step):
            lang=self._language_at(target)
            self.emit('helper_permission',action=step['action'],allowed=True,target=target)
            if step['action']=='retrieve':outcome=self._retrieve(step,target,True,helper=True)
            elif step['action']=='check':outcome=self._check(step,target,True,helper=True)
            else:
                self.speak('helper_deliver',lang)
                r=self.reply({'taken','not_here','later'},lang)
                outcome={'status':'delivered' if r['kind']=='taken' else 'failed','target':target,'item':step['item'],'provenance':'helper','helper_name':self.current.get('helper_name')}
            if outcome['status'] in ('retrieved','delivered','checked'):
                outcome.setdefault('provenance','helper');outcome.setdefault('helper_name',self.current.get('helper_name'))
                self.speak('helper_report',self.language);return outcome
            self.emit('helper_wait_boundary',maximum_wait_s=self.capability()['maximum_wait_at_stop_s'],simulated=True)
            # No helper succeeded. Apply original skip/defer/return, not an endless retry.
            safe=deepcopy(step)
            if safe['if_not_there']=='ask_anyone':safe['if_not_there']='bring_back'
            return self._absence_policy(safe,target,retry=None,seek_hint=False)
        return self._absence_policy(step,target,retry=retry,seek_hint=target not in getattr(self.roster,'temporary',{}))

    def resolve_new_person(self,result):
        q=result['ask'];job=deepcopy(q.get('proposed_job'))
        if not job or q.get('slot') not in ('to','from','at') or not q.get('query') or len(q['query'])>100:return None
        self.emit('screen',text='UNKNOWN PERSON: '+q['query'])
        self.speak('new_person')
        raw_choice=self.read('exists yes/no, or approved identity> ')
        if _contains_stop(raw_choice):
            from .rehearsal import RehearsalStopped
            raise RehearsalStopped()
        selected=self.roster.match(raw_choice)
        if len(selected)==1:
            target=selected[0]
            for chunk in job['chunks']:
                for step in walk_steps(chunk['plan']['steps']):
                    for slot in ('to','from','at'):
                        if step.get(slot)==q['query']:step[slot]=target
                if MapFacts(self.roster).check_steps(chunk['plan']['steps']) or feasibility(chunk['plan']['steps'],self.roster,self.capability()).get('reason'):return None
            self.current['alias_candidate']={'alias':q['query'],'target':target}
            updated=deepcopy(result);updated.update(decision='act',steps=job['chunks'][0]['plan']['steps'],queued_parts=job['chunks'][1:],ask=None,refuse=None)
            updated['readback']=say('confirm_plan',self.language,names=self.names,requester=self.current['requester'],steps=updated['steps'])
            return updated
        if short_reply(raw_choice,{'yes','no'},self.language)['kind']!='yes':return None
        facts=MapFacts(self.roster)
        self.emit('screen',text='Where is '+q['query']+' now? Choose: '+', '.join(facts.mapped(reachable=True)))
        raw=self.read('mapped place> ')
        if _contains_stop(raw):
            from .rehearsal import RehearsalStopped
            raise RehearsalStopped()
        places=facts.location_hint(raw)
        if len(places)!=1 or facts.check(places[0]):return None
        ident='temporary_'+uuid.uuid4().hex
        temporary={'id':ident,'name':q['query'],'usual_place':places[0],'requester':self.roster.requester_id,'job_id':self.current['job_id']}
        runtime=deepcopy(self.names);runtime['temporary_people']=[temporary]
        roster=OfficeNames(runtime,self.current['requester'])
        for chunk in job['chunks']:
            for step in walk_steps(chunk['plan']['steps']):
                for slot in ('to','from','at'):
                    if step.get(slot)==q['query']:step[slot]=ident
            if MapFacts(roster).check_steps(chunk['plan']['steps']) or feasibility(chunk['plan']['steps'],roster,self.capability()).get('reason'):return None
        self.names=runtime;self.roster=roster
        self.suggest('person_place',{'person':q['query'],'place':places[0]},typed_text=self.current['text'],decisions={'person_exists':True,'place_selected':True})
        updated=deepcopy(result);updated.update(decision='act',steps=job['chunks'][0]['plan']['steps'],queued_parts=job['chunks'][1:],ask=None,refuse=None)
        updated['readback']=say('confirm_plan',self.language,names=runtime,requester=self.current['requester'],steps=updated['steps'])
        return updated

    def suggest(self,kind,candidate,**evidence):
        if self.source in ('SCRIPTED','STARTER_SCRIPTED'):
            self.emit('learning_suggestion',source='SCRIPTED',kind_name=kind,candidate=candidate,applied=False)
            return
        from .office_learning import LearningStore
        store=LearningStore(retention_days=self.settings.get('learning_retention_days',30));store.prune()
        store.append(kind,candidate,source=self.source,requester_id=self.roster.requester_id,language=self.language,**evidence)

    def record_candidate(self,result,decision,outcome):
        if not self.current['text'].strip():return
        self.suggest('owner_row',{'language':self.language,'text':self.current['text'],'expected_english':self.current['text'],'expected_steps_notes':'Owner must supply/confirm intended English and steps; candidate feedback only'},typed_text=self.current['text'],readback=result.get('readback',''),decisions={'requester_reply':decision},outcome=outcome)
