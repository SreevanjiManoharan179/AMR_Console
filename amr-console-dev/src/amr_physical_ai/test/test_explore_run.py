"""The live exploration loop, its records, the scorer and the ROS node on a stand-in ROS (autonomous exploration, live part). Offline."""
import json
import math
from pathlib import Path
import sys
import threading
import time

import numpy as np
import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_exploration as sx
from amr_physical_ai import exploration as ex
from amr_physical_ai import exploration_run as er
from amr_physical_ai.exploration_sim import SimRobot, run_loop
from test_exploration import two_rooms


def exact(truth):
    return truth.occupied, truth.free, truth.resolution, truth.origin


def events(folder):
    return sx.read_jsonl(Path(folder) / 'events.jsonl')


@pytest.fixture(scope='module')
def explored(tmp_path_factory):
    """One recorded offline run per method in the two rooms."""
    runs = {}
    for method in ex.METHODS:
        out = tmp_path_factory.mktemp(method) / 'exploration'
        loop, robot = run_loop(method, two_rooms(), out=out)
        runs[method] = (out, loop, robot)
    return runs


@pytest.mark.parametrize('method', ex.METHODS)
def test_each_method_maps_the_rooms_and_records_the_run(explored, method):
    out, loop, robot = explored[method]
    assert loop.end == 'stalled: no eligible frontier cluster remains'
    returns = [e for e in events(out) if e['event'] == 'return']
    assert len(returns) == 1 and returns[0]['success']
    assert returns[0]['arrival_error_m'] <= .3
    found = events(out)
    kinds = [e['event'] for e in found]
    assert kinds[0] == 'start' and kinds[-1] == 'finish'
    decisions = [e for e in found if e['event'] == 'decision' and e['index']]
    results = [e for e in found if e['event'] == 'result']
    assert len(decisions) == loop.goals == len(results) and loop.goals >= 2
    assert {e['outcome'] for e in results} <= {'SUCCEEDED', 'RESOLVED'}
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['end'] == loop.end and summary['goals'] == loop.goals
    assert summary['settings_sha256'] == er.settings_digest(ex.Settings(), er.RunSettings())
    assert summary['snapshots'] == len(list((out / 'maps').glob('*.npz'))) > 2
    assert json.loads((out / 'progress.json').read_text())['state'] == 'finished'
    truth = sx.read_jsonl(out / 'truth.jsonl')
    assert len(truth) > 10 and all(s['slam'] == [s['x'], s['y'], s['yaw']] for s in truth)
    if method == 'ours':
        assert found[-2]['kind'] == 'done' and robot.pose()[:2] == pytest.approx((0, 0), abs=.3)   # home again


def test_resolved_frontiers_are_replanned_at_once(explored):
    out, loop, robot = explored['ours']
    assert loop.results['RESOLVED'] >= 1
    found = events(out)
    after = [i for i, e in enumerate(found) if e.get('outcome') == 'RESOLVED']
    assert all(found[i + 1]['event'] == 'decision' for i in after)


def test_a_new_goal_replaces_the_running_one_without_the_turn():
    robot = SimRobot(two_rooms())
    first = robot.send(2., 0., 0.)
    robot.sleep(4.)
    second = robot.send(0., 1., 0.)
    assert first['status'] == 'ABORTED' and second['status'] is None and second['delay'] == 0.
    assert robot.send(0., 0., 0.)['delay'] == 0. and second['status'] == 'ABORTED'
    robot.sleep(30.)
    third = robot.send(1., 0., 0.)
    assert third['delay'] == robot.turn_s            # after a finished goal the robot turns first


def test_settings_digest_changes_with_any_setting():
    base = er.settings_digest(ex.Settings(), er.RunSettings())
    assert base == er.settings_digest(ex.Settings(), er.RunSettings())
    assert base != er.settings_digest(ex.Settings(min_frontier_cells=4), er.RunSettings())
    assert base != er.settings_digest(ex.Settings(), er.RunSettings(time_limit_s=1200.))


class Stuck(SimRobot):
    """A robot whose goals never end, or are refused."""

    def __init__(self, *args, refuse=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.refuse = refuse

    def send(self, x, y, yaw):
        if self.refuse:
            return None
        goal = super().send(x, y, yaw)
        goal['path'], goal['fail_at'] = [(self.x, self.y)] * 2, None
        goal['next'] = 0
        return goal

    def _move(self, dt):
        pass


def loop_with(robot, run_settings=er.RunSettings(), method='gain'):
    return er.ExplorationLoop(er.new_explorer(method), robot, er.NullRecorder(), run_settings)


def test_a_goal_that_runs_too_long_is_cancelled_and_its_area_avoided():
    robot = Stuck(two_rooms())
    loop = loop_with(robot, er.RunSettings(goal_timeout_base_s=5., goal_timeout_factor=0., time_limit_s=60., return_reserve_s=10.))
    end = loop.run()
    assert end == 'time limit: exploration stopped at 50 s; 10 s reserved for return'
    assert loop.results['TIMEOUT'] >= 5
    assert loop.return_home['outcome'] in ('TIMEOUT', 'INTERRUPTED')
    assert not loop.return_home['success']
    assert len(loop.explorer.failed) == loop.results['TIMEOUT'] - int(loop.return_home['outcome'] == 'TIMEOUT')
    assert robot.goal['status'] == 'CANCELED'


def test_refused_goals_end_the_run_after_five():
    loop = loop_with(Stuck(two_rooms(), refuse=True))
    assert loop.run() == 'Nav2 refused 5 goals in a row'
    assert loop.results['REJECTED'] == 5


def test_stop_ends_the_run_and_cancels_the_goal():
    robot = SimRobot(two_rooms(), stop_at=8.)
    loop = loop_with(robot)
    assert loop.run() == er.STOPPED
    assert robot.goal['status'] == 'CANCELED' and loop.active is None
    assert loop.results['INTERRUPTED'] == 1 and sum(loop.results.values()) == loop.goals


class StopOnSend(SimRobot):
    """STOP arrives while the second goal is being sent, and Nav2 never answers it."""

    def send(self, x, y, yaw):
        if self.sent >= 1:
            self.stop_at = self.t
            return None
        return super().send(x, y, yaw)


def test_stop_while_nav2_is_asked_is_not_a_refusal_and_stops_the_old_goal():
    robot = StopOnSend(two_rooms())
    loop = loop_with(robot, method='ours')
    robot_goals = []
    original = robot.cancel
    robot.cancel = lambda goal: (robot_goals.append(goal['id']), original(goal))[1]
    end = loop.run()
    found = dict(loop.results)
    assert end == er.STOPPED and found.get('NOT_SENT') == 1 and 'REJECTED' not in found
    first_still_driving = found.get('RESOLVED', 0) == 1
    assert robot_goals == ([1] if first_still_driving else []) and loop.active is None


def test_stop_while_a_goal_is_chosen_sends_nothing():
    robot = SimRobot(two_rooms(), stop_at=1e-6, count_compute=True)   # the decision's time passes STOP
    loop = loop_with(robot)
    assert loop.run() == er.STOPPED
    assert robot.sent == 0 and dict(loop.results) == {'NOT_SENT': 1}


def test_map_frame_follows_the_spawn_pose():
    assert er.map_frame(-2., -.5, 0., (-2., -.5, 0.)) == pytest.approx((0., 0., 0.))
    assert er.map_frame(0., .5, .3, (-2., -.5, 0.)) == pytest.approx((2., 1., .3))
    x, y, yaw = er.map_frame(-2., .5, math.pi, (-2., -.5, math.pi / 2))
    assert (x, y, yaw) == pytest.approx((1., 0., math.pi / 2))


def test_launch_offers_the_explorer_methods_and_its_arguments():
    import ast
    source = (PKG / 'launch/tb3_mapping.launch.py').read_text()
    tree = ast.parse(source)
    methods = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and getattr(node.targets[0], 'id', '') == 'EXPLORE_METHODS')
    assert set(methods) == set(ex.METHODS)
    for text in ("DeclareLaunchArgument('explore'", "DeclareLaunchArgument('explore_out'",
                 "'amr_physical_ai.explore_node'", "launch/navigation_launch.py", "'use_sim_time:=true'"):
        assert text in source


def test_recorder_refuses_a_folder_with_records(tmp_path):
    (tmp_path / 'old.txt').write_text('x')
    with pytest.raises(FileExistsError):
        er.Recorder(tmp_path)
    recorder = er.Recorder(tmp_path / 'new')
    recorder.event('decision', kind='frontier', value=np.float32(1.5), nan=float('nan'))
    assert recorder.snapshot(np.zeros((2, 3), dtype=np.int16), .05, (1., 2., 0.), 7.5, header_stamp=7.) == 0
    recorder.close()
    assert events(tmp_path / 'new') == [{'event': 'decision', 'kind': 'frontier', 'value': 1.5, 'nan': None}]
    with np.load(tmp_path / 'new/maps/0000.npz') as saved:
        assert saved['data'].dtype == np.int8 and float(saved['stamp']) == 7.5 and float(saved['header_stamp']) == 7.


def test_scores_of_a_recorded_run(explored):
    out, loop, robot = explored['ours']
    score = sx.score_run(out, exact=exact(two_rooms()), world='two_rooms')
    assert score['method'] == 'ours' and score['end'] == loop.end
    assert score['completion'] >= .99 and score['T99_s'] is not None and score['T90_s'] <= score['T99_s']
    assert 0 < score['distance_to_99_m'] <= score['distance_m'] == pytest.approx(robot.travelled, rel=.05)
    assert score['wall_agreement'] == 1. and score['faces_seen'] > .95
    assert score['collisions'] == 0 and score['min_clearance_m'] >= ex.Settings().clearance_m - .06
    assert score['final_pose_error_m'] == 0. and score['recoveries'] == 0
    ratios = [r for _, r, _ in score['series']]
    assert ratios == sorted(ratios) and score['maps_scored'] == len(score['series'])
    assert score['goals'] == loop.goals and score['results'] == dict(loop.results)


def test_benchmark_compares_with_nearest_and_checks_the_targets(explored, tmp_path):
    folders = [str(explored[m][0]) for m in ex.METHODS]
    scores = [sx.score_run(f, exact=exact(two_rooms()), world='two_rooms') for f in folders]
    result = sx.aggregate(scores)
    assert set(result['methods']) == set(ex.METHODS) and set(result['versus_nearest']) == {'gain', 'ours'}
    b0, ours = result['methods']['nearest'], result['methods']['ours']
    assert result['versus_nearest']['ours']['T99_s'] == pytest.approx(1 - ours['T99_s']['mean'] / b0['T99_s']['mean'])
    targets = {t['key']: t for t in result['targets']}
    assert targets['completion']['met'] and targets['collisions']['met']
    assert targets['T99_s']['met'] == (result['versus_nearest']['ours']['T99_s'] >= .25)
    text = sx.markdown(result, 'two_rooms')
    assert '| Completion, min |' in text and 'Not checked' in text


def test_runs_that_never_reach_99_percent_void_the_comparison():
    def run(method, t99, d99):
        base = {m: 1. for m in sx.METRICS}
        base.update(method=method, end='done', settings_sha256='s', T99_s=t99, distance_to_99_m=d99,
                    coverage_auc_6min=.5, collisions=0, recoveries=0, final_pose_error_m=.02)
        return base
    scores = [run('nearest', 200., 60.) for _ in range(5)] + [run('ours', 100., 30.)] + \
        [run('ours', None, None) for _ in range(4)]
    result = sx.aggregate(scores)
    assert result['versus_nearest']['ours']['T99_s'] is None and result['versus_nearest']['ours']['distance_to_99_m'] is None
    targets = {t['key']: t for t in result['targets']}
    for key in ('T99_s', 'distance_to_99_m'):
        assert targets[key]['value'] is None and targets[key]['met'] is False and targets[key]['stretch_met'] is False


def test_targets_use_the_worst_run_and_the_means():
    def run(method, **values):
        base = {m: 1. for m in sx.METRICS}
        base.update(method=method, end='done', settings_sha256='s', T99_s=100., distance_to_99_m=50.,
                    coverage_auc_6min=.5, collisions=0, recoveries=0, final_pose_error_m=.02)
        base.update(values)
        return base
    scores = [run('nearest'), run('nearest', T99_s=140.), run('ours', T99_s=80.), run('ours', T99_s=70.,
              completion=.985, recoveries=2, distance_to_99_m=30., coverage_auc_6min=.65)]
    result = sx.aggregate(scores)
    targets = {t['key']: t for t in result['targets']}
    assert targets['T99_s']['value'] == pytest.approx(1 - 75. / 120.) and targets['T99_s']['stretch_met']
    assert targets['completion']['value'] == .985 and not targets['completion']['met']
    assert targets['recoveries']['value'] == 2 and not targets['recoveries']['met']
    assert targets['distance_to_99_m']['value'] == pytest.approx(1 - 40. / 50.)
    assert targets['coverage_auc_6min']['value'] == pytest.approx(.575 / .5 - 1) and not targets['coverage_auc_6min']['met']


# ---- the ROS node on the stand-in ROS ----

def run_node(tmp_path, monkeypatch, *, method='nearest', stop_when=None, refuse=False, hold=False,
             send=None, timeout=120., speedup=400.):
    """Run explore_node.main on the stand-in ROS until it finishes, press STOP, and return (code, out, world)."""
    from fake_ros import FakeRos, World
    world = World(two_rooms(), refuse_goals=refuse, hold_acceptance=hold, speedup=speedup)
    out = tmp_path / 'exploration'
    with FakeRos(world) as ros:
        from amr_physical_ai import explore_node
        if send is not None:
            monkeypatch.setattr(explore_node.ExploreNode, 'send', send)
        result = {}
        argv = ['explore_node', '--method', method, '--out', str(out), '--world', 'two_rooms',
                '--nav2-params', str(PKG / 'config/nav2_mppi.yaml'), '--ros-args', '-p', 'use_sim_time:=true']
        thread = threading.Thread(target=lambda: result.update(code=explore_node.main(argv)), daemon=True)
        world.start()
        thread.start()
        deadline, stopped = time.monotonic() + timeout, False
        while time.monotonic() < deadline and thread.is_alive() and not stopped:
            chosen = any(e['event'] == 'decision' for e in events(out)) if (out / 'events.jsonl').is_file() else False
            goal = world.robot.goal
            driving = goal is not None and goal['status'] is None
            if ((stop_when == 'chosen' and chosen) or (stop_when == 'driving' and driving)
                    or (out / 'summary.json').is_file()):
                ros.recovery('backup', [1] * 16)
                ros.recovery('backup', [1] * 16)          # the same goal again: recorded once
                assert ros.stop().success
                stopped = True
                world.stopped_at = time.monotonic()
            time.sleep(.01)
        thread.join(10.)
        world.exited_at = time.monotonic()
        assert not thread.is_alive(), 'the node did not exit after STOP'
    return result.get('code'), out, world


def test_node_explores_records_saves_the_map_and_exits_cleanly(tmp_path, monkeypatch):
    code, out, world = run_node(tmp_path, monkeypatch)
    assert code == 0
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['end'] == 'stalled: no eligible frontier cluster remains' and summary['error'] is None
    assert summary['final_map'] == {'saved': True, 'result': 0, 'yaml': 'final_map.yaml', 'pgm': 'final_map.pgm'}
    assert world.saved == [str((out / 'final_map').resolve())]
    assert summary['world'] == 'two_rooms' and summary['nav2_params_sha256'] == er.file_digest(
        PKG / 'config/nav2_mppi.yaml')
    assert set(summary['sources']) == {'exploration.py', 'exploration_run.py', 'explore_node.py'}
    assert all(len(v) == 64 for v in summary['sources'].values())
    kinds = [e['event'] for e in events(out)]
    assert kinds[:2] == ['ready', 'start'] and 'finish' in kinds
    truth = sx.read_jsonl(out / 'truth.jsonl')
    assert len(truth) > 5 and truth[0]['source'] == '/amr/ground_truth'
    assert all(s['slam'] is not None and math.dist((s['x'], s['y']), s['slam'][:2]) < .5 for s in truth)
    score = sx.score_run(out, exact=exact(two_rooms()), world='two_rooms')
    assert score['completion'] >= .99 and score['T99_s'] is not None
    assert any(text.startswith('Exploration finished') for _, text in world.logs)
    assert [(e['event'], e['behavior']) for e in events(out) if e['event'] == 'recovery'] == [('recovery', 'backup')]


def test_node_stop_mid_run_cancels_and_skips_the_final_map(tmp_path, monkeypatch):
    code, out, world = run_node(tmp_path, monkeypatch, method='ours', stop_when='driving')
    assert code == 0
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['end'] == er.STOPPED and summary['final_map']['saved'] is False
    assert world.saved == [] and world.robot.goal['status'] in ('CANCELED', 'SUCCEEDED', 'ABORTED')
    assert summary['results'].get('INTERRUPTED', 0) + summary['results'].get('SUCCEEDED', 0) >= 1


def test_node_records_an_error_and_exits_with_status_one(tmp_path, monkeypatch):
    def broken(self, x, y, yaw):
        raise RuntimeError('action client broke')
    code, out, world = run_node(tmp_path, monkeypatch, send=broken, timeout=30.)
    assert code == 1
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['error'] == 'RuntimeError: action client broke' and summary['end'] == 'error'
    assert any(level == 'error' and 'action client broke' in text for level, text in world.logs)


def test_node_stop_while_nav2_has_not_answered_a_goal(tmp_path, monkeypatch):
    code, out, world = run_node(tmp_path, monkeypatch, stop_when='chosen', hold=True)
    assert code == 0 and world.exited_at - world.stopped_at < 3.          # not the 10 s acceptance limit
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['end'] == er.STOPPED and summary['results'] == {'NOT_SENT': 1}


def test_node_ends_when_nav2_refuses_every_goal(tmp_path, monkeypatch):
    code, out, world = run_node(tmp_path, monkeypatch, refuse=True, speedup=10.)
    assert code == 0
    assert json.loads((out / 'summary.json').read_text())['end'] == 'Nav2 refused 5 goals in a row'
