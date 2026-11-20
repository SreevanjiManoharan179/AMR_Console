"""Regression from the nearest-method exploration run 20261020_072005; no ROS, simulator, or original-run writes."""
import json
from pathlib import Path

import numpy as np
import pytest

from amr_physical_ai import exploration as ex
from amr_physical_ai import exploration_run as er

DATA = Path(__file__).parent / 'data'


def saved_run():
    with np.load(DATA / 'nearest_stall_unchanged_map.npz') as data:
        grid = ex.Map(data['data'].copy(), float(data['resolution']), tuple(data['origin']))
    events = [json.loads(line) for line in (DATA / 'nearest_stall_events.jsonl').read_text().splitlines()]
    return grid, events


def recorded_decisions(grid, events):
    free, _, unknown = ex.masks(grid.data)
    labels, clusters = ex.frontier_clusters(free, unknown, 3)
    for event in events:
        if event['event'] != 'decision' or event['kind'] != 'frontier':
            continue
        candidates = [k for k, count in clusters.items() if count == event['frontier_cells']]
        assert len(candidates) == 1
        rows, cols = np.nonzero(labels == candidates[0])
        points = np.column_stack([grid.origin[0] + (cols + .5) * grid.resolution,
                                  grid.origin[1] + (rows + .5) * grid.resolution])
        yield ex.Decision('frontier', event['x'], event['y'], frontier_xy=points)


@pytest.mark.parametrize('method', ex.METHODS)
def test_recorded_unchanged_maps_do_not_exhaust_reachable_frontiers(method):
    grid, events = saved_run()
    explorer = ex.Explorer(method)
    explorer.next_goal(grid, events[1]['pose'][:2])
    attempts = list(recorded_decisions(grid, events))
    for decision in attempts:
        assert explorer.report_arrival(decision, grid, map_changed=False)
    assert explorer.failed == explorer.given_up == []
    next_goal = explorer.next_goal(grid, events[-1]['pose'][:2])
    assert next_goal.kind == 'frontier'
    assert next_goal.gain_m2 > 0
    assert grid.cell(next_goal.x, next_goal.y) not in [grid.cell(d.x, d.y) for d in attempts]
    free, _, _ = ex.masks(grid.data)
    passable = ex.clearance(free, grid.resolution) >= explorer.settings.clearance_m
    start = ex.snap(passable, grid.cell(*events[-1]['pose'][:2]), round(.6 / grid.resolution))
    assert ex.Paths(passable, grid.resolution, start).path(grid.cell(next_goal.x, next_goal.y))


def test_unchanged_grid_attempts_are_finite_and_not_reported_as_completion():
    grid, _ = saved_run()
    explorer = ex.Explorer('nearest')
    seen = set()
    free, _, _ = ex.masks(grid.data)
    bound = int((ex.clearance(free, grid.resolution) >= explorer.settings.clearance_m).sum())
    for _ in range(bound + 1):
        goal = explorer.next_goal(grid, (0., 0.))
        if goal.kind == 'done':
            assert goal.reason == 'stalled: no eligible frontier cluster remains on the unchanged map'
            break
        cell = grid.cell(goal.x, goal.y)
        assert cell not in seen
        seen.add(cell)
        assert explorer.report_arrival(goal, grid, map_changed=False)
    else:
        pytest.fail('Unchanged map permitted an unbounded sequence of repeated viewpoints')


def test_new_map_releases_only_unchanged_observation_attempts():
    grid, _ = saved_run()
    explorer = ex.Explorer('gain')
    goal = explorer.next_goal(grid, (0., 0.))
    explorer.report_arrival(goal, grid, map_changed=False)
    explorer.report_failure(-.4, 0.)
    assert explorer.unchanged_views
    updated = ex.Map(grid.data.copy(), grid.resolution, grid.origin)
    row, col = np.argwhere(updated.data == -1)[0]
    updated.data[row, col] = 0
    explorer.next_goal(updated, (0., 0.))
    assert explorer.unchanged_views == []
    assert explorer.failed == [(-.4, 0.)]
    assert explorer._banned(updated)[updated.cell(-.4, 0.)]


def test_loop_records_unchanged_map_without_frontier_abandonment():
    grid, _ = saved_run()
    explorer = ex.Explorer('nearest')
    decision = explorer.next_goal(grid, (0., 0.))

    class Robot:
        def grid(self): return grid
        def now(self): return 10.
        def send(self, *args): return object()
        def status(self, goal): return 'SUCCEEDED'
        def map_stamp(self): return 10.
        def map_snapshot(self): return grid, 10., 9.9
        def pose(self): return decision.x, decision.y, 0.

    class Recorder(er.NullRecorder):
        def __init__(self): self.events = []
        def event(self, kind, **values): self.events.append((kind, values))

    recorder = Recorder()
    loop = er.ExplorationLoop(explorer, Robot(), recorder)
    loop.t0 = 0.
    assert loop._drive(decision, 1) is None
    assert recorder.events[-1][1]['map_changed'] is False
    assert explorer.failed == explorer.given_up == []
    assert explorer.unchanged_views == [(decision.x, decision.y)]
