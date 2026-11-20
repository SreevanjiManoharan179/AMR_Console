"""Fixed four-language speech. Every non-English entry needs native-speaker check.

Arbitrary questions/messages/answers are screen data, never spoken slots.
"""
import re
from .office_names import LANGUAGES,OfficeNames
from .office_schema import validate_plan,split_items
from .grounding import normalize

REVIEW_STATUS={lang:'checked' if lang=='en' else 'needs native-speaker check' for lang in LANGUAGES}
REQUIRED_EVENTS=('confirm_plan','queued','start_step','arrive_deliver','arrive_retrieve','arrive_check',
 'arrive_tell','reminder','got_reply','not_there','step_done','report','job_failed','returning_home','idle',
 'ask_which','unknown_name','cannot_do','stopped')

def four(en,ta,ml,te):
    return dict(zip(LANGUAGES,(en,ta,ml,te)))

TEMPLATES={
 'waiting_later':four('A step needs to be scheduled for later. I will wait for instructions.',
  'ஒரு வேலையை பிறகு செய்ய நேரம் நிர்ணயிக்க வேண்டும். வழிமுறைகளுக்காக காத்திருப்பேன்.',
  'ഒരു ഘട്ടം പിന്നീട് ചെയ്യാൻ സമയം നിശ്ചയിക്കണം. നിർദ്ദേശങ്ങൾക്കായി കാത്തിരിക്കും.',
  'ఒక దశను తర్వాత చేయడానికి సమయం నిర్ణయించాలి. సూచనల కోసం వేచి ఉంటాను.'),
 'model_unavailable':four('The local language service is unavailable. Please use the console\'s rule-based input.',
  'உள்ளூர் மொழிச் சேவை கிடைக்கவில்லை. கன்சோலின் விதிமுறை அடிப்படையிலான உள்ளீட்டைப் பயன்படுத்துங்கள்.',
  'പ്രാദേശിക ഭാഷാ സേവനം ലഭ്യമല്ല. കൺസോളിലെ നിയമാധിഷ്ഠിത ഇൻപുട്ട് ഉപയോഗിക്കൂ.',
  'స్థానిక భాషా సేవ అందుబాటులో లేదు. కన్సోల్‌లో నియమాల ఆధారిత ఇన్‌పుట్ ఉపయోగించండి.'),
 'model_timeout':four('The language check took too long. Please split or rephrase the job.',
  'மொழிச் சரிபார்ப்புக்கு அதிக நேரம் எடுத்தது. வேலையைப் பிரித்து அல்லது வேறு விதமாகச் சொல்லுங்கள்.',
  'ഭാഷാ പരിശോധനയ്ക്ക് കൂടുതൽ സമയമെടുത്തു. ജോലി വേർതിരിച്ചോ മറ്റൊരു രീതിയിലോ പറയൂ.',
  'భాషా తనిఖీకి ఎక్కువ సమయం పట్టింది. పనిని విడదీసి లేదా మరో విధంగా చెప్పండి.'),
 'confirm_plan':four('I will do {count} things: {steps}. Shall I start?',
  'நான் {count} வேலைகள் செய்வேன்: {steps}. தொடங்கலாமா?',
  'ഞാൻ {count} കാര്യങ്ങൾ ചെയ്യും: {steps}. തുടങ്ങട്ടെ?',
  'నేను {count} పనులు చేస్తాను: {steps}. మొదలుపెట్టనా?'),
 'queued':four("I'm doing a job for {current} now. Yours is number {position}, in about {minutes} minutes.",
  'இப்போது {current} அவர்களின் வேலையை செய்கிறேன். உங்கள் வேலை எண் {position}; சுமார் {minutes} நிமிடங்களில் தொடங்குவேன்.',
  'ഇപ്പോൾ {current} നൽകിയ ജോലിയിലാണ്. നിങ്ങളുടെ ജോലി {position}-ാം സ്ഥാനത്താണ്; ഏകദേശം {minutes} മിനിറ്റിനുള്ളിൽ തുടങ്ങും.',
  'ఇప్పుడు {current} పని చేస్తున్నాను. మీ పని వరుసలో {position}; సుమారు {minutes} నిమిషాల్లో మొదలుపెడతాను.'),
 'start_step':four('Starting step {n}: {description}.','வேலை {n} தொடங்குகிறேன்: {description}.',
  'ഘട്ടം {n} തുടങ്ങുന്നു: {description}.','దశ {n} మొదలుపెడుతున్నాను: {description}.'),
 'arrive_deliver':four("Hello, I have {item} for {recipient} from {requester}. Please take it and say 'taken'.",
  "வணக்கம், {requester} அனுப்பிய {item} {recipient} அவர்களுக்காக கொண்டு வந்திருக்கிறேன். எடுத்துக்கொண்டு 'எடுத்துக்கிட்டேன்' என்று சொல்லுங்கள்.",
  "നമസ്കാരം, {requester} അയച്ച {item} {recipient}ക്ക് കൊണ്ടുവന്നിട്ടുണ്ട്. എടുത്തിട്ട് 'എടുത്തു' എന്ന് പറയൂ.",
  "నమస్కారం, {requester} పంపిన {item} {recipient} కోసం తెచ్చాను. తీసుకుని 'తీసుకున్నాను' అని చెప్పండి."),
 'arrive_retrieve':four("Hello, {requester} needs {item}. Please put it on me and say 'done'.",
  "வணக்கம், {requester} அவர்களுக்கு {item} தேவை. அதை என் மேல் வைத்து 'முடிந்தது' என்று சொல்லுங்கள்.",
  "നമസ്കാരം, {requester}ക്ക് {item} വേണം. അത് എന്റെ മുകളിൽ വെച്ചിട്ട് 'കഴിഞ്ഞു' എന്ന് പറയൂ.",
  "నమస్కారం, {requester}కి {item} కావాలి. దాన్ని నా మీద పెట్టి 'అయిపోయింది' అని చెప్పండి."),
 'arrive_check':four('Hello, I have a question from {requester}. Please read it on screen and give your answer.',
  'வணக்கம், {requester} அவர்களிடமிருந்து ஒரு கேள்வி. திரையில் படித்துவிட்டு பதில் சொல்லுங்கள்.',
  'നമസ്കാരം, {requester}ക്ക് ഒരു ചോദ്യമുണ്ട്. സ്ക്രീനിൽ വായിച്ചിട്ട് മറുപടി പറയൂ.',
  'నమస్కారం, {requester} నుంచి ఒక ప్రశ్న ఉంది. తెరపై చదివి సమాధానం చెప్పండి.'),
 'arrive_tell':four('{recipient}, I have a message from {requester}. Please read it on screen.',
  '{recipient}, {requester} அனுப்பிய செய்தி இருக்கிறது. திரையில் படியுங்கள்.',
  '{recipient}, {requester} അയച്ച സന്ദേശമുണ്ട്. സ്ക്രീനിൽ വായിക്കൂ.',
  '{recipient}, {requester} పంపిన సందేశం ఉంది. తెరపై చదవండి.'),
 'reminder':four("{recipient}, I'm still here with {item}. Please reply.",
  '{recipient}, {item} உடன் இங்கே காத்திருக்கிறேன். பதில் சொல்லுங்கள்.',
  '{recipient}, {item}യുമായി ഇവിടെ കാത്തിരിക്കുകയാണ്. മറുപടി പറയൂ.',
  '{recipient}, {item}తో ఇక్కడ వేచి ఉన్నాను. సమాధానం చెప్పండి.'),
 'got_reply':four('Thank you.','நன்றி.','നന്ദി.','ధన్యవాదాలు.'),
 'not_there':four('{recipient} is not here. {policy}.','{recipient} இங்கே இல்லை. {policy}.',
  '{recipient} ഇവിടെ ഇല്ല. {policy}.','{recipient} ఇక్కడ లేరు. {policy}.'),
 'step_done':four('Step completed.','இந்த வேலை முடிந்தது.','ഈ ഘട്ടം പൂർത്തിയായി.','ఈ దశ పూర్తయింది.'),
 'report':four('Job report: {summary}. Any answers or messages are shown on screen.',
  'வேலை அறிக்கை: {summary}. பதில்களும் செய்திகளும் திரையில் இருக்கின்றன.',
  'ജോലി റിപ്പോർട്ട്: {summary}. മറുപടികളും സന്ദേശങ്ങളും സ്ക്രീനിൽ കാണാം.',
  'పని నివేదిక: {summary}. సమాధానాలు, సందేశాలు తెరపై ఉన్నాయి.'),
 'job_failed':four("I couldn't finish: {reason}. Any carried item remains with me.",
  'வேலையை முடிக்க முடியவில்லை: {reason}. எடுத்துச் சென்ற பொருள் என்னிடமே இருக்கிறது.',
  'ജോലി പൂർത്തിയാക്കാനായില്ല: {reason}. കൊണ്ടുപോയ സാധനം എന്റെ പക്കലുണ്ട്.',
  'పని పూర్తి కాలేదు: {reason}. తీసుకెళ్లిన వస్తువు నా దగ్గరే ఉంది.'),
 'returning_home':four('No more jobs. Going back to the dock.','வேறு வேலை இல்லை. டாக்கிற்குத் திரும்புகிறேன்.',
  'ഇനി ജോലികളില്ല. ഡോക്കിലേക്ക് മടങ്ങുന്നു.','ఇంకా పనులు లేవు. డాక్‌కి తిరిగి వెళ్తున్నాను.'),
 'idle':four("I'm at the dock and free for a new job.",'டாக்கில் இருக்கிறேன். புதிய வேலைக்கு தயாராக இருக்கிறேன்.',
  'ഞാൻ ഡോക്കിലാണ്. പുതിയ ജോലിക്ക് തയ്യാറാണ്.','నేను డాక్ దగ్గర ఉన్నాను. కొత్త పనికి సిద్ధంగా ఉన్నాను.'),
 'ask_which':four('Which one: {options}?','எது: {options}?','ഏതാണ്: {options}?','ఏది: {options}?'),
 'unknown_place':four("That place is not on my map. Which mapped place should I go to?",
  'அந்த இடம் என் வரைபடத்தில் இல்லை. வரைபடத்தில் உள்ள எந்த இடத்துக்குப் போக வேண்டும்?',
  'ആ സ്ഥലം എന്റെ മാപ്പിലില്ല. മാപ്പിലുള്ള ഏത് സ്ഥലത്തേക്ക് പോകണം?',
  'ఆ ప్రదేశం నా మ్యాప్‌లో లేదు. మ్యాప్‌లోని ఏ ప్రదేశానికి వెళ్లాలి?'),
 'unknown_name':four("I don't know that name. Please use an approved name or explain who you mean.",
  'அந்தப் பெயர் தெரியவில்லை. அங்கீகரிக்கப்பட்ட பெயரைச் சொல்லுங்கள் அல்லது யாரைக் குறிக்கிறீர்கள் என்று விளக்குங்கள்.',
  'ആ പേര് അറിയില്ല. അംഗീകരിച്ച പേര് പറയൂ, അല്ലെങ്കിൽ ആരെയാണ് ഉദ്ദേശിക്കുന്നതെന്ന് വ്യക്തമാക്കൂ.',
  'ఆ పేరు తెలియదు. ఆమోదించిన పేరు చెప్పండి లేదా ఎవరిని ఉద్దేశించారో వివరించండి.'),
 'cannot_do':four('I can deliver, retrieve items, check a place or carry a message. I cannot do that task.',
  'பொருள் கொடுக்கவும் வாங்கி வரவும், இடத்தில் விசாரிக்கவும், செய்தி சொல்லவும் முடியும். அந்த வேலையை செய்ய முடியாது.',
  'സാധനം എത്തിക്കാം, വാങ്ങിവരാം, ഒരു സ്ഥലത്ത് അന്വേഷിക്കാം, സന്ദേശം നൽകാം. ആ ജോലി ചെയ്യാനാകില്ല.',
  'వస్తువులు అందించగలను, తీసుకురాగలను, ఒక చోట విచారించగలను, సందేశం చెప్పగలను. ఆ పని చేయలేను.'),
 'stopped':four("Stopped. I'll wait for instructions.",'நிறுத்திவிட்டேன். வழிமுறைகளுக்காக காத்திருக்கிறேன்.',
  'നിർത്തി. നിർദ്ദേശങ്ങൾക്കായി കാത്തിരിക്കും.','ఆగిపోయాను. సూచనల కోసం వేచి ఉంటాను.'),
 'split_job':four('Please split this into jobs of at most six steps.','இதை அதிகபட்சம் ஆறு படிகள் உள்ள வேலைகளாகப் பிரித்துச் சொல்லுங்கள்.',
  'പരമാവധി ആറ് ഘട്ടങ്ങളുള്ള ജോലികളായി ഇത് വേർതിരിക്കൂ.','దీన్ని గరిష్ఠంగా ఆరు దశలున్న పనులుగా విడదీయండి.'),
 'clarify_request':four('Please repeat the recipient, place or item for this job.',
  'வேலையை வேறு விதமாகச் சொல்லுங்கள். பெறுபவர், இடம் அல்லது பொருளை மீண்டும் சொல்லுங்கள்.',
  'ജോലിയുടെ സ്വീകർത്താവ്, സ്ഥലം അല്ലെങ്കിൽ സാധനം വീണ്ടും പറയൂ.',
  'ఈ పని కోసం గ్రహీత, స్థలం లేదా వస్తువును మళ్లీ చెప్పండి.'),
 'reply_prompt':four('Please give one of the expected replies.','எதிர்பார்க்கப்படும் பதில்களில் ஒன்றைச் சொல்லுங்கள்.',
  'പ്രതീക്ഷിക്കുന്ന മറുപടികളിൽ ഒന്ന് പറയൂ.','ఎదురుచూస్తున్న సమాధానాల్లో ఒకటి చెప్పండి.'),
 'return_to_requester':four('Returning {item} to {recipient}.','{item} எடுத்துக்கொண்டு {recipient} அவர்களிடம் திரும்புகிறேன்.',
  '{item}യുമായി {recipient}യുടെ അടുത്തേക്ക് മടങ്ങുന്നു.','{item}తో {recipient} దగ్గరకు తిరిగి వస్తున్నాను.'),
}

MAP_EVENTS=('place_not_on_map','place_unreachable','ask_where_person','ask_people_where','person_seen_elsewhere','map_request_logged')
REQUIRED_EVENTS=(*REQUIRED_EVENTS,*MAP_EVENTS)
TEMPLATES.update({
 'map_request_failed':four('I could not save the map request. Please ask the owner to review it; choose a mapped place or cancel.', 'வரைபட கோரிக்கையை பதிவு செய்ய முடியவில்லை. உரிமையாளரை அணுகவும்; வரைபடத்தில் உள்ள இடத்தைத் தேர்ந்தெடுக்கவும் அல்லது ரத்து செய்யவும்.', 'മാപ്പ് അപേക്ഷ രേഖപ്പെടുത്താനായില്ല. ഉടമയെ സമീപിക്കുക; മാപ്പിലുള്ള സ്ഥലം തിരഞ്ഞെടുക്കുക അല്ലെങ്കിൽ റദ്ദാക്കുക.', 'మ్యాప్ అభ్యర్థనను నమోదు చేయలేకపోయాను. యజమానిని సంప్రదించండి; మ్యాప్‌లోని స్థలం ఎంచుకోండి లేదా రద్దు చేయండి.'),
 'place_not_on_map':four('{place} is not on my map yet. I have logged a request for owner review. Choose {alternative}, or cancel.',
  '{place} இன்னும் என் வரைபடத்தில் இல்லை. உரிமையாளர் பார்வைக்காக கோரிக்கை பதிவு செய்தேன். {alternative} என்பதைத் தேர்ந்தெடுக்கவும் அல்லது ரத்து செய்யவும்.',
  '{place} ഇതുവരെ എന്റെ മാപ്പിലില്ല. ഉടമയുടെ പരിശോധനയ്ക്ക് അപേക്ഷ രേഖപ്പെടുത്തി. {alternative} തിരഞ്ഞെടുക്കുക, അല്ലെങ്കിൽ റദ്ദാക്കുക.',
  '{place} ఇంకా నా మ్యాప్‌లో లేదు. యజమాని సమీక్ష కోసం అభ్యర్థన నమోదు చేశాను. {alternative} ఎంచుకోండి లేదా రద్దు చేయండి.'),
 'place_unreachable':four('I cannot confirm a route to {place}: {reason}. Try later, choose another mapped place, or cancel.',
  '{place} செல்லும் வழியை உறுதி செய்ய முடியவில்லை: {reason}. பிறகு முயற்சிக்கலாம், வேறு வரைபட இடத்தைத் தேர்ந்தெடுக்கலாம் அல்லது ரத்து செய்யலாம்.',
  '{place} എത്താനുള്ള വഴി ഉറപ്പാക്കാനാവില്ല: {reason}. പിന്നീട് ശ്രമിക്കാം, മാപ്പിലുള്ള മറ്റൊരു സ്ഥലം തിരഞ്ഞെടുക്കാം, അല്ലെങ്കിൽ റദ്ദാക്കാം.',
  '{place} చేరే మార్గాన్ని నిర్ధారించలేను: {reason}. తర్వాత ప్రయత్నించండి, మ్యాప్‌లోని మరో స్థలం ఎంచుకోండి లేదా రద్దు చేయండి.'),
 'ask_where_person':four('Where is {recipient} usually located? Choose a mapped place: {options}.',
  '{recipient} வழக்கமாக எங்கே இருப்பார்? வரைபடத்தில் உள்ள இடத்தைத் தேர்ந்தெடுக்கவும்: {options}.',
  '{recipient} സാധാരണ എവിടെയാണ്? മാപ്പിലുള്ള സ്ഥലം തിരഞ്ഞെടുക്കുക: {options}.',
  '{recipient} సాధారణంగా ఎక్కడ ఉంటారు? మ్యాప్‌లోని స్థలం ఎంచుకోండి: {options}.'),
 'ask_people_where':four('Hello, do you know where {recipient} is?',
  'வணக்கம், {recipient} எங்கே இருக்கிறார் என்று தெரியுமா?',
  'നമസ്കാരം, {recipient} എവിടെയാണെന്ന് അറിയാമോ?',
  'నమస్కారం, {recipient} ఎక్కడ ఉన్నారో తెలుసా?'),
 'person_seen_elsewhere':four('They say {recipient} is at {place}. Shall I continue this step there? Please answer yes or no.',
  '{recipient} {place} இடத்தில் இருப்பதாகச் சொல்கிறார்கள். இந்த வேலையை அங்கே தொடரலாமா? ஆம் அல்லது இல்லை என்று சொல்லுங்கள்.',
  '{recipient} {place} സ്ഥലത്താണെന്ന് പറയുന്നു. ഈ ഘട്ടം അവിടെ തുടരട്ടേ? അതെ അല്ലെങ്കിൽ അല്ല എന്ന് പറയൂ.',
  '{recipient} {place} దగ్గర ఉన్నారని చెబుతున్నారు. ఈ దశను అక్కడ కొనసాగించాలా? అవును లేదా కాదు అని చెప్పండి.'),
 'map_request_logged':four('Noted. I have logged a map or person-location update request for owner review.',
  'குறித்துக்கொண்டேன். வரைபடம் அல்லது நபரின் இடத்தை புதுப்பிக்க உரிமையாளர் பார்வைக்காக கோரிக்கை பதிவு செய்தேன்.',
  'രേഖപ്പെടുത്തി. മാപ്പോ വ്യക്തിയുടെ സ്ഥലമോ പുതുക്കാൻ ഉടമയുടെ പരിശോധനയ്ക്ക് അപേക്ഷ നൽകി.',
  'నమోదు చేశాను. మ్యాప్ లేదా వ్యక్తి స్థానాన్ని నవీకరించడానికి యజమాని సమీక్ష కోసం అభ్యర్థన నమోదు చేశాను.'),
})
MAP_ALTERNATIVE=four('another mapped place','வேறு வரைபட இடம்','മാപ്പിലുള്ള മറ്റൊരു സ്ഥലം','మ్యాప్‌లోని మరో స్థలం')
MAP_REASONS={
 'blocked':four('the path is blocked','வழி தடுக்கப்பட்டுள்ளது','വഴി തടസ്സപ്പെട്ടിരിക്കുന്നു','దారి మూసివేయబడింది'),
 'no_route':four('no route is available','வழி கிடைக்கவில்லை','വഴി ലഭ്യമല്ല','మార్గం అందుబాటులో లేదు'),
 'closed':four('the place is closed','இடம் மூடப்பட்டுள்ளது','സ്ഥലം അടച്ചിരിക്കുന്നു','స్థలం మూసివేయబడింది'),
 'not_verified':four('reachability has not been verified','செல்ல முடியும் என்பது சரிபார்க்கப்படவில்லை','എത്താനാകുമോ എന്ന് പരിശോധിച്ചിട്ടില്ല','చేరగలమో నిర్ధారించలేదు'),
 'unknown':four('the route is unavailable','வழி கிடைக்கவில்லை','വഴി ലഭ്യമല്ല','మార్గం అందుబాటులో లేదు'),
}

PHRASES={
 'deliver':four('give {item} to {to}','{item} {to} அவர்களிடம் கொடுப்பது','{item} {to}ക്ക് കൊടുക്കുക','{item} {to}కి ఇవ్వడం'),
 'retrieve':four('get {item} from {origin} and bring it to {to}','{origin} அவர்களிடமிருந்து {item} வாங்கி {to} அவர்களிடம் கொடுப்பது',
  '{origin}യിൽനിന്ന് {item} വാങ്ങി {to}ക്ക് കൊണ്ടുവരുക','{origin} దగ్గర {item} తీసుకుని {to}కి ఇవ్వడం'),
 'check':four('go to {to} and ask your question','{to} சென்று உங்கள் கேள்வியைக் கேட்பது','{to}യിൽ പോയി നിങ്ങളുടെ ചോദ്യം ചോദിക്കുക','{to} దగ్గరకు వెళ్లి మీ ప్రశ్న అడగడం'),
 'tell':four('go to {to} and carry your message','{to} அவர்களிடம் சென்று உங்கள் செய்தியைச் சொல்வது','{to}ക്ക് നിങ്ങളുടെ സന്ദേശം നൽകുക','{to} దగ్గరకు వెళ్లి మీ సందేశం చెప్పడం'),
 'go':four('go to {to}','{to} செல்வது','{to}യിലേക്ക് പോകുക','{to} దగ్గరకు వెళ్లడం'),
 'leave_with':four('If they are away, leave it with {to}','அவர்கள் இல்லையென்றால் {to} அவர்களிடம் கொடுப்பேன்',
  'അവർ ഇല്ലെങ്കിൽ {to}ക്ക് കൊടുക്കും','వారు లేకపోతే {to}కి ఇస్తాను'),
 'bring_back':four('bring it back to the requester','கோரியவரிடம் திரும்பக் கொண்டுவருவேன்','ആവശ്യപ്പെട്ടയാളിന് തിരികെ കൊണ്ടുവരും','అడిగిన వ్యక్తికి తిరిగి తీసుకువస్తాను'),
 'try_later':four('try again in {minutes} minutes','{minutes} நிமிடங்களில் மீண்டும் முயற்சி செய்வேன்','{minutes} മിനിറ്റിനുശേഷം വീണ്ടും ശ്രമിക്കും','{minutes} నిమిషాల తర్వాత మళ్లీ ప్రయత్నిస్తాను'),
 'ask_anyone':four('ask someone else there','அங்கிருக்கும் வேறொருவரிடம் கேட்பேன்','അവിടെയുള്ള മറ്റൊരാളോട് ചോദിക്കും','అక్కడ మరొకరిని అడుగుతాను'),
 'otherwise':four('I will do what I was asked to do in this case','இந்த நிலைக்குச் சொல்லப்பட்ட வேலையைச் செய்கிறேன்','ഈ സാഹചര്യത്തിനായി പറഞ്ഞ ജോലി ചെയ്യും','ఈ సందర్భానికి చెప్పిన పని చేస్తాను'),
 'otherwise_branch':four('if not there: {steps}','அங்கே இல்லையென்றால்: {steps}','അവിടെ ഇല്ലെങ്കിൽ: {steps}','అక్కడ లేకపోతే: {steps}'),
 'skip':four('skip this step','இந்த வேலையைத் தவிர்ப்பேன்','ഈ ഘട്ടം ഒഴിവാക്കും','ఈ దశను దాటవేస్తాను'),
 'yes_branch':four('if the answer is yes: {steps}','பதில் ஆம் என்றால்: {steps}','മറുപടി അതെ ആണെങ്കിൽ: {steps}','సమాధానం అవును అయితే: {steps}'),
 'no_branch':four('if the answer is no: {steps}','பதில் இல்லை என்றால்: {steps}','മറുപടി അല്ല ആണെങ്കിൽ: {steps}','సమాధానం కాదు అయితే: {steps}'),
 'delivered':four('delivered {item} to {to}','{item} {to} அவர்களிடம் கொடுத்தேன்','{item} {to}ക്ക് കൊടുത്തു','{item} {to}కి ఇచ్చాను'),
 'retrieved':four('brought {item} to {to}','{item} {to} அவர்களிடம் கொண்டு வந்தேன்','{item} {to}ക്ക് കൊണ്ടുവന്നു','{item} {to}కి తీసుకువచ్చాను'),
 'checked':four('checked at {to}; the answer is on screen','{to} விசாரித்தேன்; பதில் திரையில் உள்ளது','{to}യിൽ അന്വേഷിച്ചു; മറുപടി സ്ക്രീനിലുണ്ട്','{to} దగ్గర విచారించాను; సమాధానం తెరపై ఉంది'),
 'told':four('carried the message to {to}','{to} அவர்களிடம் செய்தியைக் கொடுத்தேன்','{to}ക്ക് സന്ദേശം നൽകി','{to}కి సందేశం అందించాను'),
 'went':four('went to {to}','{to} சென்றேன்','{to}യിൽ പോയി','{to} దగ్గరకు వెళ్లాను'),
 'deferred':four('deferred the step','வேலையை ஒத்திவைத்தேன்','ഘട്ടം മാറ്റിവച്ചു','దశను వాయిదా వేశాను'),
 'skipped':four('skipped the step','வேலையைத் தவிர்த்தேன்','ഘട്ടം ഒഴിവാക്കി','దశను దాటవేశాను'),
 'failed':four('a step could not be completed','ஒரு வேலையை முடிக்க முடியவில்லை','ഒരു ഘട്ടം പൂർത്തിയായില്ല','ఒక దశ పూర్తికాలేదు'),
 'empty_report':four('no steps completed','எந்த வேலையும் முடிக்கவில்லை','ഘട്ടങ്ങളൊന്നും പൂർത്തിയായില്ല','ఏ దశా పూర్తికాలేదు'),
}

# Task 4b fixed slot questions; arbitrary payload alternatives stay on screen.
TEMPLATES.update({
 'confirm_collect':four('Shall I collect {item} from {recipient}?',
  '{recipient} அவர்களிடமிருந்து {item} வாங்கி வரட்டுமா?',
  '{recipient}യുടെ പക്കൽ നിന്ന് {item} കൊണ്ടുവരട്ടേ?',
  '{recipient} దగ్గర నుంచి {item} తీసుకురావాలా?'),
 'clarify_person':four('Which person: {options}?','யாரிடம்: {options}?','ആരാണ്: {options}?','ఎవరు: {options}?'),
 'clarify_place':four('Which place: {options}?','எந்த இடம்: {options}?','ഏത് സ്ഥലം: {options}?','ఏ ప్రదేశం: {options}?'),
 'clarify_item':four('Which item: {options}?','எந்தப் பொருள்: {options}?','ഏത് സാധനം: {options}?','ఏ వస్తువు: {options}?'),
 'clarify_absence':four('If {recipient} is away, shall I use this absence instruction: {policy}?',
  '{recipient} அங்கு இல்லை என்றால், இதைச் செய்யட்டுமா: {policy}?',
  '{recipient} അവിടെ ഇല്ലെങ്കിൽ, ഇങ്ങനെ ചെയ്യട്ടേ: {policy}?',
  '{recipient} అక్కడ లేకపోతే, ఇలా చేయాలా: {policy}?'),
 'clarify_message':four('Which message should I take? Please choose on screen.',
  'எந்தச் செய்தியை எடுத்துச் செல்ல வேண்டும்? திரையில் தேர்வு செய்யுங்கள்.',
  'ഏത് സന്ദേശം കൊണ്ടുപോകണം? സ്ക്രീനിൽ തിരഞ്ഞെടുക്കൂ.',
  'ఏ సందేశం తీసుకెళ్లాలి? తెరపై ఎంచుకోండి.'),
 'clarify_question':four('Which question should I ask? Please choose on screen.',
  'எந்தக் கேள்வியைக் கேட்க வேண்டும்? திரையில் தேர்வு செய்யுங்கள்.',
  'ഏത് ചോദ്യം ചോദിക്കണം? സ്ക്രീനിൽ തിരഞ്ഞെടുക്കൂ.',
  'ఏ ప్రశ్న అడగాలి? తెరపై ఎంచుకోండి.'),
 'clarify_action':four('Which task should I do? Please choose on screen.',
  'எந்த வேலையைச் செய்ய வேண்டும்? திரையில் தேர்வு செய்யுங்கள்.',
  'ഏത് ജോലി ചെയ്യണം? സ്ക്രീനിൽ തിരഞ്ഞെടുക്കൂ.',
  'ఏ పని చేయాలి? తెరపై ఎంచుకోండి.'),
})
CLARIFY_EVENTS=('confirm_collect','clarify_person','clarify_place','clarify_item','clarify_absence','clarify_message','clarify_question','clarify_action')

ITEMS={
 'cup':four('the cup','கப்பை','കപ്പ്','కప్పు'),
 'file':four('the file','கோப்பை','ഫയൽ','ఫైల్'),
 'folder':four('the folder','கோப்புறையை','ഫോൾഡർ','ఫోల్డర్'),
 'parcel':four('the parcel','பொதியை','പാഴ്സൽ','పార్సెల్'),
 'charger':four('the charger','சார்ஜரை','ചാർജർ','చార్జర్'),
 'letter':four('the letter','கடிதத்தை','കത്ത്','లేఖ'),
 'document':four('the document','ஆவணத்தை','രേഖ','పత్రం'),
 'item':four('the item','பொருளை','സാധനം','వస్తువు'),
}
REASONS={
 'no_reply':four('no reply was received','பதில் கிடைக்கவில்லை','മറുപടി ലഭിച്ചില്ല','సమాధానం రాలేదు'),
 'not_here':four('the recipient was not present','பெறுபவர் அங்கு இல்லை','സ്വീകർത്താവ് അവിടെ ഉണ്ടായിരുന്നില്ല','గ్రహీత అక్కడ లేరు'),
 'stopped':four('the job was stopped','வேலை நிறுத்தப்பட்டது','ജോലി നിർത്തി','పని ఆపబడింది'),
 'failed':four('the step failed','வேலை தோல்வியடைந்தது','ഘട്ടം പരാജയപ്പെട്ടു','దశ విఫలమైంది'),
 'unreachable':four('I could not reach that stop','அந்த இடத்தை அடைய முடியவில்லை','ആ സ്ഥലത്ത് എത്താൻ കഴിഞ്ഞില്ല','ఆ ప్రదేశానికి చేరుకోలేకపోయాను'),
}


PHRASES.update({
 'helper_delivered':four('a helper received {item} for {to}','{to} அவர்களுக்காக வேறொருவர் {item} பெற்றார்','{to}ക്കായി മറ്റൊരാൾ {item} സ്വീകരിച്ചു','{to} కోసం మరొకరు {item} తీసుకున్నారు'),
 'helper_retrieved':four('a helper supplied {item}; brought it to {to}','வேறொருவர் {item} கொடுத்தார்; {to} அவர்களிடம் கொண்டு வந்தேன்','മറ്റൊരാൾ {item} നൽകി; {to}ക്ക് കൊണ്ടുവന്നു','మరొకరు {item} ఇచ్చారు; {to}కి తెచ్చాను'),
 'helper_checked':four('someone else answered for {to}; exact answer and helper details are on screen','{to} அவர்களுக்காக வேறொருவர் பதில் அளித்தார்; விவரங்கள் திரையில் உள்ளன','{to}ക്കായി മറ്റൊരാൾ മറുപടി നൽകി; വിവരങ്ങൾ സ്ക്രീനിലുണ്ട്','{to} తరఫున మరొకరు సమాధానం ఇచ్చారు; వివరాలు తెరపై ఉన్నాయి'),
})

# Fixed task 4c vocabulary. Native templates remain pending owner review.
LAB_ITEMS=('charger','laptop charger','encoder','motor','motor driver','servo','sensor','lidar','camera','IMU','Raspberry Pi','Arduino','ESP32','dev board','USB cable','jumper wires','battery pack','multimeter','screwdriver set','3D-printed part','documents','file','printer')
for term in LAB_ITEMS:
    if term!='file':ITEMS[normalize(term)]=four(term,term,term,term)
OK_QUESTION=four('Is that OK?','இது சரியா?','ഇത് ശരിയാണോ?','ఇది సరేనా?')
TEMPLATES.update({
 'pickup_check':four('Is it light, and does it fit in my tray? Say yes or no.','இது எடை குறைவாகவும் என் தட்டில் பொருந்துவதாகவும் உள்ளதா? ஆம் அல்லது இல்லை சொல்லுங்கள்.','ഇത് ഭാരം കുറഞ്ഞതും എന്റെ ട്രേയിൽ ഒതുങ്ങുന്നതുമാണോ? അതെ അല്ലെങ്കിൽ അല്ല എന്ന് പറയൂ.','ఇది తేలికగా ఉండి నా ట్రేలో సరిపోతుందా? అవును లేదా కాదు చెప్పండి.'),
 'bag_check':four('Is the battery pack safely inside a bag? Say yes or no.','battery pack பாதுகாப்பாக பையில் உள்ளதா? ஆம் அல்லது இல்லை சொல்லுங்கள்.','battery pack സുരക്ഷിതമായി ബാഗിലാണോ? അതെ അല്ലെങ്കിൽ അല്ല എന്ന് പറയൂ.','battery pack సురక్షితంగా సంచిలో ఉందా? అవును లేదా కాదు చెప్పండి.'),
 'verify_answer':four('Your exact answer is on screen. Is that right?','உங்கள் பதில் அப்படியே திரையில் உள்ளது. இது சரியா?','നിങ്ങളുടെ അതേ മറുപടി സ്ക്രീനിലുണ്ട്. ശരിയാണോ?','మీ అసలు సమాధానం తెరపై ఉంది. అది సరైనదేనా?'),
 'correct_answer':four('Please give the corrected answer.','திருத்திய பதிலைச் சொல்லுங்கள்.','തിരുത്തിയ മറുപടി പറയൂ.','సరిచేసిన సమాధానం చెప్పండి.'),
 'received_answer':four('I received an answer. The exact answer is on screen.','பதில் கிடைத்தது. பதில் அப்படியே திரையில் உள்ளது.','മറുപടി ലഭിച്ചു. അതേ മറുപടി സ്ക്രീനിലുണ്ട്.','సమాధానం వచ్చింది. అసలు సమాధానం తెరపై ఉంది.'),
 'queue_parts':four('I can do this part now and queue the rest. Each part needs your yes.','இந்தப் பகுதியை இப்போது செய்து மீதியை வரிசையில் வைப்பேன். ஒவ்வொரு பகுதிக்கும் உங்கள் ஆம் தேவை.','ഈ ഭാഗം ഇപ്പോൾ ചെയ്ത് ബാക്കി വരിയിൽ വയ്ക്കാം. ഓരോ ഭാഗത്തിനും നിങ്ങളുടെ സമ്മതം വേണം.','ఈ భాగం ఇప్పుడు చేసి మిగతాది వరుసలో ఉంచుతాను. ప్రతి భాగానికి మీ అవును అవసరం.'),
 'omissions':four('I cannot do the unsupported parts shown on screen; I can do the rest.','திரையில் காட்டிய ஆதரிக்கப்படாத பகுதிகளைச் செய்ய முடியாது; மீதியைச் செய்ய முடியும்.','സ്ക്രീനിലെ പിന്തുണയില്ലാത്ത ഭാഗങ്ങൾ ചെയ്യാനാവില്ല; ബാക്കി ചെയ്യാം.','తెరపై చూపిన చేయలేని భాగాలు వదిలి మిగతావి చేయగలను.'),
 'new_person':four('Does this person exist? Please confirm, then select their current mapped place on screen.','இந்த நபர் இருக்கிறாரா? உறுதி செய்து தற்போதைய வரைபட இடத்தைத் திரையில் தேர்வு செய்யுங்கள்.','ഈ വ്യക്തി ഉണ്ടോ? ഉറപ്പാക്കി ഇപ്പോഴത്തെ മാപ്പ് സ്ഥലം തിരഞ്ഞെടുക്കൂ.','ఈ వ్యక్తి ఉన్నారా? నిర్ధారించి ప్రస్తుత మ్యాప్ స్థలాన్ని ఎంచుకోండి.'),
 'person_present':four('Hello, is the named person shown on screen here?','வணக்கம், திரையில் உள்ள நபர் இங்கே இருக்கிறாரா?','നമസ്കാരം, സ്ക്രീനിലെ വ്യക്തി ഇവിടെയുണ്ടോ?','నమస్కారం, తెరపై ఉన్న వ్యక్తి ఇక్కడ ఉన్నారా?'),
 'helper_retrieve':four('Can someone help? The requester may have left the item shown on screen here. Please put it in my tray and say done or not here.','யாராவது உதவ முடியுமா? திரையிலுள்ள பொருளை கோரியவர் இங்கே விட்டிருக்கலாம். தட்டில் வைத்து முடிந்தது அல்லது இங்கில்லை சொல்லுங்கள்.','ആർക്കെങ്കിലും സഹായിക്കാമോ? സ്ക്രീനിലെ സാധനം ഇവിടെ വച്ചിരിക്കാം. ട്രേയിൽ വച്ച് കഴിഞ്ഞു അല്ലെങ്കിൽ ഇവിടെയില്ല എന്ന് പറയൂ.','ఎవరైనా సహాయం చేయగలరా? తెరపై వస్తువు ఇక్కడ వదిలి ఉండవచ్చు. ట్రేలో పెట్టి అయిపోయింది లేదా ఇక్కడ లేదు చెప్పండి.'),
 'helper_deliver':four('Can someone take this item for the named person shown on screen? Say taken.','திரையில் உள்ள நபருக்காக இந்தப் பொருளை யாராவது பெற முடியுமா? எடுத்துக்கிட்டேன் சொல்லுங்கள்.','സ്ക്രീനിലെ വ്യക്തിക്കായി ഈ സാധനം ആരെങ്കിലും സ്വീകരിക്കാമോ? എടുത്തു എന്ന് പറയൂ.','తెరపై ఉన్న వ్యక్తి కోసం ఈ వస్తువు ఎవరైనా తీసుకోగలరా? తీసుకున్నాను చెప్పండి.'),
 'helper_check':four('Can someone answer for the named person shown on screen?','திரையில் உள்ள நபருக்காக யாராவது பதில் சொல்ல முடியுமா?','സ്ക്രീനിലെ വ്യക്തിക്കായി ആർക്കെങ്കിലും മറുപടി പറയാമോ?','తెరపై ఉన్న వ్యక్తి తరఫున ఎవరైనా సమాధానం చెప్పగలరా?'),
 'verify_item':four('Is it the item shown on screen? Say yes or no.','இது திரையில் காட்டிய பொருளா? ஆம் அல்லது இல்லை சொல்லுங்கள்.','ഇത് സ്ക്രീനിലെ സാധനമാണോ? അതെ അല്ലെങ്കിൽ അല്ല എന്ന് പറയൂ.','ఇది తెరపై ఉన్న వస్తువేనా? అవును లేదా కాదు చెప్పండి.'),
 'helper_report':four('A helper assisted; their details are on screen.','வேறொருவர் உதவினார்; விவரங்கள் திரையில் உள்ளன.','മറ്റൊരാൾ സഹായിച്ചു; വിവരങ്ങൾ സ്ക്രീനിലുണ്ട്.','మరొకరు సహాయం చేశారు; వివరాలు తెరపై ఉన్నాయి.'),
 'guess_location':four('The requester may have left this item here.','கோரியவர் இந்தப் பொருளை இங்கே விட்டிருக்கலாம்.','ആവശ്യപ്പെട്ടയാൾ ഈ സാധനം ഇവിടെ വച്ചിരിക്കാം.','అడిగినవారు ఈ వస్తువు ఇక్కడ వదిలి ఉండవచ్చు.'),
 'not_found':four('The item was not found at the place shown on screen.','திரையில் காட்டிய இடத்தில் பொருள் கிடைக்கவில்லை.','സ്ക്രീനിലെ സ്ഥലത്ത് സാധനം കണ്ടെത്തിയില്ല.','తెరపై చూపిన చోట వస్తువు దొరకలేదు.'),
})
CAPABILITY_REASONS={
 'too_heavy':four('the item is too heavy','பொருள் அதிக எடை கொண்டது','സാധനത്തിന് ഭാരം കൂടുതലാണ്','వస్తువు చాలా బరువుగా ఉంది'),
 'does_not_fit':four('the item does not fit the tray','பொருள் தட்டில் பொருந்தாது','സാധനം ട്രേയിൽ ഒതുങ്ങില്ല','వస్తువు ట్రేలో సరిపోదు'),
 'item_not_allowed':four('this item is not allowed','இந்தப் பொருளுக்கு அனுமதி இல்லை','ഈ സാധനം അനുവദനീയമല്ല','ఈ వస్తువు అనుమతించబడదు'),
 'capability_unset':four('the owner must configure the tray limits first','உரிமையாளர் முதலில் தட்டின் வரம்புகளை அமைக்க வேண்டும்','ഉടമ ആദ്യം ട്രേ പരിധി ക്രമീകരിക്കണം','యజమాని ముందుగా ట్రే పరిమితులు నిర్ణయించాలి'),
 'helper_restricted':four('this item may go only to the named person','இந்தப் பொருள் குறிப்பிட்ட நபருக்கு மட்டும்','ഈ സാധനം നിർദ്ദിഷ്ട വ്യക്തിക്ക് മാത്രം','ఈ వస్తువు పేర్కొన్న వ్యక్తికి మాత్రమే'),
 'place_unreachable':four('the destination is not reachable','இலக்கை அடைய முடியாது','ലക്ഷ്യസ്ഥാനത്ത് എത്താനാവില്ല','గమ్యం చేరలేము'),
 'physical_operation':four('I cannot operate doors, lifts, stairs or arms','கதவு, லிஃப்ட், படிகள் அல்லது கை இயக்கம் செய்ய முடியாது','വാതിൽ, ലിഫ്റ്റ്, പടികൾ, കൈ പ്രവർത്തനം ചെയ്യാനാവില്ല','తలుపులు, లిఫ్టులు, మెట్లు లేదా చేతి పనులు చేయలేను'),
 'unsupported_job':four('this task is outside my carrier jobs','இந்த வேலை எனது சுமை எடுத்துச் செல்லும் பணியில் இல்லை','ഈ ജോലി എന്റെ വാഹക ജോലികളിലില്ല','ఈ పని నా రవాణా పనుల్లో లేదు'),
}
# Said after a refusal, so the person learns what the robot can follow (6 Nov live check: a fallback
# "if she is not there, go to Priya and ask her where she is" was refused with no hint).
TEMPLATES.update({
 'hint_condition':four("I can follow only these 'if' rules: if someone is not there, leave it with another person, bring it back, or try again in some minutes; after a question, do one thing for yes and another for no.",
  "இந்த 'என்றால்' விதிகளை மட்டுமே பின்பற்ற முடியும்: ஒருவர் இல்லையென்றால் வேறொருவரிடம் கொடுக்கலாம், திரும்பக் கொண்டு வரலாம், அல்லது சில நிமிடங்களில் மீண்டும் முயற்சிக்கலாம்; கேள்விக்குப் பிறகு ஆம் என்றால் ஒன்று, இல்லை என்றால் வேறொன்று செய்யலாம்.",
  "ഈ 'എങ്കിൽ' നിയമങ്ങൾ മാത്രമേ പാലിക്കാനാകൂ: ആൾ ഇല്ലെങ്കിൽ മറ്റൊരാളെ ഏൽപ്പിക്കാം, തിരികെ കൊണ്ടുവരാം, അല്ലെങ്കിൽ കുറച്ച് മിനിറ്റിനുശേഷം വീണ്ടും ശ്രമിക്കാം; ചോദ്യത്തിനുശേഷം അതെ എങ്കിൽ ഒന്ന്, അല്ല എങ്കിൽ മറ്റൊന്ന് ചെയ്യാം.",
  "ఈ 'అయితే' నియమాలు మాత్రమే పాటించగలను: వ్యక్తి లేకపోతే వేరొకరికి ఇవ్వగలను, తిరిగి తీసుకురాగలను, లేదా కొన్ని నిమిషాల తర్వాత మళ్లీ ప్రయత్నించగలను; ప్రశ్న తర్వాత అవును అయితే ఒకటి, కాదు అయితే మరొకటి చేయగలను."),
 'hint_negation':four('Please tell me what to do, not what to avoid.','எதைத் தவிர்க்க வேண்டும் என்பதை அல்ல, என்ன செய்ய வேண்டும் என்று சொல்லுங்கள்.',
  'എന്ത് ഒഴിവാക്കണം എന്നല്ല, എന്ത് ചെയ്യണം എന്ന് പറയൂ.','ఏమి చేయకూడదో కాదు, ఏమి చేయాలో చెప్పండి.'),
 'hint_rephrase':four('Please say it again in short sentences, one job each.','ஒவ்வொரு வேலைக்கும் ஒரு சிறு வாக்கியமாக மீண்டும் சொல்லுங்கள்.',
  'ഓരോ ജോലിക്കും ഓരോ ചെറിയ വാക്യമായി വീണ്ടും പറയൂ.','ప్రతి పనికి ఒక చిన్న వాక్యంగా మళ్లీ చెప్పండి.'),
})
REFUSAL_HINTS={'unsupported_condition':'hint_condition','negation':'hint_negation','invalid_plan':'hint_rephrase'}

CAPABILITY_REASONS['too_many_items']=four('that is more items than I can carry at once','ஒரே நேரத்தில் இத்தனை பொருட்களை எடுத்துச் செல்ல முடியாது','ഒരേസമയം ഇത്രയും സാധനങ്ങൾ കൊണ്ടുപോകാനാവില്ല','ఒకేసారి ఇన్ని వస్తువులు తీసుకెళ్లలేను')
# The closing question (0.6.0): asked at the last stop, to a person on the office list.
TEMPLATES['anything_else']=four("{recipient}, do you need anything else from me? Tell me the job, or say 'no' and I will go back to the dock.",
 "{recipient}, வேறு ஏதாவது வேலை இருக்கிறதா? வேலையைச் சொல்லுங்கள்; இல்லையென்றால் 'இல்லை' என்று சொல்லுங்கள், நான் டாக்கிற்குத் திரும்புவேன்.",
 "{recipient}, മറ്റെന്തെങ്കിലും ജോലി ഉണ്ടോ? ജോലി പറയൂ; ഇല്ലെങ്കിൽ 'ഇല്ല' എന്ന് പറയൂ, ഞാൻ ഡോക്കിലേക്ക് മടങ്ങും.",
 "{recipient}, ఇంకేదైనా పని ఉందా? పని చెప్పండి; లేకపోతే 'లేదు' అని చెప్పండి, నేను డాక్‌కు తిరిగి వెళ్తాను.")
TEMPLATES['anything_else_what']=four('What would you like me to do?','என்ன செய்ய வேண்டும்?','എന്താണ് ചെയ്യേണ്ടത്?','ఏమి చేయాలి?')
TEMPLATES['carrier_refusal']=four('I cannot do this: {reason}. I can deliver, collect small items, carry a message or check something.','இதைச் செய்ய முடியாது: {reason}. பொருள் கொடுக்க, சிறிய பொருள் வாங்கிவர, செய்தி எடுத்துச் செல்ல அல்லது விசாரிக்க முடியும்.','ഇത് ചെയ്യാനാവില്ല: {reason}. സാധനം എത്തിക്കാം, ചെറിയ സാധനം ശേഖരിക്കാം, സന്ദേശം കൊണ്ടുപോകാം, അന്വേഷിക്കാം.','ఇది చేయలేను: {reason}. వస్తువు ఇవ్వగలను, చిన్నవి తేగలను, సందేశం తీసుకెళ్లగలను లేదా విచారించగలను.')

# -- 0.7.0: looking for a person, and plainer English ---------------------------------------------
TEMPLATES['call_person']=four('{recipient}, are you there?','{recipient}, இருக்கிறீர்களா?','{recipient}, ഇവിടെയുണ്ടോ?','{recipient}, ఉన్నారా?')
TEMPLATES['searching']=four("I can't find {recipient} here. I'll look nearby.",'{recipient} இங்கே இல்லை. அருகில் பார்க்கிறேன்.','{recipient} ഇവിടെ ഇല്ല. അടുത്ത് നോക്കാം.','{recipient} ఇక్కడ లేరు. దగ్గరలో చూస్తాను.')
TEMPLATES['search_ask']=four('Excuse me, is {recipient} here, or do you know where I can find them?','மன்னிக்கவும், {recipient} இங்கே இருக்கிறார்களா? அல்லது எங்கே இருக்கிறார்கள் என்று தெரியுமா?','ക്ഷമിക്കണം, {recipient} ഇവിടെയുണ്ടോ? അല്ലെങ്കിൽ എവിടെയാണെന്ന് അറിയാമോ?','క్షమించండి, {recipient} ఇక్కడ ఉన్నారా? లేదా ఎక్కడ ఉన్నారో తెలుసా?')
TEMPLATES['search_failed']=four("Sorry, I couldn't find {recipient}.",'மன்னிக்கவும், {recipient} அவர்களைக் கண்டுபிடிக்க முடியவில்லை.','ക്ഷമിക്കണം, {recipient}യെ കണ്ടെത്താനായില്ല.','క్షమించండి, {recipient} కనిపించలేదు.')
TEMPLATES['not_there_tell']=four("{recipient} is not here. I'll let {requester} know.",'{recipient} இங்கே இல்லை. {requester} அவர்களிடம் தெரிவிக்கிறேன்.','{recipient} ഇവിടെ ഇല്ല. {requester}യെ അറിയിക്കാം.','{recipient} ఇక్కడ లేరు. {requester}కి తెలియజేస్తాను.')
PHRASES['failed_not_here']=four('I could not find {to}','{to} அவர்களைக் காண முடியவில்லை','{to}യെ കണ്ടില്ല','{to} కనిపించలేదు')
# -- 0.8.0: conversation --------------------------------------------------------------------------
TEMPLATES['arrive_visit']=four('Hello {recipient}. {requester} asked me to come to you. How can I help?','வணக்கம் {recipient}. {requester} என்னை உங்களிடம் அனுப்பினார். என்ன உதவி வேண்டும்?','നമസ്കാരം {recipient}. {requester} എന്നെ നിങ്ങളുടെ അടുത്തേക്ക് അയച്ചു. എന്ത് സഹായം വേണം?','నమస్కారం {recipient}. {requester} నన్ను మీ దగ్గరకు పంపారు. ఏ సహాయం కావాలి?')
TEMPLATES['english_only']=four("Sorry, I can only work in English for now. Could you say that again in English, please?",'மன்னிக்கவும், இப்போது ஆங்கிலத்தில் மட்டுமே வேலை செய்கிறேன். ஆங்கிலத்தில் மீண்டும் சொல்லுங்கள்.','ക്ഷമിക്കണം, ഇപ്പോൾ ഇംഗ്ലീഷിൽ മാത്രമേ പ്രവർത്തിക്കൂ. ഇംഗ്ലീഷിൽ വീണ്ടും പറയൂ.','క్షమించండి, ప్రస్తుతం ఇంగ్లీషులో మాత్రమే పని చేస్తున్నాను. ఇంగ్లీషులో మళ్లీ చెప్పండి.')
TEMPLATES['say_slowly']=four("Sorry, I couldn't make that out. Could you say it once more, slowly?",'மன்னிக்கவும், புரியவில்லை. மெதுவாக மீண்டும் சொல்லுங்கள்.','ക്ഷമിക്കണം, മനസ്സിലായില്ല. പതുക്കെ ഒന്നുകൂടി പറയൂ.','క్షమించండి, అర్థం కాలేదు. నెమ్మదిగా మళ్లీ చెప్పండి.')
TEMPLATES['not_understood']=four("Sorry, I didn't understand that. Could you say it again slowly, in short sentences? For example: give the file to Divya.",'மன்னிக்கவும், புரியவில்லை. சிறிய வாக்கியங்களில் மெதுவாக மீண்டும் சொல்லுங்கள்.','ക്ഷമിക്കണം, മനസ്സിലായില്ല. ചെറിയ വാക്യങ്ങളിൽ പതുക്കെ വീണ്ടും പറയൂ.','క్షమించండి, అర్థం కాలేదు. చిన్న వాక్యాల్లో నెమ్మదిగా మళ్లీ చెప్పండి.')
TEMPLATES['what_change']=four("No problem. What should I do instead? Tell me the whole job again, or say no to drop it.",'பரவாயில்லை. அதற்குப் பதிலாக என்ன செய்ய வேண்டும்? முழு வேலையையும் மீண்டும் சொல்லுங்கள்.','സാരമില്ല. പകരം എന്ത് ചെയ്യണം? മുഴുവൻ ജോലിയും വീണ്ടും പറയൂ.','పర్వాలేదు. బదులుగా ఏమి చేయాలి? మొత్తం పనిని మళ్లీ చెప్పండి.')
TEMPLATES['dropped']=four("Okay, I've dropped that job.",'சரி, அந்த வேலையை விட்டுவிட்டேன்.','ശരി, ആ ജോലി ഒഴിവാക്കി.','సరే, ఆ పనిని వదిలేశాను.')
TEMPLATES['declined']=four("No problem. I'll take it back to {requester}.",'பரவாயில்லை. {requester} அவர்களிடம் திருப்பிக் கொண்டு செல்கிறேன்.','സാരമില്ല. {requester}ക്ക് തിരികെ കൊണ്ടുപോകാം.','పర్వాలేదు. {requester}కి తిరిగి తీసుకెళ్తాను.')
TEMPLATES['greeting']=four('Hello! How can I help? I can deliver or collect small items, pass on a message, ask someone a question, call someone, or go to a place.','வணக்கம்! என்ன உதவி வேண்டும்? பொருள் கொடுக்க, வாங்கிவர, செய்தி சொல்ல, கேள்வி கேட்க அல்லது ஓர் இடத்துக்குச் செல்ல முடியும்.','നമസ്കാരം! എന്ത് സഹായം വേണം? സാധനം എത്തിക്കാം, ശേഖരിക്കാം, സന്ദേശം നൽകാം, ചോദ്യം ചോദിക്കാം, ഒരിടത്തേക്ക് പോകാം.','నమస్కారం! ఏ సహాయం కావాలి? వస్తువు ఇవ్వగలను, తేగలను, సందేశం చెప్పగలను, ప్రశ్న అడగగలను, ఒక చోటుకు వెళ్లగలను.')
TEMPLATES['welcome']=four("You're welcome.",'பரவாயில்லை.','സ്വാഗതം.','పర్వాలేదు.')
TEMPLATES['capabilities']=four('I can take small items to people, collect things, pass on messages, ask someone a question and bring back the answer, call someone, or go to a place. I ask before I start, and you can say stop at any time.','சிறிய பொருட்களைக் கொண்டு செல்ல, வாங்கிவர, செய்தி சொல்ல, கேள்வி கேட்டு பதில் கொண்டுவர, ஒருவரை அழைக்க அல்லது ஓர் இடத்துக்குச் செல்ல முடியும்.','ചെറിയ സാധനങ്ങൾ എത്തിക്കാം, ശേഖരിക്കാം, സന്ദേശം നൽകാം, ചോദ്യം ചോദിച്ച് മറുപടി കൊണ്ടുവരാം, ഒരാളെ വിളിക്കാം, ഒരിടത്തേക്ക് പോകാം.','చిన్న వస్తువులు తీసుకెళ్లగలను, తేగలను, సందేశం చెప్పగలను, ప్రశ్న అడిగి సమాధానం తేగలను, ఒకరిని పిలవగలను, ఒక చోటుకు వెళ్లగలను.')
TEMPLATES['introduce']=four("I'm the office robot. I carry small things and messages between the people here.",'நான் அலுவலக ரோபோ. இங்குள்ளவர்களுக்கு இடையே சிறிய பொருட்களையும் செய்திகளையும் கொண்டு செல்கிறேன்.','ഞാൻ ഓഫീസ് റോബോട്ടാണ്. ഇവിടെയുള്ളവർക്കിടയിൽ ചെറിയ സാധനങ്ങളും സന്ദേശങ്ങളും കൊണ്ടുപോകുന്നു.','నేను ఆఫీస్ రోబోను. ఇక్కడి వారి మధ్య చిన్న వస్తువులు, సందేశాలు తీసుకెళ్తాను.')
TEMPLATES['person_place']=four("{recipient}'s usual place is {place}. I can go there if you ask me.",'{recipient} வழக்கமாக {place} இல் இருப்பார்.','{recipient} സാധാരണ {place}ൽ ആയിരിക്കും.','{recipient} సాధారణంగా {place}లో ఉంటారు.')
PHRASES['failed_declined']=four('{to} did not take {item}','{to} {item} எடுக்கவில்லை','{to} {item} എടുത്തില്ല','{to} {item} తీసుకోలేదు')
TEMPLATES['confirm_tentative']=four("I'm not fully sure I understood, so please check this.",'சரியாகப் புரிந்ததா என்று உறுதியில்லை; இதைச் சரிபார்க்கவும்.','ശരിയായി മനസ്സിലായോ എന്ന് ഉറപ്പില്ല; ഇത് പരിശോധിക്കൂ.','సరిగ్గా అర్థమైందో లేదో ఖచ్చితంగా తెలియదు; దీన్ని సరిచూడండి.')
NATURAL_ENGLISH={
 'start_step':"Next, I'll {description}.",
 'arrive_deliver':"Hello {recipient}. {requester} sent you {item}. Please take it and tell me when you have it.",
 'arrive_retrieve':"Hello. {requester} asked me to collect {item}. Could you put it on my tray and tell me when it's done?",
 'arrive_check':"Hello. {requester} has a question for you. It is on my screen. What shall I tell them?",
 'arrive_tell':"Hello {recipient}. I have a message for you from {requester}. It is on my screen. Please say okay when you have read it.",
 'reminder':"{recipient}, I'm still here with {item}. Could you answer, please?",
 'not_there':"I see, {recipient} is not here. {policy}.",
 'step_done':"That's done.",
 'report':"Here is how it went: {summary}. Any answers or messages are on the screen.",
 'job_failed':"Sorry, I couldn't finish this: {reason}. Anything I was carrying is still with me.",
 'returning_home':"That's everything for now. I'm heading back to the dock.",
 'idle':"I'm back at the dock. Call me when you need me.",
 'ask_which':"Which one do you mean: {options}?",
 'unknown_place':"Sorry, that place is not on my map. Which place should I go to?",
 'unknown_name':"Sorry, I don't know that name. Who do you mean?",
 'cannot_do':"Sorry, that is not something I can do. I can deliver or collect small items, pass on a message, ask a question or go to a place.",
 'stopped':"Okay, I've stopped. I'll wait here for instructions.",
 'split_job':"That's a lot for one go. Could you give it to me in parts of at most six steps?",
 'clarify_request':"Sorry, I didn't quite get that. Could you tell me again who or where, and what to take?",
 'reply_prompt':"Sorry, I didn't catch that. Could you say it again?",
 'return_to_requester':"I'll take {item} back to {recipient}.",
 'model_timeout':"Sorry, that took me too long to work out. Could you say it in shorter sentences?",
 'pickup_check':"Is it light enough, and does it fit on my tray?",
 'verify_answer':"I've put your answer on the screen. Did I get it right?",
 'correct_answer':"Sorry about that. What is the right answer?",
 'received_answer':"I have the answer. It is on the screen.",
 'hint_rephrase':"Could you say it again in shorter sentences, one job each?",
 'hint_negation':"Could you tell me what to do, rather than what not to do?",
 'anything_else':"{recipient}, is there anything else I can do for you? If not, just say no and I'll head back to the dock.",
 'anything_else_what':"Of course. What would you like me to do?",
 'person_seen_elsewhere':"I'm told {recipient} is at {place}. Shall I go there and carry on?",
 'ask_people_where':"Excuse me, do you know where {recipient} is?",
 'carrier_refusal':"Sorry, I can't do this: {reason}. I can deliver or collect small items, pass on a message or ask a question.",
}
for _event,_text in NATURAL_ENGLISH.items():TEMPLATES[_event]={**TEMPLATES[_event],'en':_text}
NATURAL_PHRASES={
 'deliver':'take {item} to {to}','retrieve':'collect {item} from {origin} and bring it to {to}',
 'check':'go to {to} and ask your question','tell':'go to {to} and pass on your message','go':'go to {to}',
 'leave_with':"if they are away, I'll leave it with {to}",'bring_back':"I'll bring it back",'try_later':"I'll try again in {minutes} minutes",
 'ask_anyone':"I'll ask someone else there",'otherwise':"I'll do what you asked for this case",'skip':"I'll skip this step",
 'otherwise_branch':"if they are not there, I'll {steps}",'yes_branch':"if the answer is yes, I'll {steps}",'no_branch':"if the answer is no, I'll {steps}",
 'delivered':'I gave {item} to {to}','retrieved':'I brought {item} to {to}','checked':'I asked {to}',
 'told':'I passed your message to {to}','went':'I went to {to}','deferred':'one step is left for later','skipped':'I skipped one step',
 'failed':'one step could not be done','empty_report':'nothing could be completed',
}
for _key,_text in NATURAL_PHRASES.items():PHRASES[_key]={**PHRASES[_key],'en':_text}
OK_QUESTION['en']='Shall I go ahead?'
REASONS['no_reply']={**REASONS['no_reply'],'en':'nobody answered'}
REASONS['not_here']={**REASONS['not_here'],'en':'the person was not there'}


def _item(value,language):
    parts=split_items(value or 'item')
    if len(parts)>1:
        named=[_item(part,language) for part in parts]
        if language=='en':
            named=[n if re.match(r'(?:the|a|an|some)\s',n) else 'the '+n for n in named]
            return ', '.join(named[:-1])+' and '+named[-1]
        return ', '.join(named)
    key=normalize(value or 'item')
    for prefix in ('the ','a ','this '):
        if key.startswith(prefix): key=key[len(prefix):]
    return ITEMS.get(key,ITEMS['item'])[language]


def _number(value,minimum=0,maximum=1440):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError('Invalid spoken number')
    return value


def _policy(value,roster,language):
    if value.startswith('leave_with:'):
        target=value.split(':',1)[1]
        if not roster.person(target): raise ValueError('Alternate recipient must be a person')
        return PHRASES['leave_with'][language].format(to=roster.label(target,language))
    if value.startswith('try_later:'):
        return PHRASES['try_later'][language].format(minutes=_number(int(value.split(':')[1]),1))
    if value not in ('bring_back','ask_anyone','skip','otherwise'): raise ValueError('Invalid spoken policy')
    return PHRASES[value][language]


def _description(step,roster,language):
    if roster is None: raise ValueError('Approved names/requester context required')
    validate_plan({'steps':[{k:v for k,v in step.items() if k!='n'}],'issue':None})
    action=step['action']
    target=step.get('to') or step.get('at') or roster.requester_id
    values={'item':_item(step.get('item'),language),'to':roster.label(target,language)}
    if action=='retrieve': values['origin']=roster.label(step['from'],language)
    result=PHRASES[action][language].format(**values)
    policy=step.get('if_not_there','bring_back')
    if policy=='otherwise':
        result+='; '+PHRASES['otherwise_branch'][language].format(steps='; '.join(_description(s,roster,language) for s in step['otherwise']))
    elif policy!='bring_back': result+='; '+_policy(policy,roster,language)
    if step.get('on_answer'):
        for key in ('yes','no'):
            branch=step['on_answer'][key]
            if branch:
                result+='; '+PHRASES[key+'_branch'][language].format(steps='; '.join(_description(s,roster,language) for s in branch))
    return result


def say(event,language,**slots):
    if event not in TEMPLATES or language not in LANGUAGES:
        raise ValueError('Unknown event or language')
    allowed={'names','requester','steps','step','recipient','item','current','position','minutes','policy','outcomes','reason','options','place','nearest'}
    if set(slots)-allowed:
        raise ValueError('Arbitrary text cannot be inserted into speech')
    roster=None
    contextual=event in ('confirm_plan','queued','start_step','arrive_deliver','arrive_retrieve','arrive_check',
                         'arrive_tell','reminder','return_to_requester','not_there','report','anything_else','call_person','searching','search_ask','search_failed','not_there_tell','arrive_visit','declined','person_place') or event in CLARIFY_EVENTS or event in MAP_EVENTS[:-1] or (event in ('ask_which','unknown_name','unknown_place') and slots.get('options'))
    if contextual and 'names' in slots:
        roster=OfficeNames(slots['names'],slots.get('requester'))
    if contextual and roster is None:
        raise ValueError('Approved names/requester context required')
    def label(key):
        if roster is None: raise ValueError('Approved names/requester context required')
        return roster.label(key,language)
    values={}
    if event=='carrier_refusal':
        reason=slots.get('reason')
        if reason in ('requires_stairs','requires_lift','requires_door_operation','requires_arm'):reason='physical_operation'
        if reason=='out_of_scope':reason='unsupported_job'
        if reason not in CAPABILITY_REASONS:raise ValueError('Unknown capability reason')
        values={'reason':CAPABILITY_REASONS[reason][language]}
    elif event=='confirm_collect':
        if not roster.person(slots['recipient']):raise ValueError('Collection proposal needs an approved person')
        values={'item':_item(slots.get('item'),language),'recipient':label(slots['recipient'])}
    elif event in ('clarify_person','clarify_place'):
        values={'options':', '.join(label(key) for key in slots.get('options',[]))}
    elif event=='clarify_item':
        values={'options':', '.join(dict.fromkeys(_item(value,language) for value in slots.get('options',[])))}
    elif event=='clarify_absence':
        values={'recipient':label(slots['recipient']),'policy':_policy(slots['policy'],roster,language)}
    elif event=='place_not_on_map':
        values={'place':label(slots['place']),'alternative':label(slots['nearest']) if slots.get('nearest') else MAP_ALTERNATIVE[language]}
    elif event=='place_unreachable':
        reason=slots.get('reason','unknown')
        if reason not in MAP_REASONS:raise ValueError('Unknown fixed route reason')
        values={'place':label(slots['place']),'reason':MAP_REASONS[reason][language]}
    elif event=='ask_where_person':
        values={'recipient':label(slots['recipient']),'options':', '.join(label(key) for key in slots.get('options',[])) or MAP_ALTERNATIVE[language]}
    elif event in ('ask_people_where','anything_else','call_person','searching','search_ask','search_failed'):values={'recipient':label(slots['recipient'])}
    elif event in ('not_there_tell','arrive_visit'):values={'recipient':label(slots['recipient']),'requester':label(roster.requester_id)}
    elif event=='declined':values={'requester':label(roster.requester_id)}
    elif event=='person_place':values={'recipient':label(slots['recipient']),'place':label(slots['place'])}
    elif event=='person_seen_elsewhere':values={'recipient':label(slots['recipient']),'place':label(slots['place'])}
    elif event=='unknown_place' and slots.get('options'):
        # Name a few mapped places, so the person can answer with one of them.
        return TEMPLATES['unknown_place'][language].split('. ')[0]+'. '+TEMPLATES['ask_which'][language].format(options=', '.join(label(key) for key in slots['options']))
    elif event=='unknown_name' and slots.get('options') and language=='en':
        return "Sorry, I don't know that name. Did you mean one of these: "+', '.join(label(key) for key in slots['options'])+'?'
    elif event=='unknown_name' and slots.get('options'):
        return TEMPLATES['unknown_name'][language]+' '+TEMPLATES['ask_which'][language].format(options=', '.join(label(key) for key in slots['options']))
    elif event=='confirm_plan':
        steps=slots['steps']
        validate_plan({'steps':steps,'issue':None})
        values={'count':len(steps),'steps':'. '.join(f'{i}) '+_description(s,roster,language) for i,s in enumerate(steps,1))}
        if language=='en':
            # Said the way a colleague would: "Sure. First I'll ..., then I'll .... Shall I go ahead?"
            parts=[_description(s,roster,language) for s in steps]
            body="I'll "+parts[0]+'.' if len(parts)==1 else ' '.join(('First' if i==0 else 'Then' if i<len(parts)-1 else 'And last,' if len(parts)>2 else 'Then')+" I'll "+part+'.' for i,part in enumerate(parts))
            return 'Sure. '+body+' '+OK_QUESTION[language]
        return values['steps']+'. '+OK_QUESTION[language]
    elif event=='queued':
        values={'current':label(slots['current']),'position':_number(slots['position'],1),
                'minutes':_number(slots['minutes'],0)}
    elif event=='start_step':
        step=slots['step']
        values={'n':_number(step['n'],1,6),'description':_description(step,roster,language)}
    elif event in ('arrive_deliver','arrive_retrieve','arrive_check','arrive_tell'):
        values['requester']=label(roster.requester_id)
        if event in ('arrive_deliver','arrive_tell'): values['recipient']=label(slots['recipient'])
        if event in ('arrive_deliver','arrive_retrieve'): values['item']=_item(slots.get('item'),language)
    elif event in ('reminder','return_to_requester'):
        values={'recipient':label(slots['recipient']),'item':_item(slots.get('item'),language)}
    elif event=='not_there':
        policy=_policy(slots['policy'],roster,language)
        values={'recipient':label(slots['recipient']),'policy':policy[:1].upper()+policy[1:] if language=='en' else policy}
    elif event=='ask_which':
        options=slots.get('options',[])
        if not options: return TEMPLATES['unknown_name'][language]
        values={'options':', '.join(label(key) for key in options)}
    elif event=='job_failed':
        reason=slots.get('reason','failed')
        if reason not in REASONS: raise ValueError('Unknown fixed failure reason')
        values={'reason':REASONS[reason][language]}
    elif event=='report':
        summaries=[]
        for outcome in slots.get('outcomes',[]):
            status=outcome['status']
            if status not in ('delivered','retrieved','checked','told','went','deferred','skipped','failed'):
                raise ValueError('Unknown report status')
            values_for={'item':_item(outcome.get('item'),language)}
            if status in ('delivered','retrieved','checked','told','went'):
                values_for['to']=label(outcome['target'])
            if status=='failed' and outcome.get('reason')=='declined' and roster.person(outcome.get('target')):
                summaries.append(PHRASES['failed_declined'][language].format(to=label(outcome['target']),item=_item(outcome.get('item'),language)));continue
            if status=='failed' and outcome.get('reason')=='not_here' and roster.person(outcome.get('target')):
                summaries.append(PHRASES['failed_not_here'][language].format(to=label(outcome['target'])));continue
            phrase='helper_'+status if (outcome.get('provenance')=='helper' or outcome.get('answered_by_someone_else')) and status in ('delivered','retrieved','checked') else status
            summaries.append(PHRASES[phrase][language].format(**values_for))
        values={'summary':'; '.join(summaries) if summaries else PHRASES['empty_report'][language]}
    return TEMPLATES[event][language].format(**values)
