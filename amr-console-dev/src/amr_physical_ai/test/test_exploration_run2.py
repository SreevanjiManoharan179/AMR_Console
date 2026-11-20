"""Full exploration-run regression: recorded manager-cabin grid and bounded return handling. No ROS."""
from dataclasses import replace
import json
import math
from pathlib import Path

import numpy as np
import pytest

from amr_physical_ai import exploration as ex
from amr_physical_ai import exploration_run as er

DATA = Path(__file__).parent / 'data'


def manager():
    meta = json.loads((DATA / 'nearest_run2_manager.json').read_text())
    with np.load(DATA / 'nearest_run2_manager.npz') as saved:
        grid = ex.Map(saved['data'].copy(), float(saved['resolution']), tuple(saved['origin']))
    return grid, meta['decision']['pose'][:2]


def paths_and_views(grid, pose, settings):
    free, occ, unknown = ex.masks(grid.data)
    passable = ex.clearance(free, grid.resolution) >= settings.clearance_m
    start = ex.snap(passable, grid.cell(*pose), round(settings.snap_radius_m / grid.resolution))
    paths = ex.Paths(passable, grid.resolution, start)
    labels, clusters = ex.frontier_clusters(free, unknown, settings.min_frontier_cells)
    candidates = []
    for k in clusters:
        for cell in ex.viewpoints(labels, k, np.isfinite(paths.distance), paths.distance,
                                  grid.resolution, settings, np.zeros_like(free)):
            if ex.visible_unknown(occ, unknown, cell, grid.resolution, settings).size >= settings.min_gain_cells:
                candidates.append((k, cell, float(paths.distance[cell])))
    return paths, labels, candidates


@pytest.mark.parametrize('method', ex.METHODS)
def test_manager_map_prefers_far_goal_when_far_eligible_view_exists(method):
    grid, pose = manager()
    explorer = ex.Explorer(method)
    paths, _, candidates = paths_and_views(grid, pose, explorer.settings)
    assert any(distance < .6 for _, _, distance in candidates)
    assert any(distance >= .6 for _, _, distance in candidates)
    goal = explorer.next_goal(grid, pose)
    assert goal.kind == 'frontier' and goal.path_m >= .6 - 1e-9
    assert paths.path(grid.cell(goal.x, goal.y))


@pytest.mark.parametrize('method', ex.METHODS)
def test_manager_cluster_is_banned_after_two_unchanged_arrivals_and_reset_by_new_map(method):
    grid, pose = manager()
    explorer = ex.Explorer(method)
    explorer.next_goal(grid, pose)  # establish this map's identity
    _, labels, candidates = paths_and_views(grid, pose, explorer.settings)
    _, clusters = ex.frontier_clusters(*ex.masks(grid.data)[::2], 3)
    cluster = next(k for k, size in clusters.items() if size == 139)
    rows, cols = np.nonzero(labels == cluster)
    points = np.column_stack([grid.origin[0] + (cols + .5) * grid.resolution,
                              grid.origin[1] + (rows + .5) * grid.resolution])
    cells = [cell for k, cell, distance in candidates if k == cluster]
    assert len(cells) >= 2
    for cell in cells[:2]:
        decision = ex.Decision('frontier', *grid.point(*cell), frontier_xy=points)
        assert explorer.report_arrival(decision, grid, map_changed=False)
    key = ex.frontier_key(points)
    assert explorer.unchanged_clusters[key] == 2
    assert len(explorer.unchanged_views) == 2 and not explorer.failed
    next_goal = explorer.next_goal(grid, pose)
    assert next_goal.kind == 'frontier'
    assert ex.frontier_key(next_goal.frontier_xy) != key
    changed = ex.Map(grid.data.copy(), grid.resolution, grid.origin)
    changed.data[0, 0] = 0 if changed.data[0, 0] != 0 else -1
    explorer.next_goal(changed, pose)
    assert explorer.unchanged_clusters == {} and explorer.unchanged_views == []


@pytest.mark.parametrize('method', ex.METHODS)
def test_nearest_short_fallback_is_allowed_once_per_map(method):
    grid, pose = manager()
    settings = replace(ex.Settings(), min_goal_step_m=1e6)
    _, _, candidates = paths_and_views(grid, pose, settings)
    explorer = ex.Explorer(method, settings)
    goal = explorer.next_goal(grid, pose)
    assert goal.kind == 'frontier'
    assert goal.path_m == pytest.approx(min(distance for _, _, distance in candidates))
    # Even without a successful arrival the same short fallback must not loop.
    assert explorer.next_goal(grid, pose).kind in ('done', 'home')
    assert explorer.next_goal(grid, pose).kind == 'done'


class Records(er.NullRecorder):
    def __init__(self): self.events = []
    def event(self, name, **values): self.events.append(dict(event=name, **values))


class ClockRobot:
    def __init__(self, *, home_duration=1., home_outcome='SUCCEEDED', home_error=0., stop_at=math.inf):
        self.map, self.position = manager()
        self.t = 0.
        self.sent = []
        self.cancelled = []
        self.home_duration, self.home_outcome, self.home_error = home_duration, home_outcome, home_error
        self.stop_at = stop_at
        self.goal = None

    def grid(self): return self.map
    def now(self): return self.t
    def map_stamp(self): return self.t  # new publications, identical cells
    def map_snapshot(self): return self.map, self.t, self.t
    def pose(self):
        if self.goal and self.goal['home'] and self.status(self.goal) == 'SUCCEEDED':
            return self.home_error, 0., 0.
        return *self.position, 0.

    def sleep(self, seconds):
        self.t += seconds
        return self.t >= self.stop_at

    def elapse(self, seconds): pass
    def send(self, x, y, yaw):
        goal = dict(home=(x, y, yaw) == (0., 0., 0.), sent=self.t, target=(x, y, yaw))
        self.sent.append(goal)
        self.goal = goal
        return None if goal['home'] and self.home_outcome == 'REJECTED' else goal

    def status(self, goal):
        if goal['home'] and self.t - goal['sent'] >= self.home_duration:
            return self.home_outcome
        return None

    def cancel(self, goal):
        self.cancelled.append(goal)
        return True


def run_clock(robot, settings, method='nearest'):
    records = Records()
    explorer = ex.Explorer(method)
    loop = er.ExplorationLoop(explorer, robot, records, settings)
    return loop, records


@pytest.mark.parametrize('method', ex.METHODS)
def test_unchanged_publications_stall_at_120_seconds_then_return(method):
    robot = ClockRobot()
    loop, records = run_clock(robot, replace(er.RunSettings(), poll_s=1., goal_timeout_base_s=9999.), method)
    assert loop.run() == 'stalled: map content unchanged for 120 s'
    home = [g for g in robot.sent if g['home']]
    assert len(home) == 1 and home[0]['sent'] == 120.
    assert robot.cancelled and not robot.cancelled[0]['home']
    returns = [e for e in records.events if e['event'] == 'return']
    assert len(returns) == 1 and returns[0]['success'] and returns[0]['arrival_error_m'] == 0.


def test_exploration_ends_at_1710_and_return_is_bounded_by_total_1800():
    robot = ClockRobot(home_duration=9999.)
    settings = replace(er.RunSettings(), poll_s=1., map_stall_s=9999., goal_timeout_base_s=9999.)
    loop, records = run_clock(robot, settings)
    assert loop.run() == 'time limit: exploration stopped at 1710 s; 90 s reserved for return'
    assert robot.sent[-1]['home'] and robot.sent[-1]['sent'] == 1710.
    assert robot.now() == 1800.
    assert loop.return_home['outcome'] == 'INTERRUPTED' and not loop.return_home['success']
    assert robot.sent[-1] in robot.cancelled


@pytest.mark.parametrize('outcome,error,success', [('SUCCEEDED', .29, True), ('SUCCEEDED', .31, False),
                                                  ('ABORTED', 0., False), ('REJECTED', 0., False)])
def test_return_requires_both_nav2_success_and_estimated_error_within_point3(outcome, error, success):
    robot = ClockRobot(home_outcome=outcome, home_error=error)
    loop, records = run_clock(robot, replace(er.RunSettings(), poll_s=1.))
    loop.t0 = 0.
    loop._go_home('stalled: test')
    assert loop.return_home['outcome'] == outcome and loop.return_home['success'] is success
    if outcome == 'SUCCEEDED':
        assert loop.return_home['arrival_error_m'] == error
    assert len([e for e in records.events if e['event'] == 'return']) == 1


def test_stop_during_return_cancels_without_retrying_home():
    robot = ClockRobot(home_duration=9999., stop_at=121.)
    loop, records = run_clock(robot, replace(er.RunSettings(), poll_s=1., goal_timeout_base_s=9999.))
    assert loop.run() == er.STOPPED
    assert len([g for g in robot.sent if g['home']]) == 1
    assert robot.sent[-1] in robot.cancelled
    assert loop.return_home['outcome'] == 'INTERRUPTED' and not loop.return_home['success']
