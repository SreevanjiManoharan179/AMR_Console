"""Typed-command adapter safety and evidence with fake ROS boundaries; no sockets/nodes."""
import json
import math
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/amr_console'))
from typed_commands import command, truth_sample
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations_fixture import install  # noqa: E402
from amr_physical_ai.command_pipeline import Grid, Locations


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return install(tmp_path, monkeypatch)


class FakeSession:
    def __init__(self, repo):
        self.grid = Locations(repo / 'src/amr_physical_ai/config/named_locations.yaml').static_grid
        self.active_goal = None
        self.stop_requested = threading.Event()
        self.goals = []
        self.xy = (0., 0.)
        self.stamp = 1.

    def command_grid(self):
        return self.grid

    def truth_pose(self, after=None):
        self.stamp += 1.
        return {'x': self.xy[0], 'y': self.xy[1], 'simulation_time_s': self.stamp,
                'source': '/amr/ground_truth'}

    def navigate(self, params, *, before_send):
        before_send()
        self.goals.append(params)
        self.xy = (params['x'] + .03, params['y'] + .04)
        return {'status': 4, 'navigation_outcome': 'SUCCEEDED'}


def test_five_browser_requests_have_three_goals_two_rejections_and_true_error(repo):
    session = FakeSession(repo)
    texts = ['go to the north station', 'go to the right station', 'return to base',
             'go to the station', 'go to the kitchen']
    records = [command(repo, session, text) for text in texts]
    assert len(session.goals) == 3
    assert [r['parsed_json']['location'] for r in records[:3]] == ['north_station', 'east_station', 'home']
    for text, record in zip(texts, records):
        assert record['text'] == text and 'FALLBACK' in record['parser']
        assert record['timestamp'] and record['total_wall_s'] >= 0
        assert record['parsed_json'] is not None
        json.dumps(record, allow_nan=False)
    for record in records[:3]:
        assert record['validation']['valid'] and record['nav2']['goal_sent']
        assert record['nav2']['status'] == 'SUCCEEDED'
        assert record['true_final_error_m'] == pytest.approx(.05)
    for record in records[3:]:
        assert not record['validation']['valid'] and record['validation']['question']
        assert record['nav2'] == {'status': 'CLARIFICATION', 'goal_sent': False}
        assert record['position_change_m'] == 0.
        assert record['true_final_error_m'] is None


@pytest.mark.parametrize('gate', ['console', 'pipeline'])
def test_either_unapproved_gate_blocks_dispatch(repo, gate):
    session = FakeSession(repo)
    if gate == 'console':
        path = repo / 'tools/amr_console/approvals.yaml'
        path.write_text(path.read_text().replace('stations_places: true', 'stations_places: false'))
    else:
        path = repo / 'src/amr_physical_ai/config/named_locations.yaml'
        path.write_text(path.read_text().replace('approval_status: confirmed', 'approval_status: proposed'))
    record = command(repo, session, 'go home')
    assert not session.goals and not record['nav2']['goal_sent']
    assert record['nav2']['status'] == 'CLARIFICATION'


@pytest.mark.parametrize('when', ['initial', 'before_send'])
def test_wrong_live_map_blocks_even_after_action_server_wait(repo, when):
    session = FakeSession(repo)
    wrong = Grid(np.zeros((10, 10)), .05, (0, 0, 0))
    original = session.grid
    session.command_grid = Mock(side_effect=[wrong] if when == 'initial' else [original, wrong])
    record = command(repo, session, 'go home')
    assert not session.goals and not record['nav2']['goal_sent']
    assert 'map' in record['validation']['question'].lower()


def test_revoked_approval_during_action_server_wait_prevents_goal(repo):
    session = FakeSession(repo)
    original = session.navigate
    def revoke(params, *, before_send):
        path = repo / 'tools/amr_console/approvals.yaml'
        path.write_text(path.read_text().replace('stations_places: true', 'stations_places: false'))
        return original(params, before_send=before_send)
    session.navigate = revoke
    record = command(repo, session, 'go home')
    assert not session.goals and not record['nav2']['goal_sent']
    assert 'changed' in record['validation']['question']


@pytest.mark.parametrize('when', ['initial', 'before_send', 'after'])
def test_missing_truth_never_becomes_a_true_error_or_unmeasured_dispatch(repo, when):
    session = FakeSession(repo)
    original = session.truth_pose
    count = 0
    def truth(after=None):
        nonlocal count
        count += 1
        if (when == 'initial' and count == 1 or when == 'before_send' and count == 2
                or when == 'after' and after is not None):
            raise TimeoutError('Fresh Gazebo ground truth timed out.')
        return original(after)
    session.truth_pose = truth
    record = command(repo, session, 'go home')
    assert record['true_final_error_m'] is None
    if when == 'after':
        assert record['nav2']['status'] == 'SUCCEEDED' and record['evidence_error']
    else:
        assert not session.goals and record['error']


def test_transport_error_requests_audited_stop_before_any_second_goal(repo):
    session = FakeSession(repo)
    def timeout(params, *, before_send):
        before_send()
        raise TimeoutError('Goal acceptance timed out.')
    session.navigate = timeout
    record = command(repo, session, 'go home')
    assert record['nav2']['status'] == 'ERROR'
    assert record['nav2']['goal_sent'] and session.stop_requested.is_set()
    assert 'Goal acceptance' in record['error']


def test_ground_truth_uses_fixed_world_offset_and_rejects_wrong_frames():
    transform = SimpleNamespace(child_frame_id='turtlebot3_waffle',
        header=SimpleNamespace(frame_id='default', stamp=SimpleNamespace(sec=8, nanosec=500000000)),
        transform=SimpleNamespace(translation=SimpleNamespace(x=-1.2, y=-.5)))
    message = SimpleNamespace(transforms=[transform])
    sample = truth_sample(message, 123.)
    assert sample['x'] == pytest.approx(.8) and sample['y'] == 0
    assert sample['simulation_time_s'] == 8.5 and sample['received_monotonic'] == 123.
    transform.header.frame_id = 'map'
    assert truth_sample(message, 123.) is None
    transform.header.frame_id = 'default'
    transform.transform.translation.x = math.nan
    assert truth_sample(message, 123.) is None


def test_worker_routes_one_command_and_saves_record_in_the_session(repo, monkeypatch):
    import worker
    monkeypatch.setattr(worker, 'ROOT', repo)
    monkeypatch.setattr(worker.threading, 'Thread',
        lambda *, target, **kwargs: SimpleNamespace(start=target))
    output = repo / 'data/runs/demo'
    session = worker.SessionWorker({'kind': 'navigation', 'record_truth': True,
        'output_dir': str(output), 'log_dir': str(repo / 'data/logs/console/demo')})
    session.ready = True
    session.adapter = FakeSession(repo)
    session.environment = {'ROS_DOMAIN_ID': '80', 'GZ_PARTITION': 'amr_console_navigation_80'}
    session.emit = Mock()
    session.log = Mock()
    request_id = 'a' * 24
    session.operation({'action_id': 'commands.typed', 'request_id': request_id,
                       'params': {'text': 'go to the kitchen'}})
    record = json.loads((output / 'commands' / (request_id + '.json')).read_text())
    assert record['transport'] == session.environment
    assert record['nav2']['status'] == 'CLARIFICATION'
    assert not session.adapter.goals and not session.operation_busy
    session.emit.assert_called_once()
    assert session.emit.call_args.kwargs['ok']
    assert 'record_path' in session.emit.call_args.kwargs['result']
    assert session.log.call_args.args[0].startswith('Typed command — RULE-BASED FALLBACK')
