"""Shared-load harness tests: fake console records/metrics, no real processes."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import pytest

spec=importlib.util.spec_from_file_location('shared_load',Path('scripts/check_shared_load.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


@pytest.fixture
def archive(tmp_path):return s.Archive(tmp_path/'records/shared_load_test',root=tmp_path)


@pytest.fixture
def console(tmp_path):
    root=tmp_path/'amr-console-dev';root.mkdir()
    return s.ConsoleRecords(root,trusted_roots=(root,))


def put(console,path,value):
    target=console.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value))
    return console.read(path)


def command(goal='cafeteria',lo=10.,hi=20.,**extra):
    row={'text':'go to the cafeteria' if goal=='cafeteria' else 'go home','parsed_json':{'location':goal},'command_map':'proposed_system',
      'nav2':{'status':'SUCCEEDED','navigation_wall_s':hi-lo},'true_final_error_m':.05,
      'truth_before':{'received_monotonic':lo,'source':'/amr/ground_truth','x':0.,'y':0.},
      'truth_after':{'received_monotonic':hi,'source':'/amr/ground_truth'},'transport':{'GZ_PARTITION':'test'},'map_sha256':'map','locations_sha256':'locations'}
    row.update(extra);return {'path':'command.json','sha256':'hash','record':row}


def session():
    return {'record':{'run_id':'run','action_id':'nav.start','params':{'map':'proposed_system'},'argv':['ground_truth:=true'],'env':{'GZ_PARTITION':'test'},'status':'running','end_at':None}}


def good_audit():
    a={'requested_stop':True,'clean':True,'forced':False,'bond_abort':False,'survivors':0,
      'audit':{'clean':True,'post_inventory_complete':True,'isolation_pass':True,'window_close_complete':True,'application_crashes':[],'abnormal_process_exits':[],'live_owned_survivors':[],'live_unowned_survivors':[]}}
    b={'clean':True,'forced':False,'bond_abort':False,'survivors':0,'simulation_lock_held':False}
    return {'cleanup.json':{'record':a},'cleanup_recheck.json':{'record':b}}


def test_read_only_console_allowlist_and_symlinks(console,tmp_path):
    for path in ('setup/run_console.sh','src/foo.py','data/runs/run/../../source.json','data/logs/console/run/ros_logs/something.log'):
        with pytest.raises(ValueError):console.read(path)
    put(console,'data/runs/run/commands/a.json',command()['record'])
    assert console.read('data/runs/run/commands/a.json')['record']['nav2']['navigation_wall_s']==10
    outside=tmp_path/'outside.json';outside.write_text('{}')
    (console.root/'data/runs/run/commands/link.json').symlink_to(outside)
    with pytest.raises(ValueError):console.read('data/runs/run/commands/link.json')
    with pytest.raises(ValueError):s.ConsoleRecords(tmp_path/'not-approved')
    with pytest.raises(ValueError):console.commands('../other')


@pytest.mark.parametrize('run_id',['../bad','/tmp/owned','run; kill -9 1',''])
def test_untrusted_run_ids_are_never_commands(console,run_id):
    with pytest.raises(ValueError):console.session(run_id)


def test_session_requires_active_correct_map_ground_truth(console):
    row=session()['record'];put(console,'data/logs/console/run/run.json',row)
    assert console.session('run')['record']['env']['GZ_PARTITION']=='test'
    for field,value in [('end_at',12),('argv',[]),('params',{'map':'wrong'})]:
        bad=deepcopy(row);bad[field]=value;put(console,'data/logs/console/run/run.json',bad)
        with pytest.raises(ValueError):console.session('run')


def test_new_records_only_and_no_stale_rewritten_commands():
    a=command();b=command('home',21,30)
    result=s.phase_navigation({'old':a},{'old':a,'new_a':a,'new_b':b},'test')
    assert result['status']=='measured' and len(result['commands'])==2
    assert s.phase_navigation({'old':a},{'old':command(hi=23),'home':b},'test')['status']=='not_measured'
    assert s.phase_navigation({}, {'a':a,'b':b,'duplicate':a},'test')['status']=='not_measured'
    assert s.phase_navigation({}, {'a':a,'b':b},'other')['status']=='not_measured'


@pytest.mark.parametrize('bad',[
 {'true_final_error_m':float('nan')},{'true_final_error_m':None},{'command_map':'different'},
 {'text':'go to storage'},{'nav2':{'status':'SUCCEEDED','navigation_wall_s':-1}}, {'truth_after':{}},
])
def test_invalid_navigation_measurements_do_not_become_zeros(bad):
    with pytest.raises((ValueError,TypeError)):s.parse_navigation(command(**bad))


def test_failed_navigation_status_is_reported_as_a_failure_not_missing():
    row=command(nav2={'status':'FAILED','navigation_wall_s':10.})
    assert s.parse_navigation(row)['success'] is False


@pytest.mark.parametrize('start,end,intervals,expected',[(0,10,[(0,10)],1),(0,10,[(0,4),(6,10)],.8),(0,10,[(0,8),(2,10)],1),(11,12,[(0,10)],0),(5,5,[(0,10)],0)])
def test_overlap_does_not_count_idle_gaps_or_duplicate_intervals(start,end,intervals,expected):
    assert s.overlap(start,end,intervals)==pytest.approx(expected)


def test_nvidia_compute_and_graphics_parsing_preserves_unavailable():
    assert s.parse_csv('123, llama-server, 3750\n',('pid','process_name','used_mib'))[0]['used_mib']==3750
    assert s.parse_csv('123, process, [N/A]\n',('pid','process_name','used_mib'))[0]['used_mib'] is None
    rows=s.parse_pmon('# gpu pid type fb ccpm command\n# Idx # C/G MB MB name\n0 123 C 3750 0 llama-server\n0 456 G 200 0 rviz2\n0 - - - - -\n')
    assert [p['type'] for p in rows]==['C','G']
    assert rows[1]['used_mib']==200
    with pytest.raises(ValueError):s.parse_csv('unexpected',('pid','name'))


def test_rtf_parser_and_nonfinite_values():
    assert s.parse_rtf('real_time_factor: 9.8e-1\nsim_time {}')==pytest.approx(.98)
    assert s.parse_rtf('time: 1') is None
    with pytest.raises(ValueError):s.parse_rtf('real_time_factor: 1e999')


def test_archive_cannot_write_console_or_traverse(archive,tmp_path):
    with pytest.raises(ValueError):s.Archive(tmp_path/'amr-console-dev/data/runs',root=tmp_path)
    with pytest.raises(ValueError):archive.write('../../amr-console-dev/source.py',{})
    path=archive.path/'link.json';path.symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError):archive.write('link.json',{})
    with pytest.raises(FileExistsError):s.Archive(archive.path,root=tmp_path)


@pytest.mark.parametrize('change',[lambda a:a['cleanup.json'].update(stale=True),lambda a:a['cleanup.json']['record'].update(forced=True),lambda a:a['cleanup.json']['record']['audit'].update(clean=False),lambda a:a['cleanup_recheck.json']['record'].update(survivors=1),lambda a:a['cleanup_recheck.json']['record'].update(simulation_lock_held=True)])
def test_cleanup_recheck_cannot_hide_initial_failure(change):
    audit=good_audit();assert s.cleanup_pass(audit) is True
    change(audit);assert s.cleanup_pass(audit) is not True


def test_cleanup_requires_new_both_records(console):
    audit=good_audit()
    for name,value in audit.items():put(console,'data/logs/console/run/'+name,value['record'])
    old=console.cleanup('run',{});before={k:v['sha256'] for k,v in old.items()}
    assert s.cleanup_pass(console.cleanup('run',before)) is None


def metrics(archive,phase='P2',allocation=(100,100)):
    row={'monotonic_s':11.,'gpu':[{'gpu_index':0,'uuid':'testgpu','total_mib':8188,'used_mib':7000}],
      'api_ps':{'models':[{'name':'qwen3.5:4b-q4_K_M','size':allocation[0],'size_vram':allocation[1]}]},
      'ram_total_kib':10000,'ram_available_kib':2000,'cpu_percent_host':10.,'tracked_pids':{'C_worker':42},
      'processes':[{'pid':42,'process_name':'python','rss_kib':200,'cpu_percent_one_core':200.}],
      'compute':[{'pid':8,'process_name':'llama-server','used_mib':3750}]}
    archive.append(phase+'/resources.jsonl',row)
    archive.append(phase+'/graphics_processes.jsonl',{'processes':[{'pid':8,'process_name':'llama-server','used_mib':3750},{'pid':9,'process_name':'rviz2','used_mib':250}]})
    return row


def test_ps_every_sample_and_process_memory_not_double_counted(archive):
    row=metrics(archive);data={'status':'measured','requests':[{'status':'measured','wall_ms':4000.,'shared_measured':True}]}
    out=s.phase_summary(archive,'P2',data)
    assert out['B_fully_GPU'] is True and out['C_process_ram_peak_kib']==200
    assert len([p for p in out['processes'] if p['pid']==8])==1
    assert next(p for p in out['processes'] if p['pid']==8)['gpu_peak_mib']==3750
    assert out['within_5s_shared']=={'count':1,'denominator':1}
    row['api_ps']['models'][0]['size_vram']=99;archive.append('P2/resources.jsonl',row)
    assert s.phase_summary(archive,'P2',data)['B_fully_GPU'] is False


def test_missing_ps_and_gpu_memory_are_not_passes(archive):
    row=metrics(archive);row.pop('api_ps');row['api_ps_error']='timed out';archive.append('P2/resources.jsonl',row)
    assert s.phase_summary(archive,'P2',{})['B_fully_GPU'] is None
    empty=s.phase_summary(archive,'P1',{})
    assert empty['gpu_below_7_5_GB'] is None and empty['latency_ms_shared']['p50'] is None


def test_navigation_thresholds_and_missing_comparability():
    a={'navigation':{'commands':[s.parse_navigation(command()),s.parse_navigation(command('home',21,31))]}}
    b=deepcopy(a);b['navigation']['commands'][0]['navigation_wall_s']=12
    b['navigation']['commands'][1]['true_final_error_m']=.2
    out=s.compare_navigation(a,b)
    assert out[0]['time_pass'] is False and out[1]['error_pass'] is False
    b['navigation']['commands'][0]['map_sha256']='other'
    assert s.compare_navigation(a,b)[0]['same_map_and_locations'] is False


def test_unrun_report_has_no_invented_phase_numbers(archive,tmp_path,monkeypatch):
    archive.write('phases.json',{p:{'status':'not_measured'} for p in s.PHASES})
    old=tmp_path/'baseline.jsonl';old.write_text(json.dumps({'id':'STARTER-en-01','status':'measured','request_wall_ms':1000.})+'\n')
    summary=s.build_summary(archive,old)
    assert not summary['complete_shared_test'] and all(v is None for v in summary['criteria'].values())
    assert summary['same_console_session_settings_and_commit'] is None
    assert summary['phases']['P2']['requests_not_attempted']==20
    monkeypatch.setattr(s,'ROOT',tmp_path)
    text=s.render_report(summary,archive)
    assert 'not measured' in text and 'No safe-sharing conclusion' in text
    assert summary['standalone_baseline']['same_ids']['n']==0


def test_only_liveness_gated_requests_count_and_recovery_is_owned(monkeypatch):
    import amr_language
    monkeypatch.setattr(amr_language,'understand',lambda *a,**k:{'decision':'ask','refuse':None})
    runner=SimpleNamespace(probe=lambda **k:{'ok':True},restart=lambda _:(_ for _ in ()).throw(AssertionError('No restart on successful inference')))
    row={'id':'STARTER-en-01','language':'en','text':'hello'}
    assert s.measure_request(row,{}, {},runner,None)['status']=='measured'
    count=[];runner.probe=lambda **k:{'ok':False};runner.restart=lambda reason:count.append(reason)
    assert s.measure_request(row,{}, {},runner,None)['status']=='not_measured' and len(count)==1


def test_phase_logic_uses_fresh_commands_and_marks_idle_tails(archive,monkeypatch):
    fake=SimpleNamespace(commands=lambda run:{},session=lambda run:session())
    calls=[{}, {'a':command(),'b':command('home',21,30)}]
    fake.commands=lambda run:calls.pop(0)
    monitor=SimpleNamespace(rtf=[],start=lambda:monitor,stop=lambda:{'samples':2},track=lambda *a:None)
    monkeypatch.setattr(s,'Sampler',lambda *a,**k:monitor)
    monkeypatch.setattr(s,'load_cases',lambda:[{'id':'a'},{'id':'b'}])
    def measure(row,*args):
        return {'id':row['id'],'status':'measured','start':12. if row['id']=='a' else 31.,'end':13. if row['id']=='a' else 32.,'wall_ms':1000.}
    monkeypatch.setattr(s,'measure_request',measure)
    answers=iter(['','','','1.0, .98, .99'])
    campaign=s.Campaign(archive,fake,input_fn=lambda _:next(answers),output_fn=lambda _:None)
    campaign.navigation('P2',session(),True)
    rows=campaign.phases['P2']['requests']
    assert [r['shared_measured'] for r in rows]==[True,False]
    assert campaign.phases['P2']['rtf']['source']=='owner_typed_during_navigation'


def test_script_does_not_expose_dispatch_or_console_lifecycle_commands():
    import ast
    tree=ast.parse(Path('scripts/check_shared_load.py').read_text())
    attrs={n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)}
    assert not attrs.intersection({'kill','killpg','send_signal','terminate','Popen'})
    assert 'dispatch' not in attrs
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='CWorker']
    assert len(calls)==1 and calls[0].args[0].value=='cpu'
    assert len(s.SELECTED)==20 and all(sum(f'-{lang}-' in i for i in s.SELECTED)==5 for lang in ('en','ta','ml','te'))


@pytest.mark.parametrize('already_done',[False,True])
def test_stop_phase_requires_pending_B_and_normal_cleanup(archive,monkeypatch,already_done):
    import amr_language
    release=threading.Event();audit=good_audit();seen=[]
    def understand(*args,**kwargs):
        if not already_done:assert release.wait(timeout=3)
        return {'decision':'act','refuse':None}
    monkeypatch.setattr(amr_language,'understand',understand)
    probe_started=threading.Event();probe_calls=[]
    def probe(*a,**k):
        probe_calls.append(1)
        if already_done and len(probe_calls)==2:
            probe_started.set();assert release.wait(timeout=3)
        return {'ok':True}
    monkeypatch.setattr(s.time,'sleep',lambda _:probe_started.wait(timeout=3) if already_done else None)
    monitor=SimpleNamespace(start=lambda:monitor,stop=lambda:{'samples':2})
    monkeypatch.setattr(s,'Sampler',lambda *a,**k:monitor)
    monkeypatch.setattr(s,'load_cases',lambda:[{'id':'STARTER-ta-10','text':'long','language':'ta'},{'id':'STARTER-en-01','text':'short','language':'en'}])
    following={'status':'measured','attempts':[{'result':{'decision':'act'}}]}
    monkeypatch.setattr(s,'measure_request',lambda *a,**k:following)
    fake=SimpleNamespace(session=lambda *a,**k:session(),cleanup=lambda *a,**k:audit)
    def input_fn(prompt):
        seen.append(prompt)
        if 'cleanup has completed' in prompt:release.set()
        return ''
    campaign=s.Campaign(archive,fake,input_fn=input_fn,output_fn=lambda _:None)
    campaign.runner=SimpleNamespace(probe=probe,restarts=0)
    campaign.settings={};campaign.names={}
    campaign.stop_check(session())
    result=campaign.phases['P3']
    assert result['next_request_works'] and result['cleanup_pass']
    if not already_done:assert result['pass'] is True
    else:assert result['pass'] is None and result['status']=='not_measured'
    assert any('STOP clicked' in x for x in seen)


def test_sampler_uses_only_read_clients_and_no_real_processes(archive,monkeypatch):
    monitor=s.Sampler(archive,'P2',api_ps=False)
    called=[]
    def read(argv,**kwargs):
        called.append(argv)
        if '--query-compute-apps=pid,process_name,used_gpu_memory' in argv:stdout='8, llama-server, 3750\n'
        else:stdout='0, GPU-test, 8188, 7000, 1188, 50\n'
        return {'returncode':0,'stdout':stdout}
    monkeypatch.setattr(s,'read_command',read)
    def proc(previous,now,tracked):
        monitor.stop_event.set()
        return {'ram_total_kib':100,'ram_available_kib':50,'cpu_percent_host':5,'processes':[]},{}
    monkeypatch.setattr(s,'proc_sample',proc)
    monitor.fast()
    assert len(monitor.rows)==1 and all(c[0]=='nvidia-smi' for c in called)
    assert monitor.rows[0]['gpu'][0]['used_mib']==7000


def test_report_ratio_uses_matching_shared_ids_only(archive,tmp_path):
    metrics(archive)
    nav={'status':'measured','commands':[s.parse_navigation(command()),s.parse_navigation(command('home',21,30))]}
    phases={p:{'status':'not_measured'} for p in s.PHASES}
    phases['P1']={'status':'measured','navigation':nav,'rtf':{'values':[1.],'source':'owner_typed_during_navigation'}}
    phases['P2']={'status':'measured','navigation':nav,'rtf':{'values':[.95],'source':'owner_typed_during_navigation'},'requests':[
      {'id':'shared','status':'measured','shared_measured':True,'wall_ms':2000},
      {'id':'idle','status':'measured','shared_measured':False,'wall_ms':100000},
      {'id':'missing','status':'not_measured','wall_ms':None}]}
    archive.write('phases.json',phases)
    baseline=tmp_path/'baseline.jsonl';baseline.write_text(''.join(json.dumps(r)+'\n' for r in [
      {'id':'shared','status':'measured','request_wall_ms':1000}, {'id':'idle','status':'measured','request_wall_ms':1000}]))
    result=s.build_summary(archive,baseline)
    assert result['standalone_baseline']['P2_over_standalone']=={'p50':2.,'p95':2.}
    assert result['phases']['P2']['within_20s_shared']=={'count':1,'denominator':1}
    assert result['phases']['P2']['requests_not_measured']==1
    assert not result['complete_shared_test'] # One shared case, not all twenty; STOP missing.


def test_case_selection_counts_all_70_starters_including_mixed_not_blank_owner_rows():
    cases=s.load_cases()
    assert len(cases)==20 and {r['id'] for r in cases}==set(s.SELECTED)
    assert all(r['speaker']=='STARTER' for r in cases)


def test_prepare_only_never_starts_clients_or_models(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'ROOT',tmp_path)
    (tmp_path/'docs').mkdir();(tmp_path/'data').mkdir();(tmp_path/'data/owner_check.csv').write_text('FAKE CORPUS')
    original_archive=s.Archive;original_summary=s.build_summary
    monkeypatch.setattr(s,'Archive',lambda path,create=True:original_archive(path,root=tmp_path,create=create))
    monkeypatch.setattr(s,'frozen_manifest',lambda:{'frozen':'hash'})
    monkeypatch.setattr(s,'load_cases',lambda:[{'id':i,'source':'FAKE'} for i in s.SELECTED])
    monkeypatch.setattr(s.Campaign,'run',lambda _:(_ for _ in ()).throw(AssertionError('Prepare must not run phases')))
    monkeypatch.setattr(s,'read_command',lambda *a,**k:(_ for _ in ()).throw(AssertionError('No clients in prepare')))
    baseline=tmp_path/'baseline.jsonl';baseline.write_text('')
    monkeypatch.setattr(s,'build_summary',lambda archive:original_summary(archive,baseline))
    directory=tmp_path/'records/shared_load_test'
    assert s.main(['--prepare-only','--output',str(directory)])==0
    metadata=json.loads((directory/'metadata.json').read_text())
    assert metadata.get('owner_run_started_at') is None
    assert all(p['status']=='not_measured' for p in json.loads((directory/'phases.json').read_text()).values())
    assert not list(directory.glob('*/resources.jsonl'))
    assert 'not measured' in (tmp_path/'docs/SHARED_LOAD_CHECK.md').read_text()


def test_reusing_started_campaign_is_refused_before_any_process(archive,monkeypatch):
    archive.write('metadata.json',{'owner_run_started_at':'already started'})
    archive.write('phases.json',{p:{'status':'not_measured'} for p in s.PHASES})
    monkeypatch.setattr(s,'frozen_manifest',lambda:{})
    monkeypatch.setattr(s,'Archive',lambda *a,**k:archive)
    with pytest.raises(ValueError,match='already started'):s.main(['--run',str(archive.path)])


def test_twenty_requests_are_released_in_two_legs(archive,monkeypatch):
    calls=[{}, {'a':command(),'b':command('home',21,30)}]
    console=SimpleNamespace(commands=lambda _:calls.pop(0))
    monitor=SimpleNamespace(rtf=[],start=lambda:monitor,stop=lambda:{'samples':2},track=lambda *a:None)
    monkeypatch.setattr(s,'Sampler',lambda *a,**k:monitor)
    monkeypatch.setattr(s,'load_cases',lambda:[{'id':i} for i in range(20)])
    home_started=threading.Event()
    def measure(row,*args):
        if row['id']>=10:assert home_started.is_set()
        return {'id':row['id'],'status':'measured','start':12. if row['id']<10 else 22.,'end':13. if row['id']<10 else 23.,'wall_ms':1000.}
    monkeypatch.setattr(s,'measure_request',measure)
    def input_fn(prompt):
        if 'returning' in prompt:home_started.set()
        return ''
    campaign=s.Campaign(archive,console,input_fn=input_fn,output_fn=lambda _:None)
    campaign.navigation('P2',session(),True)
    assert len(campaign.phases['P2']['requests'])==20 and all(r['shared_measured'] for r in campaign.phases['P2']['requests'])


def test_failed_goal_is_not_hidden_by_missing_final_ground_truth(archive,tmp_path):
    failed=command(nav2={'status':'ABORTED','navigation_wall_s':10},true_final_error_m=None)
    nav=s.phase_navigation({}, {'failed':failed,'home':command('home',21,30)},'test')
    assert nav['status']=='not_measured' and nav['failures'][0]['status']=='ABORTED'
    assert nav['errors'][0]['evidence']==failed
    phases={p:{'status':'not_measured'} for p in s.PHASES};phases['P2']={'status':'not_measured','navigation':nav}
    archive.write('phases.json',phases);baseline=tmp_path/'baseline.jsonl';baseline.write_text('')
    summary=s.build_summary(archive,baseline)
    assert summary['criteria']['navigation_time'] is False
    assert summary['criteria']['final_error_change'] is None


def test_confirmation_does_not_consume_navigation_text(archive):
    answers=iter(['go to the cafeteria','go home','']);messages=[]
    campaign=s.Campaign(archive,None,input_fn=lambda _:next(answers),output_fn=messages.append)
    campaign.wait_enter('Wait until both finish> ')
    assert len(messages)==2 and all('Sampling has not advanced' in line for line in messages)
    answers=iter(['go home','skip']);campaign.input=lambda _:next(answers)
    assert campaign.enter('Optional phase') is False


def test_p1_continuation_preserves_baseline_and_refuses_post_B(archive):
    manifest={'frozen':'hash'}
    archive.write('metadata.json',{'frozen_manifest':manifest})
    phases={p:{'status':'not_measured'} for p in s.PHASES};phases['P0']={'status':'measured','sampler':{'samples':1}}
    archive.write('phases.json',phases);archive.append('P0/resources.jsonl',{'source':'FAKE'});archive.append('P0/graphics_processes.jsonl',{'source':'FAKE'})
    archive.append('P1/resources.jsonl',{'invalid_phase':'FAKE'})
    before={str(p.relative_to(archive.path)):p.read_bytes() for p in archive.path.rglob('*') if p.is_file()}
    checkpoint=s.continuation_checkpoint(archive,manifest)
    assert checkpoint['phase']==phases['P0'] and set(checkpoint['raw_sha256'])=={'P0/resources.jsonl','P0/graphics_processes.jsonl'}
    assert before=={str(p.relative_to(archive.path)):p.read_bytes() for p in archive.path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError):s.continuation_checkpoint(archive,{'different':'hash'})
    archive.write('B_startup.json',{'source':'FAKE'})
    with pytest.raises(ValueError,match='pre-B'):s.continuation_checkpoint(archive,manifest)


def test_continuation_cli_copies_only_P0_and_skips_baseline(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'ROOT',tmp_path);(tmp_path/'docs').mkdir();(tmp_path/'data').mkdir();(tmp_path/'data/owner_check.csv').write_text('FAKE CORPUS')
    archive_class=s.Archive;original_summary=s.build_summary
    old=archive_class(tmp_path/'records/shared_load_old',root=tmp_path)
    old.write('metadata.json',{'frozen_manifest':{'frozen':'hash'}})
    phases={p:{'status':'not_measured'} for p in s.PHASES};phases['P0']={'status':'measured','sampler':{'samples':1}}
    old.write('phases.json',phases);old.append('P0/resources.jsonl',{'monotonic_s':1.,'source':'FAKE'});old.append('P0/graphics_processes.jsonl',{'source':'FAKE'})
    old.append('P1/resources.jsonl',{'invalid':'FAKE'})
    before={str(p):p.read_bytes() for p in old.path.rglob('*') if p.is_file()}
    monkeypatch.setattr(s,'Archive',lambda path,create=True:archive_class(path,root=tmp_path,create=create))
    monkeypatch.setattr(s,'frozen_manifest',lambda:{'frozen':'hash'});monkeypatch.setattr(s,'load_cases',lambda:[])
    monkeypatch.setattr(s,'ConsoleRecords',lambda:None)
    seen=[]
    def run(self,skip_baseline=False):seen.append((skip_baseline,self.phases['P0']))
    monkeypatch.setattr(s.Campaign,'run',run)
    baseline=tmp_path/'baseline.jsonl';baseline.write_text('');monkeypatch.setattr(s,'build_summary',lambda archive:original_summary(archive,baseline))
    new=tmp_path/'records/shared_load_retry'
    assert s.main(['--continue-p1',str(old.path),'--output',str(new)])==0
    assert seen==[(True,phases['P0'])]
    assert (new/'P0/resources.jsonl').read_bytes()==old.target('P0/resources.jsonl').read_bytes()
    assert not (new/'P1/resources.jsonl').exists()
    assert before=={str(p):p.read_bytes() for p in old.path.rglob('*') if p.is_file()}
    assert json.loads((new/'metadata.json').read_text())['P0_continuation']['source_archive']==str(old.path)


def test_terminal_entrypoint_bootstraps_local_package_without_install(monkeypatch):
    import runpy,sys,importlib.machinery
    root=Path('scripts/check_shared_load.py').resolve().parent.parent
    paths=[str(root/'scripts')]+[p for p in sys.path if Path(p or '.').resolve()!=root]
    monkeypatch.setattr(sys,'path',paths)
    assert importlib.machinery.PathFinder.find_spec('amr_language',sys.path) is None
    namespace=runpy.run_path(str(root/'scripts/check_shared_load.py'),run_name='shared_direct_entry_test')
    found=importlib.machinery.PathFinder.find_spec('amr_language',sys.path)
    assert found is not None and Path(found.origin)==root/'amr_language/__init__.py'
    assert namespace['ROOT']==root


def test_p2_continuation_carries_completed_navigation_without_model_start(archive):
    manifest={'frozen':'hash'};archive.write('metadata.json',{'frozen_manifest':manifest})
    phases={p:{'status':'not_measured'} for p in s.PHASES}
    phases['P0']={'status':'measured'};phases['P1']={'status':'measured','navigation':{'status':'measured','commands':[]}}
    archive.write('phases.json',phases)
    for phase in ('P0','P1'):
        archive.append(phase+'/resources.jsonl',{'source':'FAKE'});archive.append(phase+'/graphics_processes.jsonl',{'source':'FAKE'})
    archive.write('P1/phase.json',phases['P1'])
    checkpoint=s.continuation_checkpoint(archive,manifest,'P2')
    assert set(checkpoint['completed_phases'])=={'P0','P1'} and checkpoint['next_phase']=='P2'
    assert 'P1/phase.json' in checkpoint['raw_sha256']
    archive.write('B_startup.json',{'source':'FAKE'})
    with pytest.raises(ValueError):s.continuation_checkpoint(archive,manifest,'P2')


def test_p2_continuation_cli_reuses_both_baselines(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'ROOT',tmp_path);(tmp_path/'docs').mkdir();(tmp_path/'data').mkdir();(tmp_path/'data/owner_check.csv').write_text('FAKE CORPUS')
    archive_class=s.Archive;original_summary=s.build_summary
    old=archive_class(tmp_path/'records/shared_load_old',root=tmp_path)
    old.write('metadata.json',{'frozen_manifest':{'frozen':'hash'}})
    phases={p:{'status':'not_measured'} for p in s.PHASES}
    phases['P0']={'status':'measured'};phases['P1']={'status':'measured','navigation':{'commands':[]}}
    old.write('phases.json',phases)
    for phase in ('P0','P1'):
        old.append(phase+'/resources.jsonl',{'monotonic_s':1.,'source':'FAKE'});old.append(phase+'/graphics_processes.jsonl',{'source':'FAKE'})
    old.write('P1/phase.json',phases['P1'])
    monkeypatch.setattr(s,'Archive',lambda path,create=True:archive_class(path,root=tmp_path,create=create))
    monkeypatch.setattr(s,'frozen_manifest',lambda:{'frozen':'hash'});monkeypatch.setattr(s,'load_cases',lambda:[]);monkeypatch.setattr(s,'ConsoleRecords',lambda:None)
    calls=[]
    monkeypatch.setattr(s.Campaign,'run',lambda self,**options:calls.append((options,self.phases)))
    baseline=tmp_path/'baseline.jsonl';baseline.write_text('');monkeypatch.setattr(s,'build_summary',lambda archive:original_summary(archive,baseline))
    new=tmp_path/'records/shared_load_p2_retry'
    assert s.main(['--continue-p2',str(old.path),'--output',str(new)])==0
    assert calls[0][0]=={'skip_baseline':True,'skip_navigation':True}
    assert calls[0][1]['P1']==phases['P1'] and (new/'P1/phase.json').read_bytes()==old.target('P1/phase.json').read_bytes()
    assert not (new/'P2').exists()


def test_local_measurement_reaches_frozen_pipeline_without_backend_injection(names,monkeypatch):
    from amr_language import office_pipeline
    from amr_language.office_backend import LocalOfficeBackend,MockOfficeBackend,load_office_profile
    local=LocalOfficeBackend.__new__(LocalOfficeBackend) # Inert identity, no hash/network/process.
    mock=MockOfficeBackend(['Go to reception.']*4,[{}]*4)
    seen=[]
    def resolve(config):
        assert config.get('backend') is None
        seen.append(config);return mock
    monkeypatch.setattr(office_pipeline,'make_backend',resolve)
    runner=SimpleNamespace(probe=lambda **k:{'ok':True},restart=lambda _:pytest.fail('Successful pipeline must not restart B'))
    settings={**load_office_profile(),'mode':'measurement','input_source':'STARTER_MEASUREMENT','capability_profile':names['robot_capability']}
    result=s.measure_request({'id':'FAKE','language':'en','text':'Go to reception.'},names,settings,runner,local)
    assert result['status']=='measured' and seen and mock.calls
    attempt=result['attempts'][-1]
    assert attempt['result']['decision']=='act' and attempt['trace']


def test_cross_system_adapter_is_preserved_by_measurement_settings():
    from amr_language.office_backend import CrossSystemOfficeBackend,settings_for_office
    adapter=CrossSystemOfficeBackend.__new__(CrossSystemOfficeBackend) # No C worker/model startup.
    trace=[]
    config=s.request_settings({'draw_policy':'cross_system'},adapter,trace)
    assert config['backend'] is adapter and config['trace'] is trace
    assert settings_for_office(config)['backend'] is adapter


def test_invalid_context_is_excluded_without_restarting_model(monkeypatch):
    import amr_language
    monkeypatch.setattr(amr_language,'understand',lambda *a,**k:{'decision':'refuse','refuse':{'reason':'invalid_context'}})
    runner=SimpleNamespace(probe=lambda **k:{'ok':True},restart=lambda _:pytest.fail('Context failure cannot be repaired by model restart'))
    result=s.measure_request({'id':'FAKE','language':'en','text':'Go home.'},{},{},runner,None)
    assert result['status']=='not_measured' and len(result['attempts'])==1 and result['wall_ms'] is None
