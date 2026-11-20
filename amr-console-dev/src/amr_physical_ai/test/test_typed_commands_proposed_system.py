"""Typed commands and the calibrated ask path on the proposed_system profile; fake ROS boundaries,
no sockets or nodes.

The grounding threshold comes from a pilot run made inside each test's temporary repository on the
proposed_system places, so these tests never depend on a committed results file.
"""
from pathlib import Path
import shutil
import sys
import threading

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/amr_console'))
sys.path.insert(0, str(ROOT / 'tools/grounding_calibration'))
from typed_commands import command
from amr_physical_ai.command_pipeline import Locations

PROFILE = {'map_id': 'proposed_system', 'approval': 'proposed_system_places',
           'locations': 'src/amr_physical_ai/config/named_locations_proposed_system.yaml',
           'grounding_results': 'config/calibration/proposed_system/grounding/results.json'}
APPROVALS = ''


@pytest.fixture
def repo(tmp_path):
    for name in ('config/named_locations_proposed_system.yaml', 'maps/proposed_system.yaml', 'maps/proposed_system.pgm'):
        target = tmp_path / 'src/amr_physical_ai' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'src/amr_physical_ai' / name, target)
    path = tmp_path / PROFILE['locations']
    config = yaml.safe_load(path.read_text())
    config['approval_status'] = 'confirmed'          # the test's stand-in for the owner's approval edit
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    approval = tmp_path / 'tools/amr_console/approvals.yaml'
    approval.parent.mkdir(parents=True)
    approval.write_text(APPROVALS + 'proposed_system_places: true\n')
    shutil.copytree(ROOT / 'tools/grounding_calibration', tmp_path / 'tools/grounding_calibration',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    from grounding_calibration.run_pilot import main
    main(['--registry', str(path), '--out', str(tmp_path / Path(PROFILE['grounding_results']).parent),
          '--repeats', '5', '--label', 'test fixture'])
    return tmp_path


class FakeSession:
    def __init__(self, repo):
        self.grid = Locations(repo / PROFILE['locations']).static_grid
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


def ask(repo, session, text):
    return command(repo, session, text, profile=PROFILE)


def test_every_place_is_sent_with_exactly_its_named_pose(repo):
    locations = Locations(repo / PROFILE['locations'])
    for name, place in locations.by_name.items():
        session = FakeSession(repo)
        record = ask(repo, session, 'go to the ' + name.replace('_', ' '))
        assert session.goals == [{'x': place.x, 'y': place.y, 'yaw': place.yaw}], name
        assert record['nav2']['status'] == 'SUCCEEDED' and 'grounding' not in record
        assert record['command_map'] == 'proposed_system' and record['locations_file'] == PROFILE['locations']


def test_office_asks_which_one_and_the_answer_is_dispatched(repo):
    session = FakeSession(repo)
    record = ask(repo, session, 'go to the office')
    assert not session.goals and record['position_change_m'] == 0.
    assert record['validation']['question'] == 'Which one: the open office or the team office?'
    assert record['grounding']['set_locations'] == ['open_office', 'team_office']
    assert record['grounding']['source'] == PROFILE['grounding_results']
    record = ask(repo, session, 'team office')
    assert record['grounding_answer']['resolution'] == 'named_in_set'
    assert session.goals == [{'x': 10.0, 'y': -3.0, 'yaw': -1.5708}]


def test_unknown_and_typo_replies_in_proposed_system(repo):
    session = FakeSession(repo)
    record = ask(repo, session, 'go to the library')
    assert record['validation']['question'] == (
        "I don't know that place. I can go to: home, reception, conference hall, demo area, cafeteria, "
        "open office, team office, manager cabin, storage.")
    record = ask(repo, session, 'go to the cafetria')
    assert record['validation']['question'] == 'Did you mean the cafeteria?' and not session.goals
    record = ask(repo, session, 'yes')
    assert record['grounding_answer']['resolution'] == 'confirmed'
    assert session.goals == [{'x': 12.5, 'y': 2.5, 'yaw': 0.0}]


def test_proposed_system_needs_its_own_approval(repo):
    approvals = repo / 'tools/amr_console/approvals.yaml'
    approvals.write_text('proposed_system_places: false\n')     # the other keys do not count
    session = FakeSession(repo)
    record = ask(repo, session, 'go to the storage')
    assert not session.goals and record['nav2']['status'] == 'CLARIFICATION'
    assert 'approval' in record['validation']['question']


def test_another_maps_calibration_is_not_used_for_proposed_system_places(repo):
    places = repo / 'src/amr_physical_ai/config/named_locations_stations.yaml'
    shutil.copyfile(Path(__file__).resolve().parent / 'data/stations/named_locations.yaml', places)
    from grounding_calibration.run_pilot import main
    main(['--registry', str(places), '--out', str(repo / 'config/calibration/stations/grounding'),
          '--repeats', '5', '--label', 'test fixture'])       # a real calibration for another map, not a missing file
    session = FakeSession(repo)
    wrong = dict(PROFILE, grounding_results='config/calibration/stations/grounding/results.json')
    record = command(repo, session, 'go to the office', profile=wrong)
    assert 'registry changed' in record['grounding']['error'] and not session.goals   # plain question instead
    assert record['validation']['question'].startswith('Which single destination')
