"""Grammar-first planning with conservative fallback, adaptive draws and map checks."""
from copy import deepcopy
import re
import time
import unicodedata

from .backends import BackendError,BackendTimeout
from .core import _MODEL_LOCK
from .office_backend import LocalOfficeBackend,make_backend,settings_for_office
from .office_grammar import parse_english
from .office_jobs import grammar_job,agreement_job
from .office_capabilities import validate_capability,feasibility
from .office_map import MapFacts,MapRequests
from .office_names import LANGUAGES,OfficeNames
from .office_schema import normalized_plan,TooManySteps,walk_steps
from .office_speech import say,MAP_EVENTS,REFUSAL_HINTS
from .office_clarifications import collection_proposal,first_difference


def social(text,roster):
    """Small talk that needs no plan: a greeting, thanks, "what can you do?", "where is Ravi?"."""
    from .office_map import MapFacts
    said=' '.join(re.sub(r"[.!?,']",' ',text.casefold()).split())
    said=re.sub(r'\s+(?:robot|please)$','',re.sub(r'^(?:hey|hi|hello|ok|okay)\s+robot\s+','',said))
    if re.fullmatch(r'(?:hi|hello|hey|hello there|hi there|good (?:morning|afternoon|evening)|hey robot|hi robot|hello robot)',said):return {'event':'greeting'}
    if re.fullmatch(r'(?:thanks|thank you|thanks a lot|thank you (?:so|very) much|great thanks|good job|well done)',said):return {'event':'welcome'}
    if re.fullmatch(r'(?:what (?:can|do) you do|what (?:all )?can you do(?: for me)?|how can you help(?: me)?|what are you (?:able to do|for)|help|what jobs can you do)',said):return {'event':'capabilities'}
    if re.fullmatch(r'(?:who are you|what are you|what is your name|whats your name)',said):return {'event':'introduce'}
    where=re.fullmatch(r'(?:where is|wheres|where does|where can i find|do you know where)\s+(.+?)(?:\s+(?:sit|sits|sitting|work|works|now|is|seated|usually sit))?',said)
    if where:
        people=[key for key in roster.match(where[1]) if roster.person(key)]
        place=MapFacts(roster).usual(people[0]) if len(people)==1 else None
        if place:return {'event':'person_place','person':people[0],'place':place}
    return None


def run(text,names,requester,language=None,settings=None):
    from .office import detect_language,validate_english,_slot_sources,_condition_issue,_ground,_literal_choices,_agreement_key,substitute_aliases,ask_whether
    started=time.monotonic();pairs=[];config={}
    lang=language if language in LANGUAGES else detect_language(text if isinstance(text,str) else '')
    identity=requester.get('id','') if isinstance(requester,dict) else requester if isinstance(requester,str) else ''
    result={'requester':identity,'language':lang,'english':'','steps':[],'decision':'refuse','ask':None,'refuse':None,
            'readback':'','model':{'name':None,'file_sha256':None},'latency_ms':0.,'planner':None,'draws':0}
    def finish(decision,event=None,reason=None,options=None,slot=None,problem=None):
        result['decision']=decision;result['draws']=len(pairs)
        planners={p.get('planner') for p in pairs}
        result['planner']='model' if 'model' in planners else 'grammar' if 'grammar' in planners else None
        if decision=='ask':
            result['ask']={'event':event,'slot':slot or 'plan','options':options or []}
            if problem:result['ask'].update(problem)
        elif decision=='refuse':result['refuse']={'event':event,'reason':reason}
        if event:
            slots={'names':names,'requester':requester}
            if event in ('ask_which','unknown_name','unknown_place','ask_where_person','clarify_person','clarify_place','clarify_item'):slots['options']=options or []
            if problem:
                for field in ('item','recipient','policy'):
                    if field in problem and event in ('confirm_collect','clarify_absence'):slots[field]=problem[field]
                if event in ('place_not_on_map','place_unreachable'):slots['place']=problem['target']
                if event=='place_not_on_map':slots['nearest']=problem.get('nearest')
                if event=='place_unreachable':slots['reason']=problem.get('reason','unknown')
                if event=='carrier_refusal':slots['reason']=reason
                if event=='ask_where_person':slots['recipient']=problem['target']
                if event=='person_place':slots.update(recipient=problem['person'],place=problem['place'])
            result['readback']=say('map_request_failed',lang) if problem and problem.get('log_status')=='failed' else say(event,lang,**slots)
            if decision=='refuse' and event=='cannot_do' and reason in REFUSAL_HINTS:
                result['readback']+=' '+say(REFUSAL_HINTS[reason],lang)
        result['latency_ms']=round((time.monotonic()-started)*1000,3)
        return result
    if language not in (None,*LANGUAGES) or not isinstance(text,str) or not text.strip():return finish('refuse','cannot_do','invalid_input')
    if len(text)>2000:return finish('ask','split_job','too_many_steps')
    if any(unicodedata.category(c)=='Cs' or (unicodedata.category(c)=='Cc' and c not in '\n\r\t') for c in text):return finish('refuse','cannot_do','invalid_input')
    try:
        if names.get('review_aliases') is not None and not (settings or {}).get('review_only'):raise ValueError('Unapproved review aliases')
        roster=OfficeNames(names,requester);facts=MapFacts(roster)
        # The words decide: a request written in Tamil is answered in Tamil; plain English falls back to the person's own language.
        if language is None and lang=='en':lang=roster.language_for(roster.requester_id,lang)
        result['language']=lang;config=settings_for_office(settings);backend=make_backend(config)
        capability=validate_capability(config.get('capability_profile') or names.get('robot_capability'))
        result['model']={'name':backend.name,'file_sha256':backend.file_sha256}
        if config['draw_policy']=='cross_system' and not getattr(backend,'cross_system',False):raise ValueError('Cross-system backend required')
    except (OSError,ValueError,KeyError,TypeError):return finish('refuse','cannot_do','invalid_context')
    if config['english_only']:
        # The console works in English for now: another language is asked for again, politely.
        written=detect_language(text)
        lang=result['language']='en'
        if written!='en':
            result['language_written']=written
            return finish('ask','english_only','other_language')
    if config['conversational']:
        said=social(text,roster)
        if said:
            return finish('refuse',said['event'],'conversation',problem=said)
    deadline=started+config['timeout_s'];measurement=config['mode']=='measurement';trace=config.get('trace')
    def budget():
        if measurement:return 60.
        left=deadline-time.monotonic()
        if left<=0:raise BackendTimeout('Office deadline exceeded')
        return left
    if not _MODEL_LOCK.acquire(timeout=60. if measurement else max(0.,deadline-time.monotonic())):return finish('refuse','model_timeout','model_timeout')
    cache={}
    def execute(index):
        pair={'index':index,'rewrite_temperature':0. if index==0 else config['sample_temperature'],'rewrite_ms':0.,'plan_ms':0.,'grammar_ms':0.,'grounding_ms':0.,'distinct_pair':True}
        if config['draw_policy']=='cross_system':
            pair['rewrite_source']=backend.rewrite_sources[index];pair['rewrite_temperature']=backend.rewrite_temperatures[index]
        tick=time.monotonic()
        try:
            pair['rewrite_input']=substitute_aliases(unicodedata.normalize('NFC',text),roster)
            pair['rewrite_raw']=backend.rewrite(pair['rewrite_input'],roster,lang,index,budget(),config)
            pair['english']=ask_whether(validate_english(pair['rewrite_raw']));roster.check_preserved_names(text,pair['english'])
            from .office_speech import LAB_ITEMS
            from .core import source_span
            if any(re.search(r'(?<![A-Za-z0-9])'+re.escape(term)+r'(?![A-Za-z0-9])',text,re.I) and not source_span(term,pair['english']) for term in LAB_ITEMS):raise ValueError('Rewrite changed an English lab item term')
            proposal=collection_proposal(pair['english'],roster)
            if proposal:pair['collection_proposal']=proposal
        except BackendTimeout:pair['error']='model_timeout'
        except BackendError:pair['error']='model_unavailable'
        except (ValueError,TypeError,KeyError) as error:pair.update(error='invalid_rewrite',error_detail=str(error))
        pair['rewrite_ms']=(time.monotonic()-tick)*1000
        if pair.get('error')=='invalid_rewrite' and pair.get('english'):
            # A dropped source name still disqualifies the draw. A pure grammar
            # interpretation may explain the differing slot, but never votes/acts.
            try:
                candidate=parse_english(pair['english'],roster.requester_id)
                if candidate and candidate['issue'] is None:
                    _slot_sources(candidate,pair['english'],roster.requester_id)
                    grounded,unknown=_ground(candidate,roster)
                    if not unknown and _condition_issue(candidate,pair['english']) is None:
                        pair['clarification_only_plan']=grounded
            except (ValueError,TypeError,KeyError):pass
        if not pair.get('error') and config['planner_mode']=='model' and len(re.findall(r'^\s*\d+[.)]\s+',pair['english'],re.M))>6:
            pair.update(issue='too_many_steps',planner='precheck')
            pairs.append(pair);return pair
        if not pair.get('error'):
            key=' '.join(unicodedata.normalize('NFC',pair['english']).split())
            if config['deduplicate_plans'] and key in cache:
                original=cache[key];pair.update(plan_reused_from=original['index'],distinct_pair=False)
                for field in ('raw_plan','raw_job','plan','job','planner','issue','uncertain','error','error_detail'):
                    if field in original:pair[field]=deepcopy(original[field])
            else:
                tick=time.monotonic()
                try:
                    english=pair['english'];job=None
                    if config['planner_mode']=='hybrid':
                        job=grammar_job(english,roster.requester_id,(capability or {}).get('maximum_steps_per_job',6))
                        pair['grammar_ms']=(time.monotonic()-tick)*1000
                    pair['planner']='grammar' if job is not None else 'model'
                    if job is None:
                        raw=backend.plan(english,roster,index,budget(),config)
                        job={'issue':raw['issue'],'chunks':[{'english':english,'plan':raw}] if raw['issue'] is None else [],'omissions':[]}
                    pair['plan_ms']=(time.monotonic()-tick)*1000;pair['raw_job']=deepcopy(job)
                    pair['raw_plan']=deepcopy(job['chunks'][0]['plan'] if job['chunks'] else {'steps':[],'issue':job['issue']})
                    tick=time.monotonic()
                    try:
                        pair['issue']=job['issue'];pair['uncertain']=[]
                        if not job['issue']:
                            for chunk in job['chunks']:
                                plan=normalized_plan(chunk['plan'],roster.requester_id)
                                source_english=chunk['english']
                                _slot_sources(plan,source_english,roster.requester_id)
                                issue=_condition_issue(plan,source_english)
                                if issue:pair['issue']=issue;break
                                chunk['plan'],unknown=_ground(plan,roster)
                                pair['uncertain'].extend(unknown)
                                pair['uncertain'].extend(_literal_choices(plan,source_english,roster))
                                if not pair['uncertain']:
                                    source=source_english;targets=[]
                                    for step in walk_steps(chunk['plan']['steps']):
                                        targets.extend(step[k] for k in ('to','from','at') if k in step)
                                        if step['if_not_there'].startswith('leave_with:'):targets.append(step['if_not_there'].split(':',1)[1])
                                        for k in ('question','message','item_hint','item'):
                                            if step.get(k):source=source.replace(step[k],'')
                                        # "Call Ravi to the conference hall": the place is part of the message, not a stop.
                                        if step.get('message','').startswith('Please come to '):source=re.sub(re.escape(step['message'][15:]),'',source,flags=re.I)
                                    roster.check_preserved_names(source,'; '.join(targets))
                            pair['job']=job
                            pair['plan']=job['chunks'][0]['plan']
                    finally:pair['grounding_ms']=(time.monotonic()-tick)*1000
                except TooManySteps:pair['issue']='too_many_steps'
                except BackendTimeout:
                    if not pair['plan_ms']:pair['plan_ms']=(time.monotonic()-tick)*1000
                    pair['error']='model_timeout'
                except BackendError:pair['error']='model_unavailable'
                except (ValueError,TypeError,KeyError) as error:pair.update(error='invalid_plan',error_detail=str(error))
                if pair.get('error'):pair.pop('plan',None);pair.pop('uncertain',None)
                cache[key]=pair
        pair['latency_ms']=sum(pair[k] for k in ('rewrite_ms','plan_ms','grounding_ms'));pairs.append(pair)
        return pair
    def valid(pair):return bool(pair.get('plan')) and not pair.get('error') and not pair.get('uncertain') and pair.get('issue') is None
    try:
        if isinstance(backend,LocalOfficeBackend):backend.verify(config,budget())
        first=execute(0)
        if config['draw_policy']=='cross_system':
            execute(1)
        elif config['draw_policy']=='adaptive' and first.get('planner')=='grammar' and valid(first):
            second=execute(1)
            if not (valid(second) and _agreement_key(agreement_job(first['job']))==_agreement_key(agreement_job(second['job']))):
                execute(2);execute(3)
        else:
            for index in (1,2,3):execute(index)
        result['english']=next((p['english'] for p in pairs if 'english' in p),'')
        if isinstance(backend,LocalOfficeBackend):backend.verify(config,budget())
        if not measurement and time.monotonic()>deadline:return finish('refuse','model_timeout','model_timeout')
    except BackendTimeout:return finish('refuse','model_timeout','model_timeout')
    except BackendError:return finish('refuse','model_unavailable','model_unavailable')
    finally:
        if trace is not None:
            trace.extend(deepcopy(pairs));trace.append({'kind':'pipeline_metrics','rewrite_draws':len(pairs),
              'distinct_pairs':len(cache),'valid_distinct_pairs':sum(valid(p) for p in cache.values()),
              'planned_representatives':sum(p.get('distinct_pair') and 'planner' in p for p in pairs),
              'plans_reused':sum('plan_reused_from' in p for p in pairs),'mode':config['mode'],'draw_policy':config['draw_policy'],
              'rewrite_wall_ms':sum(p['rewrite_ms'] for p in pairs),'plan_and_grounding_wall_ms':sum(p['plan_ms']+p['grounding_ms'] for p in pairs),
              'grounding_total_ms':sum(p['grounding_ms'] for p in pairs),'parallel':1,'requested_parallel':config['pipeline_parallel'],
              'grammar_pairs':sum(p.get('planner')=='grammar' and p['distinct_pair'] for p in pairs),
              'model_pairs':sum(p.get('planner')=='model' and p['distinct_pair'] for p in pairs),
              'office_meanings':config['office_meanings']})
        _MODEL_LOCK.release()
    if any(p.get('error') in ('model_timeout','model_unavailable') for p in pairs):
        reason=next(p['error'] for p in pairs if p.get('error') in ('model_timeout','model_unavailable'));return finish('refuse',reason,reason)
    if any(p.get('issue')=='too_many_steps' for p in pairs):return finish('ask','split_job','too_many_steps')
    proposals=[p['collection_proposal'] for p in pairs if p.get('collection_proposal')]
    if proposals:
        proposal=proposals[0]
        return finish('ask',proposal['event'],options=proposal['options'],slot=proposal['slot'],problem=proposal)
    uncertain=[q for p in pairs for q in p.get('uncertain',[])]
    if uncertain:
        first=uncertain[0];options=list(dict.fromkeys(k for q in uncertain if q['slot']==first['slot'] for k in q['options']))[:3]
        query = first.get('query', '')
        # A clear place phrase should not enter the temporary-person workflow.
        if first['event'] == 'unknown_name' and first.get('action') == 'go' and re.search(
            r'\b(?:room|office|area|hall|lab|laboratory|floor|wing|desk)(?:\s+\d+[a-z]?)?$',
            query.rstrip(' .?!'), re.IGNORECASE,
        ):
            mapped_options = [key for key in options if key in facts.places and not facts.check(key)]
            return finish('ask', 'unknown_place', options=mapped_options, slot=first['slot'],
                          problem={'query': query, 'action': 'go', 'step_n': first['step_n']})
        return finish('ask',first['event'],options=options,slot=first['slot'],problem={**first,'proposed_job':deepcopy(pairs[0].get('job'))} if all(p.get('job') and not p.get('error') and p.get('issue') is None for p in pairs) and len({_agreement_key(agreement_job(p['job'])) for p in pairs})==1 else first)
    plans=[p['plan'] for p in pairs if valid(p)]
    if not plans:
        reason=next((p['issue'] for p in pairs if p.get('issue')),'invalid_plan')
        if reason=='invalid_plan' and config['conversational']:return finish('refuse','not_understood','invalid_plan')
        return finish('refuse','carrier_refusal' if reason in ('requires_stairs','requires_lift','requires_door_operation','unsupported_job','out_of_scope') else 'cannot_do',reason,problem={'reason':reason})
    jobs=[p['job'] for p in pairs if valid(p)]
    if len({_agreement_key(agreement_job(job)) for job in jobs})!=1 and len({_agreement_key(p) for p in plans})==1:return finish('ask','clarify_request','job_disagreement')
    comparable=plans+[p['clarification_only_plan'] for p in pairs if p.get('clarification_only_plan')]
    if len({_agreement_key(p) for p in comparable})!=1:
        question=first_difference(comparable,roster)
        return finish('ask',question['event'],options=question['options'],slot=question['slot'],problem=question)
    if len(plans)!=len(pairs):
        # Some readings failed. With tentative_plans, two or more readings that agree are read back as
        # "please check this" instead of asking the person to say it all again; the yes is still needed.
        if not (config.get('tentative_plans') and len(plans)>=2 and not any(p.get('issue') for p in pairs)):
            return finish('ask','clarify_request','incomplete_agreement')
        result['tentative']=True
    job=jobs[0]
    for chunk in job['chunks']:
        steps=chunk['plan']['steps']
        problem=facts.check_steps(steps)
        if problem:
            problem['proposed_steps']=deepcopy(steps)
            if problem['event']=='place_unreachable':
                result['feasibility']=problem
                return finish('refuse','carrier_refusal','place_unreachable',problem={'reason':'place_unreachable'})
            if problem['event']=='place_not_on_map':
                try:
                    MapRequests(names.get('map_id'),config.get('input_source','UNREVIEWED_TYPED')).append('place_update_request',roster.requester_id,text,lang,'Place is named but not on map',problem.get('nearest'),target=problem['target'])
                    problem['log_status']='recorded'
                except (OSError,ValueError):problem['log_status']='failed'
            # Resolve the entire job later, never expose an unchecked queue.
            if len(job['chunks'])>1:return finish('ask','clarify_request','queued_map_context')
            return finish('ask',problem['event'],options=problem.get('options'),slot=problem['slot'],problem=problem)
        checked=feasibility(steps,roster,capability)
        if checked.get('reason')=='step_limit':return finish('ask','split_job','too_many_steps')
        if checked.get('reason'):
            result['feasibility']=checked
            return finish('refuse','carrier_refusal',checked['reason'],problem=checked)
        chunk['pickup_checks']=checked['pickup_checks']
    result['review_only']=config.get('review_only',False)
    result['steps']=job['chunks'][0]['plan']['steps']
    result['queued_parts']=job['chunks'][1:];result['omissions']=job['omissions']
    result['pickup_checks']=job['chunks'][0]['pickup_checks']
    result['readback']=say('confirm_plan',lang,names=names,requester=requester,steps=result['steps'])
    if result.get('tentative'):result['readback']=say('confirm_tentative',lang)+' '+result['readback'].replace('Sure. ','',1)
    if job['omissions']:result['readback']=say('omissions',lang)+' '+result['readback']
    if result['queued_parts']:result['readback']=say('queue_parts',lang)+' '+result['readback']
    return finish('act')
