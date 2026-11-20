"""Exact phrase replies with expectation filtering. Non-English lists need review."""
import re
import time
import unicodedata

from .backends import BackendError,BackendTimeout
from .core import _MODEL_LOCK
from .grounding import normalize
from .office import detect_language,validate_english
from .office_backend import make_backend,settings_for_office
from .office_names import LANGUAGES
from .schema import valid_string

KINDS={'taken','done','not_here','yes','no','later','stop','answer','unclear','location'}
WORDS={
 'en':{
  'taken':['taken','got it','i have it','received','i got it','i took it','took it','i have taken it','thanks','thank you','taken thanks','got it thanks'],
  'done':['done','finished','placed','placed it','ready','its done','it is done','i have placed it','i put it','put it','kept it','i kept it','its on the tray','read it','i have read it','noted','understood'],
  'not_here':['not here','away','not available','nobody here','she is not here','he is not here','they are not here','shes not here','hes not here','not at the desk','not in','on leave','gone out','stepped out','absent','not around','not in today'],
  'yes':['yes','yeah','yep','ok','okay','sure','go ahead','yes go ahead','yeah go ahead','ok go ahead','okay go ahead','sure go ahead','yes thats right','yes correct','please do','yes please','correct','right','thats right','that is right','thats correct','fine','alright','all right','yes it is','yes she is','yes he is'],
  'no':['no','nope','not mine','its not mine','i dont want it','dont want it','not for me','wrong person','no thanks','no thank you','not now','wrong','thats wrong','that is wrong','not right','dont','no dont','nothing','nothing else','thats all'],
  'later':['later','come later','come back later','not now come later','busy','im busy','i am busy','in a while'],
  'stop':['stop','stop now','cancel','please stop'],
 },
 'ta':{
  'taken':['எடுத்துக்கிட்டேன்','எடுத்துட்டேன்','வாங்கிக்கிட்டேன்','eduthukitten','eduthutten'],
  'done':['முடிந்தது','முடிஞ்சது','வச்சுட்டேன்','mudinjathu','vechutten'],
  'not_here':['இங்க இல்லை','இல்ல','அவங்க இல்லை','inga illa','illa'],
  'yes':['ஆம்','ஆமா','சரி','aama','sari'],
  'no':['இல்லை','இல்ல','வேண்டாம்','illa','vendam'],
  'later':['அப்புறம் வா','பிறகு வாங்க','கொஞ்சம் கழிச்சு வாங்க','appuram va','apparam vaanga'],
  'stop':['நிறுத்து','நிறுத்துங்க','நிறுத்துங்கள்','niruthu','niruthunga'],
 },
 'ml':{
  'taken':['എടുത്തു','എടുത്തിട്ടുണ്ട്','കിട്ടി','eduthu','kitti'],
  'done':['കഴിഞ്ഞു','വച്ചു','പൂർത്തിയായി','kazhinju','vechu'],
  'not_here':['ഇവിടെ ഇല്ല','ഇല്ല','അവിടെ ഇല്ല','ivide illa','illa'],
  'yes':['അതെ','ശരി','ഉവ്വ്','athe','sheri'],
  'no':['അല്ല','ഇല്ല','വേണ്ട','alla','venda'],
  'later':['പിന്നെ വരൂ','പിന്നീട് വരൂ','കുറച്ച് കഴിഞ്ഞ് വരൂ','pinne varu','pinneedu varu'],
  'stop':['നിർത്തൂ','നിർത്തുക','നിർത്തണം','nirthu','nirthuka'],
 },
 'te':{
  'taken':['తీసుకున్నాను','అందుకుంది','వచ్చింది','teesukunnanu','andindi'],
  'done':['అయిపోయింది','పెట్టాను','పూర్తయింది','ayipoyindi','pettanu'],
  'not_here':['ఇక్కడ లేరు','ఇక్కడ లేదు','లేరు','ikkada leru','leru'],
  'yes':['అవును','సరే','అలాగే','avunu','sare'],
  'no':['కాదు','లేదు','వద్దు','kaadu','ledu'],
  'later':['తర్వాత రండి','తరువాత రా','కాసేపటి తర్వాత రండి','tarvata randi','taruvata ra'],
  'stop':['ఆపు','ఆపండి','నిలిపివేయండి','aapu','aapandi'],
 },
}
ENGLISH={'taken':'Taken.','done':'Done.','not_here':'Not here.','yes':'Yes.','no':'No.',
         'later':'Come later.','stop':'Stop.'}


def normalized_reply(value):
    return normalize(value).strip(' .,!?:;"\'“”‘’')


def _loose(value):
    """A spoken reply as speech-to-text writes it ("Yeah, go ahead." / "Taken, thanks!") in bare words."""
    value=re.sub(r"[.,!?;:'’\"]",'',normalized_reply(value))
    value=re.sub(r'^(?:oh|ah|um|uh|hey|hi|hello|robot|well)\s+','',' '.join(value.split()))
    return re.sub(r'\s+(?:please|robot|sir|madam)$','',value)


def _contains_stop(text):
    text=normalized_reply(text)
    for phrases in WORDS.values():
        for phrase in phrases['stop']:
            query=normalized_reply(phrase)
            for match in re.finditer(re.escape(query),text):
                before=text[:match.start()]
                after=text[match.end():]
                def word(c): return c.isalnum() or unicodedata.category(c).startswith('M')
                if (not before or not word(before[-1])) and (not after or not word(after[0])):
                    return True
    return False


def short_reply(text,expect,language=None):
    if not isinstance(expect,(set,frozenset,list,tuple)) or not expect or set(expect)-KINDS:
        raise ValueError('Invalid expected replies')
    if language not in (None,*LANGUAGES): raise ValueError('Invalid reply language')
    if not valid_string(text,2000): return {'kind':'unclear','answer_english':None}
    if _contains_stop(text): return {'kind':'stop','answer_english':'Stop.'}
    value=normalized_reply(text)
    languages=LANGUAGES if language is None else (language,)
    kinds=set()
    for lang in set(languages)|{'en'}:
        for kind,phrases in WORDS[lang].items():
            if value in {normalized_reply(phrase) for phrase in phrases} or (lang=='en' and _loose(text) in {_loose(phrase) for phrase in phrases}):
                kinds.add(kind)
    if not kinds and (language in (None,'en')):
        # "No, it's not mine." / "No, she's not here." / "Yes, go ahead": the rest of the sentence decides;
        # a sentence that opens with "no" and says nothing else we know is still a no.
        opened=re.match(r'(no|nope|yes|yeah|yep)\s+(.+)',_loose(text))
        if opened:
            rest=opened[2]
            for kind,phrases in WORDS['en'].items():
                if kind!='stop' and rest in {_loose(phrase) for phrase in phrases}:kinds.add(kind)
            if opened[1] in ('no','nope'):
                kinds-={'yes','taken','done'}
                if not kinds:kinds.add('no')
    # English acknowledgement loanwords are context-sensitive, never substring matched.
    if value in ('ok','okay'):
        kinds.update(('yes','taken','done'))
    permitted=kinds & set(expect)
    # "Okay" where both a yes and a "done" would do is simply the acknowledgement that was asked for.
    if len(permitted)>1 and permitted<={'yes','done','taken'}:permitted={next(k for k in ('done','taken','yes') if k in permitted)}
    if len(permitted)==1:
        kind=next(iter(permitted))
        return {'kind':kind,'answer_english':ENGLISH[kind]}
    if 'answer' in expect and len(kinds & {'yes','no'})==1:
        kind=next(iter(kinds & {'yes','no'}))
        return {'kind':'answer','answer_english':ENGLISH[kind]}
    if 'location' in expect and not kinds:
        if value in ("i don't know","don't know","not sure",'தெரியாது','தெரியல','അറിയില്ല','తెలియదు'):
            return {'kind':'unclear','answer_english':None}
        return {'kind':'answer','answer_english':text if text.isascii() else None,'answer_original':text}
    if kinds or 'answer' not in expect:
        return {'kind':'unclear','answer_english':None}
    return None # free answer needs rewriting


def _hear_reply(text,expect,language=None,settings=None):
    reply=short_reply(text,expect,language)
    if reply is not None: return reply
    # Human answer text is evidence, never model input.
    return {'kind':'answer','answer_original':text,'answer_english':text if text.isascii() else None}


def hear_reply(text,expect,language=None):
    return _hear_reply(text,expect,language)
