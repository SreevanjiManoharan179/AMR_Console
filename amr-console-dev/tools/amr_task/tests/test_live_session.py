import json

import pytest
import yaml

from tools.amr_task.live_session import ApprovalGate, SessionAttachment, process_transport
from tools.amr_task.tags import Tags


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def session(tmp_path):
    path = tmp_path/'data/logs/console/test_run'
    path.mkdir(parents=True)
    env = {'ROS_DOMAIN_ID': '80', 'GZ_PARTITION': 'amr_console_navigation_80'}
    run = dict(run_id='test_run', action_id='nav.start', status='running', end_at=None,
               params={'map': 'proposed_system', 'controller': 'mppi'}, env=env)
    spec = dict(run_id='test_run', kind='navigation', map_id='proposed_system', env=env)
    processes = [dict(pid=i+10, start_ticks=100+i, comm=name) for i, name in enumerate(
        ['map_server', 'amcl', 'bt_navigator', 'controller_serv', 'launch leader'])]
    verification = dict(ok=True, expected=env, processes=processes)
    for name, data in [('run', run), ('spec', spec), ('transport_verification', verification)]:
        write(path/(name+'.json'), data)
    def reader(pid): return (100+pid-10, env)
    return tmp_path, path, run, spec, verification, reader


def test_attach_and_recheck_existing_transport(session, monkeypatch):
    root, path, run, spec, v, reader = session
    a = SessionAttachment('test_run', root, reader)
    monkeypatch.setenv('ROS_DOMAIN_ID', '73')
    monkeypatch.setenv('GZ_PARTITION', 'old')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    monkeypatch.setenv('RMW_IMPLEMENTATION', 'old')
    a.configure_environment()
    import os
    assert os.environ['ROS_DOMAIN_ID'] == '80'
    assert os.environ['GZ_PARTITION'] == v['expected']['GZ_PARTITION']
    assert 'ROS_LOCALHOST_ONLY' not in os.environ
    run['status'] = 'stopping'; write(path/'run.json', run)
    with pytest.raises(RuntimeError): a.check()


@pytest.mark.parametrize('mutation', ['stopped', 'mapping', 'wrong_map', 'stage1', 'busy', 'transport', 'unverified', 'missing_process'])
def test_stale_or_wrong_session_refused(session, mutation):
    root, path, run, spec, v, reader = session
    if mutation == 'stopped': run['end_at'] = 123
    if mutation == 'mapping': spec['kind'] = 'mapping'
    if mutation == 'wrong_map': spec['map_id'] = 'office_v1'
    if mutation == 'stage1': run['action_id'] = 'nav.stage1'
    if mutation == 'busy': run['operations'] = [{'end_at': None}]
    if mutation == 'transport': spec['env'] = dict(spec['env'], ROS_DOMAIN_ID='73')
    if mutation == 'unverified': v['ok'] = False
    if mutation == 'missing_process': v['processes'] = v['processes'][1:]
    for name, data in [('run', run), ('spec', spec), ('transport_verification', v)]: write(path/(name+'.json'), data)
    with pytest.raises(RuntimeError): SessionAttachment('test_run', root, reader)


def test_pid_reuse_and_session_path_refused(session):
    root, path, run, spec, v, reader = session
    with pytest.raises(RuntimeError): SessionAttachment('test_run', root, lambda pid: (999, v['expected']))
    with pytest.raises(ValueError): SessionAttachment('../test_run', root, reader)


def test_process_reader_retains_only_transport(tmp_path):
    proc = tmp_path/'44'; proc.mkdir()
    fields = ['S'] + ['0']*18 + ['1234']
    (proc/'stat').write_text('44 (test process) '+' '.join(fields))
    (proc/'environ').write_bytes(b'ROS_DOMAIN_ID=80\0GZ_PARTITION=test\0UNRELATED_PRIVATE_FIELD=do_not_return\0')
    assert process_transport(44, tmp_path) == (1234, {'ROS_DOMAIN_ID': '80', 'GZ_PARTITION': 'test'})


def make_gate(root):
    return ApprovalGate(Tags(root/'config/tags_proposed_system.yaml'), root,
                        root/'config/calibration/proposed_system/tasks/results.json')


@pytest.mark.parametrize('which', ['tags', 'locations', 'console'])
def test_each_approval_required(live_repo, which):
    paths = {'tags': 'config/tags_proposed_system.yaml', 'locations': 'src/amr_physical_ai/config/named_locations_proposed_system.yaml',
             'console': 'tools/amr_console/approvals.yaml'}
    path = live_repo/paths[which]
    data = yaml.safe_load(path.read_text())
    data['proposed_system_places' if which == 'console' else 'approval_status'] = False if which == 'console' else 'proposed'
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    with pytest.raises(RuntimeError, match='approval'): make_gate(live_repo)


@pytest.mark.parametrize('which', ['tags', 'map', 'locations', 'threshold', 'console'])
def test_gate_rechecked_after_preview(live_repo, which):
    gate = make_gate(live_repo)
    paths = {'tags': 'config/tags_proposed_system.yaml', 'map': 'src/amr_physical_ai/maps/proposed_system.yaml',
             'locations': 'src/amr_physical_ai/config/named_locations_proposed_system.yaml',
             'threshold': 'config/calibration/proposed_system/tasks/results.json',
             'console': 'tools/amr_console/approvals.yaml'}
    path = live_repo/paths[which]
    path.write_text('proposed_system_places: false\n' if which == 'console' else path.read_text()+'\n')
    with pytest.raises(RuntimeError): gate.check()


@pytest.mark.parametrize('key,value', [('tag_semantics_sha256', 'stale'), ('aggregation', 'min')])
def test_semantically_stale_threshold_refused(live_repo, key, value):
    path = live_repo/'config/calibration/proposed_system/tasks/results.json'
    result = json.loads(path.read_text()); result['meta'][key] = value; write(path, result)
    with pytest.raises(RuntimeError, match='calibration'): make_gate(live_repo)


def test_runtime_module_import_has_no_ros_side_effects():
    import subprocess
    code = "import sys; import tools.amr_task.run_live; assert 'rclpy' not in sys.modules"
    subprocess.run([__import__('sys').executable, '-c', code], check=True)
