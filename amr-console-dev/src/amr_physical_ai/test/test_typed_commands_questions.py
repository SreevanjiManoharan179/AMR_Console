"""The calibrated ask path (grounding calibration) in the typed-command adapter; fake ROS boundaries,
no sockets/nodes.

The grounding threshold comes from a pilot run made inside each test's temporary repository
with the real package, so these tests never depend on a committed results file.
"""
import json
from pathlib import Path
import shutil
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/amr_console'))
sys.path.insert(0, str(ROOT / 'tools/grounding_calibration'))
import typed_commands
from typed_commands import command
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations_fixture import install  # noqa: E402
from amr_physical_ai.command_pipeline import Locations

STATIONS = ['east_station', 'north_station', 'south_station']


@pytest.fixture
def repo(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    path = tmp_path / 'src/amr_physical_ai/config/named_locations.yaml'
    shutil.copytree(ROOT / 'tools/grounding_calibration', tmp_path / 'tools/grounding_calibration',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    from grounding_calibration.run_pilot import main
    main(['--registry', str(path), '--out', str(tmp_path / Path(typed_commands.GROUNDING_RESULTS).parent),
          '--repeats', '5', '--label', 'test fixture'])
    return tmp_path


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


def asked(record):
    return (record['nav2'] == {'status': 'CLARIFICATION', 'goal_sent': False}
            and not record['validation']['valid'] and record['validation']['question'])


def test_ambiguous_command_asks_a_closed_question_and_sends_nothing(repo):
    session = FakeSession(repo)
    record = command(repo, session, 'go to the station')
    assert asked(record) and not session.goals
    assert record['validation']['question'] == 'Which one: the east station, the north station or the south station?'
    assert record['grounding']['decision'] == 'ask' and record['grounding']['set_locations'] == STATIONS
    assert record['grounding']['awaiting_answer'] and session.grounding_pending['options'] == STATIONS
    assert record['position_change_m'] == 0.
    json.dumps(record, allow_nan=False)


def test_answer_in_the_offered_set_is_dispatched_as_a_normal_goal(repo):
    session = FakeSession(repo)
    command(repo, session, 'go to the station')
    record = command(repo, session, 'north station')
    assert session.goals == [{'x': .4, 'y': .6, 'yaw': 1.5708}]
    assert record['nav2']['status'] == 'SUCCEEDED' and record['nav2']['goal_sent']
    assert record['validation']['valid'] and record['parsed_json']['location'] == 'north_station'
    assert record['grounding_answer']['resolution'] == 'named_in_set'
    assert record['true_final_error_m'] == pytest.approx(.05)
    assert session.grounding_pending is None


def test_bare_place_answer_with_article_is_understood(repo):
    session = FakeSession(repo)
    command(repo, session, 'go to the station')
    record = command(repo, session, 'the south station')
    assert record['grounding_answer']['resolution'] == 'named_in_set'
    assert session.goals == [{'x': .4, 'y': -.5, 'yaw': -1.5708}]


def test_answer_outside_the_offered_set_sends_nothing_and_ends_the_question(repo):
    session = FakeSession(repo)
    command(repo, session, 'go to the station')
    record = command(repo, session, 'go home')
    assert asked(record) and not session.goals
    assert record['grounding_answer']['resolution'] == 'outside_offered_set'
    assert session.grounding_pending is None
    assert command(repo, session, 'go home')['nav2']['status'] == 'SUCCEEDED'   # a fresh command works


def test_unclear_answer_sends_nothing(repo):
    session = FakeSession(repo)
    command(repo, session, 'go to the station')
    record = command(repo, session, 'the blue one')
    assert asked(record) and not session.goals
    assert record['grounding_answer']['resolution'] == 'not_understood'


def test_unknown_place_is_rejected_without_a_question(repo):
    session = FakeSession(repo)
    record = command(repo, session, 'go to the kitchen')
    assert asked(record) and not session.goals
    assert record['grounding']['decision'] == 'reject' and not record['grounding']['awaiting_answer']
    assert record['validation']['question'].startswith("I don't know that place.")
    assert session.grounding_pending is None


def test_single_place_set_is_confirmed_before_any_motion(repo):
    session = FakeSession(repo)
    record = command(repo, session, 'go to the loer station')     # typo: the rule parser cannot ground it
    assert asked(record) and not session.goals
    assert record['grounding']['set_locations'] == ['south_station']
    assert record['validation']['question'] == 'Did you mean the south station?'
    record = command(repo, session, 'yes')
    assert record['grounding_answer']['resolution'] == 'confirmed'
    assert session.goals == [{'x': .4, 'y': -.5, 'yaw': -1.5708}]
    assert record['nav2']['status'] == 'SUCCEEDED'


def test_declined_confirmation_sends_nothing(repo):
    session = FakeSession(repo)
    command(repo, session, 'go to the loer station')
    record = command(repo, session, 'no')
    assert asked(record) and not session.goals
    assert record['grounding_answer']['resolution'] == 'declined'


def test_clear_commands_never_touch_o3(repo):
    session = FakeSession(repo)
    record = command(repo, session, 'go to the right station')
    assert 'grounding' not in record and record['parsed_json']['location'] == 'east_station'
    assert session.goals == [{'x': .8, 'y': 0., 'yaw': 0.}]


@pytest.mark.parametrize('break_it', ['scorer', 'registry', 'missing'])
def test_inconsistent_or_missing_threshold_falls_back_to_the_plain_clarification(repo, break_it):
    results = repo / typed_commands.GROUNDING_RESULTS
    data = json.loads(results.read_text())
    if break_it == 'scorer':
        data['meta']['scorer'] = 'C (rule-based token match)'
        results.write_text(json.dumps(data))
    elif break_it == 'registry':
        data['registry'][1]['aliases'] = ['east']
        results.write_text(json.dumps(data))
    else:
        results.unlink()
    session = FakeSession(repo)
    record = command(repo, session, 'go to the station')
    assert asked(record) and not session.goals
    assert 'error' in record['grounding'] and session.grounding_pending is None
    assert record['validation']['question'].startswith('Which single destination')
