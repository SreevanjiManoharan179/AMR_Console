"""Isolation inventory and transport regressions, without ROS or Gazebo."""
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transport_isolation as isolation


def record(pid, ticks=100, state='S'):
    return {'pid': pid, 'start_ticks': ticks, 'state': state,
            'ppid': 1, 'pgid': pid, 'session': pid}


def owner(records, leader=42):
    return SimpleNamespace(records=records, proc=SimpleNamespace(pid=leader),
                           refresh=lambda: None, lock=threading.Lock())


def snapshot(processes=(), ports=()):
    return {'processes': list(processes), 'udp_ports': list(ports),
            'inaccessible_environment_pids': [], 'scope': 'test namespace'}


@pytest.mark.parametrize(('comm', 'argv'), [
    ('ruby', ['gz sim -r world.sdf']),
    ('ruby', ['/usr/bin/ruby', '/opt/ros/jazzy/opt/gz_tools_vendor/bin/gz', 'sim', '-s']),
    ('gz', ['gz', 'sim', '-g']),
    ('gz-sim8-server', ['/usr/bin/gz-sim8-server', 'world.sdf']),
    ('map_server', ['/opt/ros/jazzy/lib/nav2_map_server/map_server']),
    ('robot_state_pub', ['/opt/ros/jazzy/lib/robot_state_publisher/robot_state_publisher']),
    ('parameter_bridg', ['/opt/ros/jazzy/lib/ros_gz_bridge/parameter_bridge']),
    ('component_conta', ['/opt/ros/jazzy/lib/rclcpp_components/component_container_isolated']),
    ('lifecycle_manag', ['/opt/ros/jazzy/lib/nav2_lifecycle_manager/lifecycle_manager']),
    ('python3', ['/usr/bin/python3', '/opt/ros/jazzy/lib/nav2_velocity_smoother/velocity_smoother']),
    ('rviz2', ['/opt/ros/jazzy/lib/rviz2/rviz2']),
])
def test_relevant_processes_including_hidden_gazebo(comm, argv):
    assert isolation.is_relevant(comm, argv)


@pytest.mark.parametrize(('comm', 'argv'), [
    ('ruby', ['ruby', '/home/user/app.rb']),
    ('python3', ['python3', '-c', 'print("gz sim map_server rviz2")']),
    ('bash', ['bash', '-c', 'cat /tmp/rviz2.log']),
    ('code', ['/usr/bin/code', 'map_server']),
])
def test_prose_arguments_and_unrelated_ruby_are_not_processes(comm, argv):
    assert not isolation.is_relevant(comm, argv)


def test_inventory_includes_unowned_and_rejects_reused_identity(monkeypatch):
    table = {42: record(42, 200), 43: record(43), 44: record(44, state='Z')}
    monkeypatch.setattr(isolation, 'process_table', lambda: table)
    monkeypatch.setattr(isolation, '_metadata', lambda pid: ('rviz2', ['rviz2']))
    monkeypatch.setattr(isolation, '_identity', lambda pid: table[pid]['start_ticks'])
    result = isolation.process_inventory(owner({42: record(42, 100), 43: record(43)}))
    assert [(item['pid'], item['owned']) for item in result] == [(42, False), (43, True)]


def test_inventory_omits_process_exited_during_read(monkeypatch):
    monkeypatch.setattr(isolation, 'process_table', lambda: {42: record(42)})
    monkeypatch.setattr(isolation, '_metadata', lambda pid: ('rviz2', ['rviz2']))
    monkeypatch.setattr(isolation, '_identity', lambda pid: None)
    assert isolation.process_inventory() == []


def test_environment_reader_retains_only_allowlisted_keys(monkeypatch, tmp_path):
    proc = tmp_path / '42'
    proc.mkdir()
    (proc / 'environ').write_bytes(b'SECRET=never-retained\0ROS_DOMAIN_ID=21\0'
                                 b'GZ_PARTITION=example\0ROS_LOCALHOST_ONLY=1\0')
    monkeypatch.setattr(isolation, 'PROC', tmp_path)
    assert isolation._transport_environment(42) == {
        'ROS_DOMAIN_ID': '21', 'GZ_PARTITION': 'example', 'ROS_LOCALHOST_ONLY': '1'}


def test_allocate_sixty_domains_without_reuse_or_partition_collision(monkeypatch):
    monkeypatch.setattr(isolation, 'transport_snapshot', lambda: snapshot())
    used, partitions = set(), set()
    for ordinal in range(1, 61):
        result = isolation.allocate_transport('run_20261003_100000', 'objective1', ordinal, used)
        domain = int(result['ROS_DOMAIN_ID'])
        assert 20 <= domain <= 99
        assert domain not in used
        assert result['GZ_PARTITION'] not in partitions
        used.add(domain)
        partitions.add(result['GZ_PARTITION'])
    assert len(used) == len(partitions) == 60


def test_partition_names_cannot_alias_after_sanitizing(monkeypatch):
    monkeypatch.setattr(isolation, 'transport_snapshot', lambda: snapshot())
    first = isolation.allocate_transport('a/b', 'objective1', 1, set())
    second = isolation.allocate_transport('a_b', 'objective1', 1, set())
    assert first['GZ_PARTITION'] != second['GZ_PARTITION']


def test_domain_block_rejects_out_of_range_and_exhaustion(monkeypatch):
    monkeypatch.setattr(isolation, 'transport_snapshot', lambda: snapshot())
    with pytest.raises(ValueError, match='0–101'):
        isolation.allocate_transport('run', 'objective1', 1, set(), domain_max=102)
    with pytest.raises(ValueError, match='ordinal'):
        isolation.allocate_transport('run', 'objective1', 0, set())
    with pytest.raises(RuntimeError, match='No unused'):
        isolation.allocate_transport('run', 'objective1', 1, set(range(20, 100)))


def test_collision_checks_process_environment_and_dds_udp_block():
    env = {'ROS_DOMAIN_ID': '20', 'GZ_PARTITION': 'test_partition'}
    collisions = isolation.transport_collisions(env, snapshot([
        {'pid': 5, 'ROS_DOMAIN_ID': '20'},
        {'pid': 6, 'GZ_PARTITION': 'test_partition'},
        {'pid': 7, 'ROS_DOMAIN_ID': '21'},
    ], [7400 + 250 * 20, 7400 + 250 * 21]))
    assert [rec.get('pid') for rec in collisions] == [5, 6, None]
    assert collisions[-1]['ports'] == [12400]


def test_allocation_records_skipped_collision_and_available_assertion_fails(monkeypatch):
    occupied = snapshot([{'pid': 5, 'ROS_DOMAIN_ID': '20'}], [12650])
    monkeypatch.setattr(isolation, 'transport_snapshot', lambda: occupied)
    result = isolation.allocate_transport('run', 'objective1', 1, set(), 20, 22)
    assert result['ROS_DOMAIN_ID'] == '22'
    with pytest.raises(RuntimeError, match='no process was signalled'):
        isolation.assert_transport_available({'ROS_DOMAIN_ID': '20', 'GZ_PARTITION': 'unused'})


def test_transport_verification_fails_missing_key_and_foreign_process(monkeypatch):
    owned = owner({42: record(42)})
    monkeypatch.setattr(isolation, 'process_inventory', lambda _: [
        {**record(42), 'comm': 'rviz2', 'owned': True},
        {**record(43), 'comm': 'map_server', 'owned': False},
    ])
    monkeypatch.setattr(isolation, 'process_table', lambda: {42: record(42)})
    monkeypatch.setattr(isolation, '_identity', lambda pid: 100)
    monkeypatch.setattr(isolation, '_transport_environment', lambda pid: {'ROS_DOMAIN_ID': '20'})
    result = isolation.verify_transport(owned, {'ROS_DOMAIN_ID': '20', 'GZ_PARTITION': 'test'})
    assert not result['ok']
    assert len(result['mismatches']) == 2
    assert result['mismatches'][0]['error'] == 'Missing or mismatched transport environment'


def test_transport_verification_checks_launch_leader_and_localhost_scope(monkeypatch):
    owned = owner({42: record(42), 43: record(43)})
    monkeypatch.setattr(isolation, 'process_inventory', lambda _: [
        {**record(43), 'comm': 'gz', 'owned': True}])
    monkeypatch.setattr(isolation, 'process_table', lambda: {42: record(42), 43: record(43)})
    monkeypatch.setattr(isolation, '_identity', lambda pid: 100)
    env = {'ROS_DOMAIN_ID': '20', 'GZ_PARTITION': 'test', 'ROS_LOCALHOST_ONLY': '1',
           'ROS_AUTOMATIC_DISCOVERY_RANGE': 'LOCALHOST'}
    monkeypatch.setattr(isolation, '_transport_environment', lambda pid: env)
    result = isolation.verify_transport(owned, env)
    assert result['ok']
    assert {rec['pid'] for rec in result['processes']} == {42, 43}
    monkeypatch.setattr(isolation, '_transport_environment', lambda pid: {**env, 'ROS_LOCALHOST_ONLY': '0'})
    assert not isolation.verify_transport(owned, env)['ok']


def test_transport_verification_fails_on_stale_pid_and_unreadable_environment(monkeypatch):
    owned = owner({42: record(42)})
    monkeypatch.setattr(isolation, 'process_inventory', lambda _: [
        {**record(42), 'comm': 'gz', 'owned': True}])
    monkeypatch.setattr(isolation, 'process_table', lambda: {42: record(42)})
    monkeypatch.setattr(isolation, '_identity', lambda pid: 101)
    env = {'ROS_DOMAIN_ID': '20', 'GZ_PARTITION': 'test'}
    monkeypatch.setattr(isolation, '_transport_environment', lambda pid: env)
    assert not isolation.verify_transport(owned, env)['ok']
    def unavailable(pid):
        raise PermissionError('test process environment hidden')
    monkeypatch.setattr(isolation, '_transport_environment', unavailable)
    assert not isolation.verify_transport(owned, env)['ok']
