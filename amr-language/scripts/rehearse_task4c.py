#!/usr/bin/env python3
"""Authored English/Tamil behavior rehearsals with mock rewrites; no accuracy claim."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import yaml
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from amr_language.office_backend import MockOfficeBackend
from amr_language.rehearsal import Rehearsal


def fixture():
    names=yaml.safe_load((ROOT/'reference/tags_example.yaml').read_text())
    names['robot_capability']=yaml.safe_load((ROOT/'tests/fixtures/robot_capability.yaml').read_text())
    for room in names['rooms']:room.update(on_map=True,reachable=True)
    for person in names['people']:person['usual_place']=person['seat']
    names['objects'].append({'name':'printer_payload','room':'storage','aliases':['printer'],'weight_class':'heavy','fits_tray':False})
    return names


# These are scripted translations, not measured model rewrites or owner examples.
SCENARIOS=[
 ('heavy','Deliver the printer to Divya.','printer திவ்யாவிடம் கொடுங்கள்.',[], 'refuse',[]),
 ('unknown_yes','Get the widget from reception.','reception இடத்திலிருந்து widget வாங்கி வாருங்கள்.',['yes','done','yes','taken'],'rehearsed',['retrieved']),
 ('unknown_no','Get the widget from reception; then go home.','reception இடத்தில் widget வாங்கி பிறகு வீட்டுக்குச் செல்லுங்கள்.',['yes','done','no'],'rehearsed',['failed','went']),
 ('unsupported','Make tea.','தேநீர் செய்யுங்கள்.',[],'refuse',[]),
 ('queue','Go to reception; then ask at reception: Is it free?; then go to cafeteria; then go to storage; then go to conference hall; then go to demo area; then go to home; then go to team office.','பல இடங்களுக்கு வரிசையாகச் சென்று reception இடத்தில் கேளுங்கள்.',['yes','yes','yes','yes'],'rehearsed',['checked','went','went','went','went','went']),
 ('place','Ask at reception: Did two visitors come?','reception இடத்தில் இரண்டு பார்வையாளர்கள் வந்தார்களா என்று கேளுங்கள்.',['yes','  Two visitors came.  ','yes'],'rehearsed',['checked']),
 ('position','Ask Divya where she is now.','திவ்யா இப்போது எங்கே இருக்கிறார் என்று கேளுங்கள்.',['yes','conference hall','yes','no'],'rehearsed',['checked']),
 ('approval','Ask Ravi whether he approves the draft.','வரைவுக்கு ரவி ஒப்புதல் தருகிறாரா என்று கேளுங்கள்.',['yes','No, I need another day.','yes'],'rehearsed',['checked']),
 ('corrected','Ask Priya what time suits her.','பிரியாவுக்கு எந்த நேரம் வசதியாக இருக்கும் என்று கேளுங்கள்.',['yes','three','no','four','yes'],'rehearsed',['checked']),
 ('verify_rejected','Ask at reception: Is it free?','reception இடம் காலியாக உள்ளதா என்று கேளுங்கள்.',['yes','yes','no','no','no'],'rehearsed',['failed']),
 ('bag_no','Get the battery pack from reception.','reception இடத்திலிருந்து battery pack வாங்கி வாருங்கள்.',['yes','done','yes','no'],'rehearsed',['failed']),
 ('new_person','Deliver the charger to Karthik.','charger கார்த்திக்கிடம் கொடுங்கள்.',['yes','reception','yes','yes','taken'],'rehearsed',['delivered']),
 ('helper_retrieve','Get the encoder from Karthik.','encoder கார்த்திக்கிடமிருந்து வாங்கி வாருங்கள்.',['yes','reception','yes','no','/helper_name Visitor','done','yes','taken'],'rehearsed',['retrieved']),
 ('nobody','Get the encoder from Karthik.','encoder கார்த்திக்கிடமிருந்து வாங்கி வாருங்கள்.',['yes','reception','yes','no','not here'],'rehearsed',['failed']),
 ('helper_allowed','Deliver the charger to Karthik; if he is not there, ask anyone there.','charger கார்த்திக்கிடம் கொடுங்கள்; அவர் இல்லையென்றால் அங்குள்ள யாரிடமாவது கொடுங்கள்.',['yes','reception','yes','no','/helper_name Visitor','taken'],'rehearsed',['delivered']),
 ('helper_denied','Deliver the charger to Karthik.','charger கார்த்திக்கிடம் மட்டும் கொடுங்கள்.',['yes','reception','yes','no'],'rehearsed',['failed']),
 ('helper_check','Ask Karthik whether he approves the draft; if he is not there, ask anyone there.','கார்த்திக் வரைவுக்கு ஒப்புதல் தருகிறாரா கேளுங்கள்; இல்லையென்றால் மற்றவரிடம் கேளுங்கள்.',['yes','reception','yes','no','/helper_name Visitor','Looks fine to me.','yes'],'rehearsed',['checked']),
 ('guess_not_found','I think I left the encoder at reception.','encoder reception இடத்தில் விட்டதாக நினைக்கிறேன்.',['yes','not here'],'rehearsed',['failed']),
 ('hint','Get the encoder I left on the bench from Ravi.','bench மேல் வைத்த encoder ரவியிடமிருந்து வாங்கி வாருங்கள்.',['yes','done','taken'],'rehearsed',['retrieved']),
 ('omitted','Go to reception; then make tea.','reception செல்லுங்கள்; பிறகு தேநீர் செய்யுங்கள்.',['yes'],'rehearsed',['went']),
]


def run_scenario(case,language):
    key,english,native,replies,decision,statuses=case
    names=fixture();before=deepcopy(names)
    backend=MockOfficeBackend([english]*4,[{}]*4)
    localized={'yes':'ஆம்','no':'இல்லை','taken':'எடுத்துக்கிட்டேன்','done':'முடிந்தது','not here':'இங்க இல்லை'}
    answers=iter([localized.get(s,s) if language=='ta' else s for s in replies])
    def read(_):
        try:return next(answers)
        except StopIteration:raise EOFError()
    session=Rehearsal(names,{'backend':backend},input_fn=read,output_fn=lambda _:None,source='SCRIPTED')
    result=session.run(english if language=='en' else native,'Bharathwaj',language)
    assert names==before,'Names mutated'
    assert result['status']=='finished',(key,language,result)
    assert result['jobs'][0]['decision']==decision,(key,language,result)
    if statuses:assert [o['status'] for o in result['jobs'][0]['outcomes']]==statuses,(key,language,result)
    assert not any(c[0]=='answer' for c in backend.calls),'Answer passed through model'
    return {'id':key+'-'+language,'source':'SCRIPTED','native_review':'pending','backend':'mock rewrites','result':result,'events':session.records}


def learning_scenarios():
    import csv
    import tempfile
    import shutil
    from amr_language.office_learning import LearningStore,regression,accept
    from amr_language.office import understand
    rows=[]
    for lang in ('en','ta'):
        with tempfile.TemporaryDirectory(prefix='amr-scripted-learning-') as directory:
            root=Path(directory);(root/'data').mkdir()
            for name in ('owner_check.csv','grammar_holdout.csv','grammar_holdout_v2.csv','learned_aliases.json','learned_aliases.sha256','office_meanings.json','office_meanings.sha256'):
                shutil.copyfile(ROOT/'data'/name,root/'data'/name)
            # No synthetic row is ever written to the production OWNER sheet.
            with (root/'data/owner_check.csv').open('a',newline='') as stream:
                csv.writer(stream).writerow(['OWNER-SCRIPTED','OWNER','en','Go to reception.','Go to reception.','go reception','OWNER_CONFIRMED:SCRIPTED_TEST_ONLY'])
            with (ROOT/'data/owner_check.csv').open(newline='') as stream:
                rewrites={r['text']:r['expected_english'] for r in csv.DictReader(stream)}
            def evaluate(text,names,requester,language,settings):
                english=rewrites.get(text,text)
                return understand(text,names,requester,language,{**settings,'backend':MockOfficeBackend([english]*4,[{}]*4)})
            store=LearningStore(root)
            for key,candidate in [('learning_approved',{'alias':'cradle','target':'home'}),('learning_rejected',{'alias':'reception','target':'home'})]:
                row=store.append('alias',candidate,source='SCRIPTED',requester_id='scripted_person',language=lang,typed_text='SCRIPTED clarification',decisions={'readback_yes':True})
                report=regression(candidate,'alias',fixture(),{},root=root,evaluate=evaluate)
                if key=='learning_approved':
                    assert report['passed'];accept(store,row,report,owner_confirmed=True)
                else:
                    assert not report['passed']
                    try:accept(store,row,report,owner_confirmed=True)
                    except ValueError:pass
                    else:raise AssertionError('Regressing data was accepted')
                    store.review_event(row,'rejected_regression',report)
                rows.append({'id':key+'-'+lang,'source':'SCRIPTED','owner_approval':'SCRIPTED TEST ONLY','candidate':row,'report':report,'review':store.rows()[-1]})
    return rows


def main():
    target=ROOT/'records/task4c/rehearsals.jsonl';target.parent.mkdir(parents=True,exist_ok=True)
    rows=[run_scenario(case,lang) for case in SCENARIOS for lang in ('en','ta')]+learning_scenarios()
    target.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
    print(f'{len(rows)} scripted behavior rehearsals completed; no native accuracy measurement. {target}')


if __name__=='__main__':main()
