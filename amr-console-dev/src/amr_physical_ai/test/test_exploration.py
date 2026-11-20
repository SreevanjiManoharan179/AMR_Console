"""Explorer, offline grid simulator and exploration metrics (autonomous exploration, offline part). Offline: no ROS or simulator."""
import itertools
import math
from pathlib import Path
import sys

import numpy as np
import pytest

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / 'scripts'))
import exploration_metrics as em
import office_world
from amr_physical_ai import exploration as ex
from amr_physical_ai.exploration_sim import Truth, explored_ratio, run, scan

RES = .05


def two_rooms():
    """8 x 5 m, two rooms joined by a 1 m door, a box in the east room; map (0, 0) is in the west room."""
    rows, cols = 100, 160
    occupied = np.zeros((rows, cols), dtype=bool)
    occupied[:2, :] = occupied[-2:, :] = occupied[:, :2] = occupied[:, -2:] = True
    occupied[:, 80:82] = True                        # partition at x = 3 m
    occupied[40:60, 80:82] = False                   # door: y from 1 to 2 m
    occupied[60:80, 110:130] = True                  # box: x 4.5..5.5, y 2..3
    return Truth(occupied, ~occupied, RES, (-1., -1.))


def grid_of(truth, belief):
    return ex.Map(belief, truth.resolution, truth.origin)


def known(truth):
    """A belief that knows the whole world."""
    return np.where(truth.occupied, ex.OCCUPIED, ex.FREE).astype(np.int16)


def test_frontiers_are_free_cells_beside_unknown_grouped_by_size():
    data = np.full((6, 8), ex.UNKNOWN, dtype=np.int16)
    data[1:5, 1:4] = ex.FREE
    data[2, 2] = ex.OCCUPIED
    free, occupied, unknown = ex.masks(data)
    mask = ex.frontier_mask(free, unknown)
    assert mask.sum() == 10 and mask[1, 1] and mask[4, 3] and not mask[3, 2]   # (3, 2) has no unknown neighbour
    labels, clusters = ex.frontier_clusters(free, unknown, 3)
    assert len(clusters) == 1 and sum(clusters.values()) == int(mask.sum())
    assert ex.frontier_clusters(free, unknown, 100)[1] == {}
    edge = np.zeros((3, 3), dtype=np.int16)          # beyond the grid counts as unknown
    assert ex.frontier_mask(*ex.masks(edge)[::2]).sum() == 8


def test_clear_floor_keeps_the_robot_radius_from_walls_and_unknown():
    truth = two_rooms()
    passable = ex.clearance(truth.free, RES) >= ex.Settings().clearance_m
    assert passable[50, 40] and not passable[50, 4] and not passable[20, 77]   # 0.15 m from a wall
    assert passable[50, 81]                                                     # the door's middle


def test_paths_follow_the_floor_and_cost_diagonals_root_two():
    passable = np.zeros((5, 6), dtype=bool)
    passable[1:4, 1:5] = True
    paths = ex.Paths(passable, RES, (1, 1))
    assert paths.distance[3, 3] == pytest.approx(2 * math.sqrt(2) * RES)
    assert paths.distance[0, 0] == np.inf
    assert paths.path((3, 4))[0] == (1, 1) and paths.path((3, 4))[-1] == (3, 4)
    assert paths.between([(1, 1)], [(1, 4)])[0, 0] == pytest.approx(3 * RES)


def test_snap_moves_the_start_onto_clear_floor_within_the_radius():
    passable = np.zeros((20, 20), dtype=bool)
    passable[10:, :] = True
    assert ex.snap(passable, (12, 5), 4) == (12, 5)
    assert ex.snap(passable, (7, 5), 4) == (10, 5)
    assert ex.snap(passable, (2, 5), 4) is None


def test_gain_counts_unknown_cells_in_sight_and_stops_at_walls():
    data = np.full((60, 60), ex.FREE, dtype=np.int16)
    data[:, 40:] = ex.UNKNOWN
    occupied, unknown = data >= 65, data == ex.UNKNOWN
    settings = ex.Settings(sensor_range_m=1.5)
    open_view = ex.visible_unknown(occupied, unknown, (30, 30), RES, settings).size
    data[:, 35] = ex.OCCUPIED                         # a wall between the robot and the unknown
    blocked = ex.visible_unknown(data >= 65, data == ex.UNKNOWN, (30, 30), RES, settings).size
    assert open_view > 200 and blocked == 0


def test_viewpoints_are_reachable_near_their_frontier_and_spread():
    truth = two_rooms()
    belief = known(truth)
    belief[:, 120:] = ex.UNKNOWN
    free, _, unknown = ex.masks(belief)
    passable = ex.clearance(free, RES) >= .27
    paths = ex.Paths(passable, RES, (20, 20))
    labels, clusters = ex.frontier_clusters(free, unknown, 3)
    settings = ex.Settings()
    k = max(clusters, key=clusters.get)
    banned = np.zeros_like(passable)
    views = ex.viewpoints(labels, k, np.isfinite(paths.distance), paths.distance, RES, settings, banned)
    assert 1 < len(views) <= settings.viewpoints_per_frontier
    rows, cols = np.nonzero(labels == k)
    for v in views:
        assert passable[v] and np.isfinite(paths.distance[v])
        assert np.min(np.hypot(rows - v[0], cols - v[1])) * RES <= settings.viewpoint_radius_m + 1e-9
    banned[:] = True
    assert ex.viewpoints(labels, k, np.isfinite(paths.distance), paths.distance, RES, settings, banned) == []


def test_shortest_tour_is_exact_for_few_stops_and_a_permutation_for_many():
    rng = np.random.default_rng(7)
    pts = rng.uniform(0, 10, size=(6, 2))
    start = np.array([0., 0.])
    table = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    start_row = np.linalg.norm(pts - start, axis=1)
    order = ex.shortest_tour(start_row, table)

    def length(o):
        return start_row[o[0]] + sum(table[o[i], o[i + 1]] for i in range(len(o) - 1))

    assert length(order) == pytest.approx(min(length(list(p)) for p in itertools.permutations(range(6))))
    pts = rng.uniform(0, 10, size=(12, 2))
    table = np.linalg.norm(pts[:, None] - pts[None], axis=2)
    order = ex.shortest_tour(np.linalg.norm(pts, axis=1), table, exact_limit=5)
    assert sorted(order) == list(range(12))


def test_methods_choose_differently_on_the_same_map():
    truth = two_rooms()
    belief = known(truth)
    belief[2:98, 140:158] = ex.UNKNOWN                # a large unknown strip at the far east wall
    belief[86:98, 2:12] = ex.UNKNOWN                  # a small unknown corner near the start
    grid = grid_of(truth, belief)
    near = ex.Explorer('nearest').next_goal(grid, (0., 0.))
    gain = ex.Explorer('gain').next_goal(grid, (0., 0.))
    ours = ex.Explorer('ours').next_goal(grid, (0., 0.))
    assert near.kind == gain.kind == ours.kind == 'frontier'
    assert near.x < 1. and gain.x > 4. and ours.x > 4.         # nearest takes the corner; gain the big strip
    assert ours.phase == 'explore' and gain.gain_m2 > near.gain_m2


def test_ours_sweeps_small_frontiers_returns_home_once_then_stops():
    truth = two_rooms()
    belief = known(truth)
    belief[95:98, 2:5] = ex.UNKNOWN                   # only a small corner is left (6 frontier cells)
    grid = grid_of(truth, belief)
    explorer = ex.Explorer('ours')
    assert explorer.next_goal(grid, (5., 3.5)).phase == 'sweep'
    belief[:] = known(truth)
    home = explorer.next_goal(grid, (5., 3.5))
    assert home.kind == 'home' and (home.x, home.y) == (0., 0.)
    assert explorer.next_goal(grid, (0., 0.)).kind == 'done'
    assert ex.Explorer('gain').next_goal(grid, (5., 3.5)).kind == 'done'     # baselines just stop
    failing = ex.Explorer('ours')                     # home cannot be reached: it is not sent again
    assert failing.next_goal(grid, (5., 3.5)).kind == 'home'
    failing.report_failure(0., 0.)
    for _ in range(3):
        assert failing.next_goal(grid, (5., 3.5)).kind == 'done'


def test_failed_and_unresolved_goals_are_not_chosen_again():
    truth = two_rooms()
    belief = known(truth)
    belief[86:98, 2:12] = ex.UNKNOWN
    grid = grid_of(truth, belief)
    explorer = ex.Explorer('nearest')
    first = explorer.next_goal(grid, (0., 0.))
    explorer.report_failure(first.x, first.y)
    second = explorer.next_goal(grid, (0., 0.))
    assert math.dist((first.x, first.y), (second.x, second.y)) > ex.Settings().blacklist_radius_m
    assert explorer.report_arrival(second, grid)               # still open after a visit: one more try
    third = explorer.next_goal(grid, (0., 0.))
    assert third.kind == 'frontier' and explorer.report_arrival(third, grid)
    assert explorer.next_goal(grid, (0., 0.)).kind == 'done'  # given up after two open visits
    with pytest.raises(ValueError):
        ex.Explorer('random')


def test_a_scan_marks_free_floor_and_the_walls_it_hits():
    truth = two_rooms()
    belief = np.full(truth.free.shape, ex.UNKNOWN, dtype=np.int16)
    scan(belief, truth, (20, 20))
    assert belief[20, 20] == ex.FREE and belief[20, 1] == ex.OCCUPIED and belief[20, 80] == ex.OCCUPIED
    assert belief[20, 100] == ex.UNKNOWN                      # behind the partition
    assert 0.3 < explored_ratio(belief, truth) < 0.8


@pytest.mark.parametrize('method', ex.METHODS)
def test_every_method_explores_the_two_rooms(method):
    result = run(method, two_rooms(), time_limit_s=1200.)
    assert result['series'][-1][1] >= .99 and result['end'] == 'stalled: no eligible frontier cluster remains'
    times = [t for t, _, _ in result['series']]
    assert times == sorted(times) and result['distance_m'] > 3.
    if method == 'ours':
        assert math.dist(result['final_pose'], (0., 0.)) <= .3


def test_metrics_are_perfect_for_the_exact_map_itself():
    e_occ, e_free, e_res, e_origin = office_world.read_map(PKG / 'maps/proposed_system.yaml')
    data = np.where(e_occ, 100, np.where(e_free, 0, -1))
    score = em.score_map(data, e_res, e_origin[:2], e_occ, e_free, e_res, e_origin[:2])
    assert score['explored_ratio'] == score['wall_agreement'] == score['faces_seen'] == score['floor_iou'] == 1.
    assert score['corners']['ratio'] == 1. and score['corners']['corners'] > 40
    shifted = em.score_map(data, e_res, (e_origin[0] + .3, e_origin[1] + .3), e_occ, e_free, e_res, e_origin[:2])
    assert shifted['faces_seen'] < .5 and shifted['corners']['ratio'] < .5 and shifted['floor_iou'] < .9


def test_corners_of_a_single_room():
    occupied = np.zeros((20, 30), dtype=bool)
    occupied[:2] = occupied[-2:] = occupied[:, :2] = occupied[:, -2:] = True
    labels, count = em.corner_sites(occupied, ~occupied)
    assert count == 4                                 # four inside corners, no convex ones
    seen = em.corners_seen(occupied, RES, (0., 0.), occupied, ~occupied, RES, (0., 0.))
    assert seen == {'corners': 4, 'seen': 4, 'ratio': 1.}
    partial = occupied.copy()
    partial[:, :2] = False                            # the west wall was never mapped
    assert em.corners_seen(partial, RES, (0., 0.), occupied, ~occupied, RES, (0., 0.))['seen'] == 2


def test_time_scores_from_a_series():
    series = [(0., .2, 0.), (60., .5, 10.), (120., .91, 25.), (300., .995, 60.), (500., 1., 80.)]
    assert em.first_reaching(series, .9) == (120., 25.)
    assert em.first_reaching(series, .99) == (300., 60.)
    assert em.first_reaching(series[:3], .99) == (None, None)
    auc = (.2 * 60 + .5 * 60 + .91 * 180 + .995 * 60) / 360
    assert em.coverage_auc(series) == pytest.approx(auc)
    assert em.score_series(series)['T99_s'] == 300. and em.path_length([(0, 0), (3, 4), (3, 0)]) == 9.


def test_grids_with_other_origins_are_compared_cell_by_cell():
    data = np.full((10, 10), ex.FREE, dtype=np.int16)
    data[0] = ex.OCCUPIED
    values = em.on_exact(data, RES, (1., 1.), (20, 20), RES, (.5, .5))
    assert values[10, 10] == ex.OCCUPIED and values[19, 19] == ex.FREE and values[0, 0] == ex.UNKNOWN
