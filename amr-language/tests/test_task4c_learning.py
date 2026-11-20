from copy import deepcopy
from datetime import datetime,timedelta,timezone
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import pytest
from amr_language.office_learning import LearningStore,regression,accept,load_aliases,corpus,validate_candidate
from amr_language.office_backend import MockOfficeBackend
from amr_language.office import understand
from amr_language.rehearsal import Rehearsal


@pytest.fixture
def root(tmp_path):
    directory=tmp_path/'module';(directory/'data').mkdir(parents=True)
    for name in ('owner_check.csv','grammar_holdout.csv','grammar_holdout_v2.csv','learned_aliases.json','learned_aliases.sha256','office_meanings.json','office_meanings.sha256'):
        shutil.copyfile(Path('data')/name,directory/'data'/name)
    # One test-only confirmed OWNER row exercises inclusion without inventing production data.
    with (directory/'data/owner_check.csv').open('a',newline='') as stream:
        csv.writer(stream).writerow(['OWNER-SCRIPTED','OWNER','en','Go to reception.','Go to reception.','go reception','OWNER_CONFIRMED:SCRIPTED_TEST_ONLY'])
    return directory


def evaluator(text,names,requester,language,settings):
    if language=='en':english=text
    else:
        with Path('data/owner_check.csv').open(newline='') as stream:english=next(r['expected_english'] for r in csv.DictReader(stream) if r['text']==text)
    return understand(text,names,requester,language,{**settings,'backend':MockOfficeBackend([english]*4,[{}]*4)})


@pytest.mark.parametrize('language',['en','ta'])
def test_scripted_learning_approval_and_regression_rejection(root,names,language):
    store=LearningStore(root)
    row=store.append('alias',{'alias':'cradle','target':'home'},source='SCRIPTED',requester_id='scripted_person',language=language,typed_text='SCRIPTED clarification',decisions={'readback_yes':True})
    report=regression(row['candidate'],'alias',names,{},root=root,evaluate=evaluator)
    assert report['passed'] and report['measured']==143 and any(r['id']=='OWNER-SCRIPTED' for r in report['cases'])
    before=(root/'data/learned_aliases.json').read_bytes()
    with pytest.raises(ValueError):accept(store,row,report)
    assert (root/'data/learned_aliases.json').read_bytes()==before
    accept(store,row,report,owner_confirmed=True)
    assert load_aliases(root)['cradle']=='home' and json.loads((root/'data/learned_aliases.json').read_text())['version']==2
    bad=store.append('alias',{'alias':'reception','target':'home'},source='SCRIPTED',requester_id='scripted_person',language=language)
    failed=regression(bad['candidate'],'alias',names,{},root=root,evaluate=evaluator)
    assert not failed['passed'] and failed['failures']
    with pytest.raises(ValueError):accept(store,bad,failed,owner_confirmed=True)
    assert 'reception' not in load_aliases(root)
    store.review_event(bad,'rejected_regression',failed)
    assert not store.pending()


def test_changed_candidate_or_data_invalidates_review(root,names):
    store=LearningStore(root);row=store.append('alias',{'alias':'cradle','target':'home'},source='SCRIPTED')
    report=regression(row['candidate'],'alias',names,{},root=root,evaluate=evaluator)
    edited=deepcopy(row);edited['candidate']['target']='storage'
    with pytest.raises(ValueError):accept(store,edited,report,owner_confirmed=True)
    (root/'data/grammar_holdout.csv').write_text('changed')
    with pytest.raises(ValueError):accept(store,row,report,owner_confirmed=True)


def test_unmeasured_case_never_accepts(root,names):
    def failed(*args):return {'decision':'refuse','refuse':{'reason':'model_timeout'}}
    report=regression({'alias':'foo','target':'home'},'alias',names,{},root=root,evaluate=failed)
    assert not report['passed'] and report['measured']==0 and len(report['not_measured'])==143


def test_learning_does_not_modify_names_map_or_code(root,names):
    original=deepcopy(names);store=LearningStore(root)
    row=store.append('person_place',{'person':'Karthik','place':'reception'},source='SCRIPTED')
    report=regression(row['candidate'],row['kind'],names,{},root=root,evaluate=evaluator)
    data_before={p.name:p.read_bytes() for p in (root/'data').iterdir()}
    accept(store,row,report,owner_confirmed=True)
    assert names==original and data_before=={p.name:p.read_bytes() for p in (root/'data').iterdir()}
    assert store.rows()[-1]['status']=='approved_for_console_review'


def test_word_meaning_and_owner_row_version_hash(root,names):
    store=LearningStore(root)
    row=store.append('office_meaning',{'language':'en','word':'adapter','meaning':'small cable adapter'},source='SCRIPTED')
    report=regression(row['candidate'],row['kind'],names,{},root=root,evaluate=evaluator)
    accept(store,row,report,owner_confirmed=True)
    path=root/'data/office_meanings.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==path.with_suffix('.sha256').read_text().strip()
    assert json.loads(path.read_text())['version']==1
    row=store.append('owner_row',{'language':'en','text':'Go home.','expected_english':'Go home.','expected_steps_notes':'go home'},source='SCRIPTED',requester_id='scripted_person')
    report=regression(row['candidate'],row['kind'],names,{},root=root,evaluate=evaluator)
    accept(store,row,report,owner_confirmed=True)
    assert len(corpus(root))==144
    assert json.loads((root/'data/owner_check.version.json').read_text())['version']==1


def test_privacy_retention_delete_and_no_audio(root):
    store=LearningStore(root)
    with pytest.raises(ValueError):store.append('alias',{'alias':'a','target':'home'},audio='forbidden')
    old=store.append('alias',{'alias':'old','target':'home'},source='SCRIPTED',requester_id='private_person')
    store.append('alias',{'alias':'new','target':'home'},source='SCRIPTED',requester_id='another_person')
    future=datetime.now(timezone.utc)+timedelta(days=31)
    store.review_event(old,'rejected',{})
    assert store.prune(now=future)==1 # reviewed row retained; unapproved row removed
    assert store.prune(person='private_person')>=1 and not store.rows()
    with pytest.raises(ValueError):LearningStore(root,0)


def test_logs_refuse_symlink_and_hardlink(root):
    store=LearningStore(root);store.directory.mkdir(parents=True)
    target=root/'sentinel';target.write_text('unchanged')
    path=store.directory/(datetime.now(timezone.utc).date().isoformat()+'.jsonl');path.symlink_to(target)
    with pytest.raises(ValueError):store.append('alias',{'alias':'x','target':'home'})
    path.unlink();path.hardlink_to(target)
    with pytest.raises(ValueError):store.append('alias',{'alias':'x','target':'home'})
    assert target.read_text()=='unchanged'


def test_alias_candidate_requires_real_approved_destination(root,names):
    with pytest.raises(ValueError):regression({'alias':'alien','target':'new_unmapped_place'},'alias',names,{},root=root,evaluate=evaluator)
    with pytest.raises(ValueError):validate_candidate('alias',{'alias':'x','target':'home','audio':'bad'})


def test_review_context_cannot_run_a_job(names):
    with pytest.raises(ValueError):Rehearsal(names,{'review_only':True})
    text='Go to cradle.';context={**names,'review_aliases':{'cradle':'home'}}
    result=understand(text,context,'Bharathwaj',settings={'backend':MockOfficeBackend([text]*4,[{}]*4)})
    assert result['decision']=='refuse' and result['refuse']['reason']=='invalid_context'


def test_person_erasure_removes_approved_data_and_private_history(root,names):
    store=LearningStore(root)
    row=store.append('alias',{'alias':'cradle','target':'home'},source='SCRIPTED',requester_id='private_person')
    report=regression(row['candidate'],'alias',names,{},root=root,evaluate=evaluator)
    accept(store,row,report,owner_confirmed=True)
    other=store.append('alias',{'alias':'othercradle','target':'home'},source='SCRIPTED',requester_id='another_person')
    other_report=regression(other['candidate'],'alias',names,{},root=root,evaluate=evaluator)
    accept(store,other,other_report,owner_confirmed=True)
    logs=root/'records/rehearsals';logs.mkdir()
    (logs/'example.json').write_text(json.dumps({'requester_id':'private_person','typed_text':'private'}))
    spec=importlib.util.spec_from_file_location('delete_person',Path('scripts/delete_person_records.py'))
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    tool.delete_person(root,'private_person')
    assert all(r.get('requester_id')!='private_person' for r in store.rows())
    assert load_aliases(root)=={'othercradle':'home'} and not list(logs.glob('*'))
    snapshots=list((root/'records/learning_versions').glob('*/learned_aliases.json'))
    assert len(snapshots)==1 and json.loads(snapshots[0].read_text())['aliases']=={'othercradle':'home'}


def test_console_feedback_callback_is_pending_not_an_owner_approval(root):
    from amr_language.office_learning import record_feedback
    before=(root/'data/owner_check.csv').read_bytes()
    row=record_feedback('Go home.','private_person','en','Go home. Is that OK?','yes',{'status':'went'},root=root)
    assert row['status']=='pending' and row['decisions']['requester_reply']=='yes'
    assert (root/'data/owner_check.csv').read_bytes()==before


def test_person_erasure_removes_real_map_suggestions_preserves_scripted(root):
    directory=root/'records/map_requests';directory.mkdir(parents=True)
    rows=[{'source':'UNREVIEWED_TYPED','who_asked':'private_person','person':'Karthik'},{'source':'SCRIPTED','who_asked':'private_person','person':'Karthik'}]
    path=directory/'example.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    spec=importlib.util.spec_from_file_location('delete_map_records',Path('scripts/delete_person_records.py'));tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    tool.delete_person(root,'private_person')
    assert [json.loads(l) for l in path.read_text().splitlines()]==[rows[1]]


def test_default_store_writes_where_the_records_override_points(tmp_path,monkeypatch):
    from amr_language import office_learning
    from amr_language.office_learning import record_feedback
    target=tmp_path/'elsewhere'
    monkeypatch.setenv('AMR_LANGUAGE_RECORDS_DIR',str(target))
    assert LearningStore().directory==target.resolve()/'learning_suggestions'
    row=record_feedback('Go home.','private_person','en','Go home. Is that OK?','yes',{'status':'went'})
    saved=[json.loads(line) for path in (target/'learning_suggestions').glob('*.jsonl') for line in path.read_text().splitlines()]
    assert [r['id'] for r in saved]==[row['id']]
    real=office_learning.ROOT/'records/learning_suggestions'
    assert all(row['id'] not in path.read_text() for path in real.glob('*.jsonl'))


def test_without_the_override_the_store_uses_the_module_records(monkeypatch):
    from amr_language import office_learning
    monkeypatch.delenv('AMR_LANGUAGE_RECORDS_DIR',raising=False)
    assert LearningStore().directory==office_learning.ROOT/'records/learning_suggestions'
    assert LearningStore('/tmp/explicit').directory==Path('/tmp/explicit').resolve()/'records/learning_suggestions'


def test_a_relative_records_override_is_refused(monkeypatch):
    monkeypatch.setenv('AMR_LANGUAGE_RECORDS_DIR','relative/records')
    with pytest.raises(ValueError):LearningStore()


def test_tests_never_write_into_the_module_records():
    import os
    from amr_language import office_learning
    override=Path(os.environ['AMR_LANGUAGE_RECORDS_DIR'])
    assert override.is_absolute() and not override.resolve().is_relative_to(office_learning.ROOT)
