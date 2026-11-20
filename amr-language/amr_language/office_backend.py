"""Office rewrite -> plan using the installed loopback-only model. No downloads."""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import threading

from .backends import BackendError,BackendTimeout,OllamaBackend
from .calibration import ROOT,file_sha256,local_path
from .office_schema import SCHEMA_V3

REWRITE_PROMPT='''Translate the user's text into plain English. If it is already English, preserve it.
Output only the translated request, without headings, explanations, examples or JSON.
Keep every action, name, item, number, quoted message, question and condition in the same order.
Keep English technical item terms exactly as said, including encoder. Write proper names in English letters. Do not add facts or new tasks. Do not perform the request.'''

# Tamil typed in English letters (detected in office.romanised_tamil).
ROMAN_TAMIL_HINT='''
The text is Tamil typed in English letters, mixed with English words. Translate it to English.
Word meanings: kitta = to (a person); kitta irunthu = from (a person); kudunga, kodunga = give; kelunga = ask;
sollunga = tell; eduthuttu vaanga = collect and bring; vaanga = come; ponga = go; -ku = to (a place);
-a, -ai after a word marks the item; nu = that; ready-aa = whether it is ready.'''

PLAN_PROMPT='''Convert ONLY the current English office rewrite into the supplied JSON plan schema.
Actions: deliver(item,to); retrieve(item,from,to=requester); check(at,question,on_answer);
tell(to,message); go(to). Never use return, wait, drive, pickup or other actions.
Use exactly the requested main actions and order. The console adds idle return to dock.
An explicit come-back-to-requester is go(to=requester); explicit go home is go(to=dock ID).
Copy every item, target, question and message from the English rewrite, not from examples.
Absent retrieve.to is null and will default to requester. Copy quoted questions/messages verbatim.
Default if_not_there is bring_back. Allowed: bring_back, leave_with:PERSON, try_later:MINUTES,
ask_anyone, skip. No condition other than absence or a check's yes/no on_answer is allowed.
on_answer is null unless explicitly requested. Otherwise use yes/no arrays containing only
tell or deliver. Branches are one level deep. At most SIX actions including both branches.
When unsupported, negated, conditional in another way, or too long, output steps:[] with
issue respectively out_of_scope, negation, unsupported_condition or too_many_steps.
For a supported plan issue MUST be null. Unknown destinations are still copied for later grounding.
Each step gets n in spoken order. Never manufacture recipients or infer a question/message.
Use the schema; no additional fields. Do not turn an unsupported task into navigation.'''

REPLY_PROMPT='''You label one short spoken reply given to an office delivery robot.
The robot asked a question and can accept only the labels listed in "allowed".
yes = agrees or confirms. no = disagrees, refuses or declines. taken = the person has taken the item.
done = the person has put the item on the robot, or has read the message. not_here = the person the
robot wants is not here. later = come back later, the person is busy.
If the reply does not clearly mean one allowed label, the label is unclear. Never guess.
Output JSON only: {"kind": "<label>"}.'''

ANSWER_PROMPT='''Rewrite this short answer into plain English. Preserve its meaning and names.
Output the English answer only. Do not answer a question, add facts, give instructions or use JSON.'''


class MockOfficeBackend:
    name='mock-office'
    file_sha256=None
    def __init__(self,rewrites,plans,answers=None):
        self.rewrites=deepcopy(rewrites)
        self.plans=deepcopy(plans)
        self.answers=answers or {}
        self.calls=[]
    def rewrite(self,text,roster,language,index,timeout,settings):
        self.calls.append(('rewrite',index,0. if index==0 else settings['sample_temperature']))
        value=self.rewrites[index%len(self.rewrites)]
        if isinstance(value,Exception): raise value
        return value
    def plan(self,english,roster,index,timeout,settings):
        self.calls.append(('plan',index))
        value=self.plans[index%len(self.plans)]
        if isinstance(value,Exception): raise value
        return deepcopy(value)
    def classify(self,text,allowed,timeout,settings):
        self.calls.append(('classify',text))
        return getattr(self,'reply_kinds',{}).get(text)
    def answer(self,text,language,timeout,settings):
        self.calls.append(('answer',text))
        value=self.answers.get(text,text)
        if isinstance(value,Exception): raise value
        return value


class LocalOfficeBackend:
    def __init__(self,settings):
        self.name=settings['model']
        self.file_sha256=file_sha256(local_path(settings['model_file']))
        self.client=OllamaBackend(self.name,self.file_sha256)
        self.client.load_manifest(local_path(settings['model_manifest_file']))
        self.stats=[]
        self.stats_lock=threading.Lock()
    def verify(self,settings,timeout):
        self.client.verify_identity(settings,timeout)
    def _chat(self,system,user,temperature,index,timeout,settings,schema=None,tokens=384):
        payload={'model':self.name,'stream':False,'think':False,'keep_alive':'5m',
          'messages':[{'role':'system','content':system},{'role':'user','content':user}],
          'options':{'temperature':temperature,'num_ctx':settings['num_ctx'],'num_predict':tokens,
                     'seed':settings['seed']+index,'presence_penalty':0.,'top_p':.95,'top_k':20}}
        if schema is not None: payload['format']=schema
        started=time.monotonic()
        value=self.client._request('POST','/api/chat',payload,settings['port'],timeout)
        if not isinstance(value,dict): raise BackendError('Invalid office-model response')
        with self.stats_lock:
            self.stats.append({'stage':'plan' if schema is not None else 'rewrite','index':index,
                'wall_ms':(time.monotonic()-started)*1000,
                'output_text':value.get('message',{}).get('content') if isinstance(value.get('message'),dict) else None,
                **{key:value.get(key) for key in ('eval_count','prompt_eval_count','prompt_eval_cached_count',
                    'eval_duration','prompt_eval_duration','load_duration','total_duration','done_reason')}})
        if not isinstance(value,dict) or value.get('done') is not True or value.get('done_reason')=='length':
            raise ValueError('Incomplete or truncated office-model response')
        try: return value['message']['content']
        except (KeyError,TypeError) as error: raise BackendError('Missing office-model content') from error
    def rewrite(self,text,roster,language,index,timeout,settings):
        aliases=[]
        for row in roster.prompt_roster(language)['approved']:
            for alias in row['aliases']:
                if alias in text and not alias.isascii(): aliases.append(alias+' = '+row['id'])
        context=('\nName spellings: '+'; '.join(aliases)) if aliases else ''
        if language=='ta' and text.isascii():context+=ROMAN_TAMIL_HINT
        if settings['office_meanings']:
            meanings,_=load_office_meanings()
            entries=meanings['languages']['en']+meanings['languages'].get(language,[])
            if settings.get('review_only') and settings.get('review_meaning'):entries=entries+[settings['review_meaning']]
            context+='\nGeneral office meanings (not extra tasks): '+ '; '.join(entry['word']+' = '+entry['meaning'] for entry in entries)
        cap=min(settings['rewrite_tokens'],max(64,len(text)*2//3))
        return self._chat(REWRITE_PROMPT+context,text,0. if index==0 else settings['sample_temperature'],index,timeout,settings,tokens=cap)
    def plan(self,english,roster,index,timeout,settings):
        from .schema import parse_json
        user=json.dumps({'requester':roster.requester_id,'english':english},ensure_ascii=False)
        raw=self._chat(PLAN_PROMPT,user,0.,100+index,timeout,settings,SCHEMA_V3,settings['plan_tokens'])
        return parse_json(raw)
    def classify(self,text,allowed,timeout,settings):
        """The model's reading of a free reply: one of the allowed kinds, or None."""
        from .schema import parse_json
        kinds=sorted(allowed)
        schema={'type':'object','additionalProperties':False,'required':['kind'],'properties':{'kind':{'enum':kinds+['unclear']}}}
        raw=self._chat(REPLY_PROMPT,json.dumps({'allowed':kinds,'reply':text},ensure_ascii=False),0.,300,timeout,settings,schema,24)
        kind=parse_json(raw).get('kind')
        return kind if kind in kinds else None
    def answer(self,text,language,timeout,settings):
        return self._chat(ANSWER_PROMPT,json.dumps({'language':language,'answer':text},ensure_ascii=False),
                          0.,200,timeout,settings,tokens=192)



class CrossSystemOfficeBackend(LocalOfficeBackend):
    """One isolated deterministic C translation, one real B greedy rewrite."""
    cross_system=True
    rewrite_sources=('C_translation','B_greedy')
    rewrite_temperatures=(None,0.)
    def __init__(self,local_backend,translator):
        from .office_c_client import CWorker
        if not isinstance(local_backend,LocalOfficeBackend) or not isinstance(translator,CWorker):
            raise ValueError('Verified local planner and isolated C worker required')
        if translator.ready.get('isolated') is not True:raise ValueError('C must run with Python -I')
        for key in ('name','file_sha256','client','stats','stats_lock'):setattr(self,key,getattr(local_backend,key))
        self.translator=translator
    def rewrite(self,text,roster,language,index,timeout,settings):
        if index==0:return self.translator.translate(text,language,timeout)
        if index==1:return super().rewrite(text,roster,language,0,timeout,settings)
        raise ValueError('Cross-system comparison has exactly two draws')

DEFAULT_SETTINGS={'closing_question':False,'follow_up_limit':3,'search_for_person':False,'tentative_plans':False,'conversational':False,'english_only':False,'reply_model':False,'model':'qwen3.5:2b-q4_K_M','timeout_s':5.,'reply_timeout_s':1.,
 'num_ctx':4096,'rewrite_tokens':384,'plan_tokens':768,'seed':1729,'port':11434,
 'runner_version':'0.40.12','mode':'product','pipeline_parallel':1,'deduplicate_plans':True,'planner_mode':'hybrid','draw_policy':'adaptive','office_meanings':False,'follow_person_hint':'ask','input_source':'UNREVIEWED_TYPED','sample_temperature':.7,'capability_profile':None,'learning_retention_days':30,'review_only':False,'review_meaning':None}

_LOCAL_CACHE={}
_CACHE_LOCK=threading.Lock()


def _artifact_stamp(settings):
    result=[]
    for key in ('model_file','model_manifest_file'):
        path=local_path(settings[key]);stat=path.stat()
        result.append((str(path),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
    return tuple(result)


def prepare_office(settings=None):
    """Hash/validate once at worker startup, before measuring user requests.

    Calls still fail closed if pinned artifacts change. Lazy first-use is retained
    for old callers, but its cost remains in that call's measured latency.
    """
    config=settings_for_office(settings)
    started=time.monotonic()
    glossary=load_office_meanings() if config['office_meanings'] else None
    backend=make_backend(config)
    return {'office_meanings_sha256':glossary[1] if glossary else None,'model':backend.name,'file_sha256':backend.file_sha256,
            'startup_hash_and_manifest_ms':round((time.monotonic()-started)*1000,3)}


def load_office_profile(name='office_B'):
    if name!='office_B':raise ValueError('Unknown office profile')
    return settings_for_office(json.loads((ROOT/'data/office_B.json').read_text()))


def settings_for_office(supplied=None):
    if supplied is not None and not isinstance(supplied,dict): raise ValueError('Invalid settings')
    path=ROOT/'data/office_model_A.json'
    installed=json.loads(path.read_text()) if path.exists() else {}
    settings={**DEFAULT_SETTINGS,**installed,**(supplied or {})}
    allowed={*DEFAULT_SETTINGS,'model_file','model_manifest_file','backend','trace'}
    if set(settings)-allowed: raise ValueError('Unknown office setting')
    for key,low,high in (('timeout_s',.01,120.),('reply_timeout_s',.01,30.)):
        value=settings[key]
        if type(value) not in (int,float) or not low<=value<=high: raise ValueError('Invalid deadline')
    for key,low,high in (('num_ctx',1024,4096),('rewrite_tokens',64,1024),('plan_tokens',128,1536),
                         ('seed',0,2**31-201),('port',1,65535)):
        if type(settings[key]) is not int or not low<=settings[key]<=high: raise ValueError('Invalid integer setting')
    if not isinstance(settings['model'],str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,150}',settings['model']) or 'cloud' in settings['model'].lower():
        raise ValueError('Local model required')
    if settings.get('backend') is not None and not isinstance(settings['backend'],(MockOfficeBackend,CrossSystemOfficeBackend)):
        raise ValueError('Only explicit mock or verified cross-system backend may be injected')
    if settings.get('trace') is not None and not isinstance(settings['trace'],list):
        raise ValueError('Trace must be a caller-owned list')
    if any(type(settings[key]) is not bool for key in ('search_for_person','tentative_plans','conversational','english_only','reply_model')):raise ValueError('Invalid behaviour settings')
    if type(settings['closing_question']) is not bool or type(settings['follow_up_limit']) is not int or not 0<=settings['follow_up_limit']<=10:raise ValueError('Invalid closing question settings')
    if type(settings['learning_retention_days']) is not int or not 1<=settings['learning_retention_days']<=3650:raise ValueError('Invalid learning retention')
    if settings['capability_profile'] is not None:
        from .office_capabilities import validate_capability
        validate_capability(settings['capability_profile'])
    if type(settings['review_only']) is not bool or settings['review_meaning'] is not None and not settings['review_only']:raise ValueError('Review context requires review_only')
    if settings['mode'] not in ('product','measurement'): raise ValueError('Invalid office mode')
    if type(settings['pipeline_parallel']) is not int or settings['pipeline_parallel'] not in (1,4):
        raise ValueError('Invalid pipeline parallelism')
    if type(settings['deduplicate_plans']) is not bool: raise ValueError('Invalid duplicate policy')
    if type(settings['sample_temperature']) not in (int,float) or not 0<=settings['sample_temperature']<=1:raise ValueError('Invalid sample temperature')
    if settings['planner_mode'] not in ('hybrid','model'):raise ValueError('Invalid planner mode')
    if settings['draw_policy'] not in ('adaptive','four','cross_system'):raise ValueError('Invalid draw policy')
    if type(settings['office_meanings']) is not bool:raise ValueError('Invalid office glossary flag')
    if settings['follow_person_hint'] not in ('ask','never'):raise ValueError('Invalid person-hint policy')
    if settings['input_source'] not in ('UNREVIEWED_TYPED','STARTER_MEASUREMENT','SCRIPTED','OWNER_TYPED'):raise ValueError('Invalid input source')
    return settings


def make_backend(settings):
    if settings.get('backend') is not None: return settings['backend']
    key=(settings['model'],str(local_path(settings['model_file'])),str(local_path(settings['model_manifest_file'])))
    stamp=_artifact_stamp(settings)
    with _CACHE_LOCK:
        if key in _LOCAL_CACHE:
            old_stamp,backend=_LOCAL_CACHE[key]
            if old_stamp!=stamp: raise ValueError('Prepared model artifacts changed; restart the worker')
            return backend
        backend=LocalOfficeBackend(settings)
        _LOCAL_CACHE[key]=(stamp,backend)
        return backend


def load_office_meanings():
    import hashlib
    path=ROOT/'data/office_meanings.json'
    expected=(ROOT/'data/office_meanings.sha256').read_text().strip()
    raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if digest!=expected:raise ValueError('Office meanings file changed')
    return json.loads(raw),digest
