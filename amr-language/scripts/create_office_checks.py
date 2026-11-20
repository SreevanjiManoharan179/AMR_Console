#!/usr/bin/env python3
"""Create authored STARTER sheets and blank owner rows; never invent real commands."""
import csv
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent

EN=[
 'Give the file to Divya; if she is not there, leave it with Priya.',
 'Get the charger from Arun and bring it to me.',
 'Ask at reception: Did any visitors come today?',
 'Tell Ravi: the meeting is at 3.',
 'Go home to the dock.',
 'Check at the conference hall: Is the room free? If yes, tell Ravi: the room is free. If no, deliver the file to Priya.',
 'Give the file to Priya, get the charger from Arun for me, then tell Ravi: I am ready.',
 'Go to the server room.',
 'Make tea.',
 'Go to reception; go to storage; go to cafeteria; go to demo area; go to open office; go to team office; go to manager cabin.',
]
TEXTS={
 'en':EN,
 'ta':[
 'இந்த கோப்பை திவ்யாவிடம் கொடுங்க. அவங்க இல்லைன்னா பிரியாவிடம் விட்டுடுங்க.',
 'அருண்கிட்ட இருக்கிற சார்ஜரை வாங்கிட்டு என்கிட்ட கொண்டு வாங்க.',
 'வரவேற்பில் இன்று யாராவது விருந்தினர்கள் வந்தாங்களான்னு கேளுங்க.',
 'ரவிகிட்ட மீட்டிங் மூணு மணிக்குன்னு சொல்லுங்க.',
 'வேலை இல்லை, டாக்குக்கு திரும்பிப் போங்க.',
 'கூட்ட அறை காலியா என்று கேளுங்க. ஆம்னா ரவிகிட்ட அறை காலின்னு சொல்லுங்க. இல்லைன்னா கோப்பை பிரியாவிடம் கொடுங்க.',
 'பிரியாவிடம் கோப்பை கொடுங்க, அருணிடம் சார்ஜரை வாங்கி என்கிட்ட கொண்டு வாங்க, அப்புறம் ரவியிடம் நான் தயார் என்று சொல்லுங்க.',
 'server room-ku ponga.',
 'எனக்கு டீ போடுங்க.',
 'reception போங்க; storage போங்க; canteen போங்க; demo area போங்க; open office போங்க; team office போங்க; manager cabin போங்க.',
 ],
 'ml':[
 'ഈ ഫയൽ ദിവ്യയ്ക്ക് കൊടുക്കൂ. ദിവ്യ അവിടെ ഇല്ലെങ്കിൽ പ്രിയയ്ക്ക് കൊടുക്കൂ.',
 'അരുണിന്റെ അടുത്തുനിന്ന് ചാർജർ വാങ്ങി എനിക്ക് കൊണ്ടുവരൂ.',
 'ഇന്ന് ആരെങ്കിലും സന്ദർശകർ വന്നോ എന്ന് റിസപ്ഷനിൽ ചോദിക്കൂ.',
 'മീറ്റിംഗ് മൂന്ന് മണിക്കാണെന്ന് രവിയോട് പറയൂ.',
 'ജോലിയൊന്നുമില്ല, ഡോക്കിലേക്ക് മടങ്ങൂ.',
 'യോഗമുറി ഒഴിവാണോ എന്ന് ചോദിക്കൂ. അതെ എങ്കിൽ മുറി ഒഴിവാണെന്ന് രവിയോട് പറയൂ. അല്ലെങ്കിൽ ഫയൽ പ്രിയയ്ക്ക് കൊടുക്കൂ.',
 'ഫയൽ പ്രിയയ്ക്ക് കൊടുക്കൂ, അരുണിൽനിന്ന് ചാർജർ വാങ്ങി എനിക്ക് കൊണ്ടുവരൂ, പിന്നെ ഞാൻ തയ്യാറാണെന്ന് രവിയോട് പറയൂ.',
 'സെർവർ മുറിയിലേക്ക് പോകൂ.',
 'ചായ ഉണ്ടാക്കൂ.',
 'റിസപ്ഷനിലേക്ക് പോകൂ; സംഭരണ മുറിയിലേക്ക് പോകൂ; കാന്റീനിലേക്ക് പോകൂ; പ്രദർശന സ്ഥലത്തേക്ക് പോകൂ; തുറന്ന ഓഫീസിലേക്ക് പോകൂ; ടീം ഓഫീസിലേക്ക് പോകൂ; മാനേജർ കാബിനിലേക്ക് പോകൂ.',
 ],
 'te':[
 'ఈ ఫైల్ దివ్యకు ఇవ్వండి. ఆమె అక్కడ లేకపోతే ప్రియకు ఇవ్వండి.',
 'అరుణ్ దగ్గర చార్జర్ తీసుకుని నా దగ్గరకు తీసుకురండి.',
 'ఈ రోజు సందర్శకులు ఎవరైనా వచ్చారా అని రిసెప్షన్‌లో అడగండి.',
 'మీటింగ్ మూడు గంటలకు అని రవికి చెప్పండి.',
 'పని లేదు, డాక్‌కి తిరిగి వెళ్ళండి.',
 'సమావేశ గది ఖాళీగా ఉందా అని అడగండి. అవును అంటే గది ఖాళీగా ఉందని రవికి చెప్పండి. కాదు అంటే ఫైల్ ప్రియకు ఇవ్వండి.',
 'ఫైల్ ప్రియకు ఇవ్వండి, అరుణ్ దగ్గర చార్జర్ తీసుకుని నాకు తీసుకురండి, తరువాత నేను సిద్ధంగా ఉన్నానని రవికి చెప్పండి.',
 'సర్వర్ గదికి వెళ్ళండి.',
 'టీ తయారు చేయండి.',
 'రిసెప్షన్‌కి వెళ్ళండి; నిల్వ గదికి వెళ్ళండి; క్యాంటీన్‌కి వెళ్ళండి; ప్రదర్శన ప్రాంతానికి వెళ్ళండి; ఓపెన్ ఆఫీస్‌కి వెళ్ళండి; టీమ్ ఆఫీస్‌కి వెళ్ళండి; మేనేజర్ క్యాబిన్‌కి వెళ్ళండి.',
 ],
}


def step(action,**kw):
    return {'action':action,'if_not_there':'bring_back',**kw}


def plans():
    return [
     [step('deliver',item='file',to='Divya',if_not_there='leave_with:Priya')],
     [step('retrieve',item='charger',**{'from':'Arun','to':'Bharathwaj'})],
     [step('check',at='reception',question='Did any visitors come today?',on_answer=None)],
     [step('tell',to='Ravi',message='the meeting is at 3')],
     [step('go',to='home')],
     [step('check',at='conference_hall',question='Is the room free?',on_answer={
       'yes':[step('tell',to='Ravi',message='the room is free')],
       'no':[step('deliver',to='Priya',item='file')]})],
     [step('deliver',to='Priya',item='file'),step('retrieve',item='charger',**{'from':'Arun','to':'Bharathwaj'}),
      step('tell',to='Ravi',message='I am ready')],
     [],[],[],
    ]


def main():
    owner=ROOT/'data/owner_check.csv'
    if owner.exists():
        raise SystemExit('Refusing to overwrite owner_check.csv; preserve owner edits')
    labels=[]
    expected=plans()
    with owner.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['id','speaker','language','text','expected_english','expected_steps_notes'])
        writer.writeheader()
        for language,texts in TEXTS.items():
            for index,text in enumerate(texts):
                key=f'STARTER-{language}-{index+1:02}'
                issue='unknown_name' if index==7 else 'out_of_scope' if index==8 else 'too_many_steps' if index==9 else None
                notes=EN[index] if issue is None else {'unknown_name':'Ask for an approved destination name.',
                    'out_of_scope':'Refuse because this is outside the office abilities.',
                    'too_many_steps':'Ask the requester to split the seven-step job.'}[issue]
                writer.writerow({'id':key,'speaker':'STARTER','language':language,'text':text,
                                 'expected_english':EN[index],'expected_steps_notes':notes})
                labels.append({'id':key,'source':'STARTER','confirmed_by_owner':False,'expected_english':EN[index],
                               'expected_steps':expected[index],'expected_issue':issue,
                               'needs_native_speaker_check':language!='en'})
            for index in range(10):
                writer.writerow({'id':f'OWNER-{language}-'+('LONG-' if index<4 else 'SHORT-')+f'{index+1:02}',
                                 'speaker':'','language':language,'text':'','expected_english':'','expected_steps_notes':''})
    (ROOT/'data/owner_labels_proposed.json').write_text(json.dumps({'status':'UNCONFIRMED STARTER LABELS',
        'requester':'Bharathwaj','rows':labels},ensure_ascii=False,indent=2)+'\n')
    with (ROOT/'data/owner_labels_review.csv').open('w',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['id','rewrite_meaning_yes_no','steps_yes_no','names_yes_no','notes'])
        for label in labels: writer.writerow([label['id'],'','','',''])
    print('Created 40 STARTER commands, 40 blank owner rows (4 long per language), and unconfirmed proposed labels')


if __name__=='__main__': main()
