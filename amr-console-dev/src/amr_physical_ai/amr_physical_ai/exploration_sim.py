"""Offline grid simulator for the explorer (a "Level-0" simulator, as in Explore-Bench): no ROS, no Gazebo.

The robot knows its pose exactly. Its scan is a raycast on the exact map (the simulated LiDAR's 360
beams to 20 m), and it drives the shortest path over known clear floor at the Waffle's 0.26 m/s, with
a fixed time per goal for turning and settling. This is for developing and testing the explorer: the
scored results come from Gazebo runs with SLAM Toolbox and Nav2 (the exploration benchmark), never from here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .exploration import FREE, OCCUPIED, UNKNOWN, Explorer, Map, Paths, Settings, clearance, masks, snap, still_open
from .exploration_run import ExplorationLoop, NullRecorder, Recorder, RunSettings, new_explorer


@dataclass(frozen=True)
class Truth:
    occupied: np.ndarray        # row 0 at the lowest y
    free: np.ndarray
    resolution: float
    origin: tuple               # (x, y) of cell (0, 0)'s lower-left corner

    @classmethod
    def from_layout_map(cls, read_map, path):
        """From office_world.read_map(path): (occupied, free, resolution, origin)."""
        occupied, free, resolution, origin = read_map(path)
        if abs(origin[2]) > 1e-9:
            raise ValueError('Rotated map origins are not supported.')
        return cls(occupied, free, resolution, (origin[0], origin[1]))


def scan(belief, truth, cell, beams=360, max_range_m=20.):
    """Mark what one full scan from cell sees: free cells up to and the wall cell at each beam's end."""
    rows, cols = belief.shape
    angles = np.linspace(0., 2 * math.pi, beams, endpoint=False)
    steps = np.arange(1, int(max_range_m / truth.resolution) * 2 + 1) * .5
    rr = np.floor(cell[0] + .5 + np.outer(np.sin(angles), steps)).astype(np.int64)
    cc = np.floor(cell[1] + .5 + np.outer(np.cos(angles), steps)).astype(np.int64)
    inside = (rr >= 0) & (rr < rows) & (cc >= 0) & (cc < cols)
    r_in, c_in = np.where(inside, rr, 0), np.where(inside, cc, 0)
    blocked = ~inside | ~truth.free[r_in, c_in]           # walls, furniture and the unknown outside
    before = np.cumsum(blocked, axis=1) == 0
    belief[rr[before], cc[before]] = FREE
    first = blocked & (np.cumsum(blocked, axis=1) == 1) & inside
    hit_r, hit_c = rr[first], cc[first]
    wall = truth.occupied[hit_r, hit_c]
    belief[hit_r[wall], hit_c[wall]] = OCCUPIED
    belief[cell] = FREE


def explored_ratio(belief, truth):
    """Share of the exact map's free cells that the belief knows to be free."""
    return float(np.count_nonzero((belief == FREE) & truth.free) / np.count_nonzero(truth.free))


def run(method, truth, *, start=(0., 0.), settings=Settings(), speed=.26, turn_s=3., sense_step_m=.25,
        check_step_m=1., max_goals=400, time_limit_s=3600.):
    """Explore from start until the method stops; returns the explored-ratio series and totals."""
    belief = np.full(truth.free.shape, UNKNOWN, dtype=np.int16)
    res, origin = truth.resolution, truth.origin
    grid = Map(belief, res, origin)
    x, y = start
    t = travelled = 0.
    cell = grid.cell(x, y)
    scan(belief, truth, cell, max_range_m=settings.sensor_range_m)
    series = [(0., explored_ratio(belief, truth), 0.)]           # (time s, explored ratio, distance m)
    poses = [(x, y)]
    explorer = Explorer(method, settings, home=start)
    goals, end = [], 'goal limit'
    snapshots = {}                                  # belief when the explored ratio first reaches a level

    def record():
        ratio = explored_ratio(belief, truth)
        series.append((t, ratio, travelled))
        poses.append((x, y))
        for level in (.90, .99):
            if ratio >= level and level not in snapshots:
                snapshots[level] = belief.copy()

    while len(goals) < max_goals and t < time_limit_s:
        decision = explorer.next_goal(grid, (x, y))
        if decision.kind == 'done':
            end = decision.reason
            break
        free, _, _ = masks(belief)
        passable = clearance(free, res) >= settings.clearance_m
        here = snap(passable, grid.cell(x, y), int(round(settings.snap_radius_m / res)))
        path = Paths(passable, res, here).path(grid.cell(decision.x, decision.y))
        if len(path) < 2 and decision.kind == 'frontier':
            explorer.report_failure(decision.x, decision.y)
            goals.append((decision, 'unreachable'))
            continue
        goals.append((decision, 'sent'))
        t += turn_s
        since_scan = since_check = 0.
        for a, b in zip(path, path[1:]):
            step = res * math.hypot(b[0] - a[0], b[1] - a[1])
            t += step / speed
            travelled += step
            since_scan += step
            since_check += step
            x, y = grid.point(*b)
            if since_scan >= sense_step_m:
                scan(belief, truth, b, max_range_m=settings.sensor_range_m)
                record()
                since_scan = 0.
            if decision.kind == 'frontier' and since_check >= check_step_m:
                since_check = 0.
                if still_open(decision.frontier_xy, grid)[0] < settings.min_frontier_cells:
                    break                                 # the target frontier is resolved: re-plan
        scan(belief, truth, grid.cell(x, y), max_range_m=settings.sensor_range_m)
        record()
        if math.dist((x, y), (decision.x, decision.y)) <= 2 * res:
            explorer.report_arrival(decision, grid)
    return {'method': method, 'series': series, 'distance_m': travelled, 'time_s': t, 'goals': len(goals),
            'end': end, 'belief': belief, 'final_pose': (x, y), 'poses': poses, 'snapshots': snapshots}


class SimRobot:
    """An offline robot for exploration_run.ExplorationLoop: the live node's loop without ROS or Gazebo.

    Exact pose; a raycast scan every sense_step_m of travel; the map is published every map_period_s
    (SLAM Toolbox publishes every 5 s); a goal follows the shortest clear path on the published map at
    full speed after a turn_s start; a goal sent while another runs replaces it without the turn (Nav2
    preemption); a goal with no path is ABORTED after 1 s. With a recorder it writes the run folder as
    the live node does, with the exact pose as both truth and SLAM pose. Development and tests only.
    """

    def __init__(self, truth, *, start=(0., 0.), settings=Settings(), speed=.26, turn_s=3., sense_step_m=.25,
                 map_period_s=5., step_s=.1, recorder=None, truth_period_s=.5, count_compute=False,
                 stop_at=None):
        self.truth, self.settings, self.speed, self.turn_s = truth, settings, speed, turn_s
        self.sense_step_m, self.map_period_s, self.step_s = sense_step_m, map_period_s, step_s
        self.recorder, self.truth_period_s = recorder, truth_period_s
        self.count_compute, self.stop_at = count_compute, stop_at
        self.belief = np.full(truth.free.shape, UNKNOWN, dtype=np.int16)
        self.x, self.y, self.yaw = start[0], start[1], 0.
        self.t = self.travelled = self.since_scan = 0.
        self.goal = None
        self.sent = 0
        self._scan()
        self.published = self.published_at = None
        self.truth_at = -np.inf
        self._publish()
        self._record_truth()

    # The robot interface of ExplorationLoop.
    def now(self):
        return self.t

    def grid(self):
        return self.published

    def map_stamp(self):
        return self.published_at

    def map_snapshot(self):
        return self.published, self.published_at, self.published_at

    def pose(self):
        return (self.x, self.y, self.yaw)

    def send(self, x, y, yaw):
        replaced = self.goal is not None and self.goal['status'] is None
        if replaced:
            self.goal['status'] = 'ABORTED'
        grid, res = self.published, self.truth.resolution
        free, _, _ = masks(grid.data)
        passable = clearance(free, res) >= self.settings.clearance_m
        radius = int(round(self.settings.snap_radius_m / res))
        here, there = snap(passable, grid.cell(self.x, self.y), radius), snap(passable, grid.cell(x, y), radius)
        cells = Paths(passable, res, here).path(there) if here is not None and there is not None else []
        self.sent += 1
        self.goal = {'id': self.sent, 'status': None, 'next': 1, 'delay': 0. if replaced else self.turn_s,
                     'path': [grid.point(*c) for c in cells], 'fail_at': None if cells else self.t + 1.}
        return self.goal

    def status(self, goal):
        return goal['status']

    def cancel(self, goal):
        if goal['status'] is None:
            goal['status'] = 'CANCELED'
        return True

    def sleep(self, seconds):
        self._advance(seconds)
        return self.stop_at is not None and self.t >= self.stop_at

    def elapse(self, seconds):
        if self.count_compute:
            self._advance(seconds)

    # The simulated world.
    def _scan(self):
        scan(self.belief, self.truth, Map(self.belief, self.truth.resolution, self.truth.origin).cell(self.x, self.y),
             max_range_m=self.settings.sensor_range_m)

    def _publish(self):
        self.published = Map(self.belief.copy(), self.truth.resolution, self.truth.origin)
        self.published_at = self.t
        if self.recorder is not None:
            self.recorder.snapshot(self.published.data, self.truth.resolution, self.truth.origin, self.t)

    def _record_truth(self):
        self.truth_at = self.t
        if self.recorder is not None:
            self.recorder.truth({'t': self.t, 'x': self.x, 'y': self.y, 'yaw': self.yaw,
                                 'slam': [self.x, self.y, self.yaw], 'slam_t': self.t, 'source': 'offline robot'})

    def _advance(self, seconds):
        end = self.t + max(0., seconds)
        while self.t < end - 1e-9:
            step = min(self.step_s, end - self.t)
            self._move(step)
            self.t += step
            if self.t - self.published_at >= self.map_period_s - 1e-9:
                self._publish()
            if self.t - self.truth_at >= self.truth_period_s - 1e-9:
                self._record_truth()

    def _move(self, dt):
        goal = self.goal
        if goal is None or goal['status'] is not None:
            return
        if goal['fail_at'] is not None:
            if self.t + dt >= goal['fail_at']:
                goal['status'] = 'ABORTED'
            return
        used = min(dt, goal['delay'])
        goal['delay'] -= used
        budget = (dt - used) * self.speed
        points = goal['path']
        while budget > 1e-12 and goal['next'] < len(points):
            tx, ty = points[goal['next']]
            gap = math.hypot(tx - self.x, ty - self.y)
            if gap > 1e-12:
                self.yaw = math.atan2(ty - self.y, tx - self.x)
            step = min(gap, budget)
            if gap <= budget:
                self.x, self.y = tx, ty
                goal['next'] += 1
            else:
                self.x += (tx - self.x) * step / gap
                self.y += (ty - self.y) * step / gap
            budget -= step
            self.travelled += step
            self.since_scan += step
            if self.since_scan >= self.sense_step_m:
                self._scan()
                self.since_scan = 0.
        if goal['next'] >= len(points):
            self._scan()
            goal['status'] = 'SUCCEEDED'


def run_loop(method, truth, *, out=None, settings=Settings(), run_settings=RunSettings(), log=None, **robot):
    """Run the live loop (exploration_run.ExplorationLoop) on the offline robot; returns (loop, robot).
    With out, the run folder is written as the live node writes it."""
    recorder = Recorder(out) if out is not None else NullRecorder()
    sim = SimRobot(truth, settings=settings, recorder=recorder if out is not None else None, **robot)
    loop = ExplorationLoop(new_explorer(method, settings), sim, recorder, run_settings, log)
    try:
        loop.run()
        recorder.summary(loop.summary(world='offline', source='exploration_sim.SimRobot',
                                      distance_m=sim.travelled))
    finally:
        recorder.close()
    return loop, sim
