"""One autonomous exploration run: the goal loop and its records, shared by the live node and the offline robot.

No ROS here. ExplorationLoop asks the explorer (exploration.Explorer) for a goal, has the robot drive to it
and reports the outcome back, until the explorer has nothing left, the time limit passes or STOP comes.
The live node (explore_node.py) and the offline robot (exploration_sim.SimRobot) both offer:

  now()             simulation time, s
  grid()            the latest published map as exploration.Map, or None before the first one
  map_stamp()       simulation time at which that map was received
  pose()            (x, y, yaw) of the robot in the map frame, or None
  send(x, y, yaw)   start a goal and return its handle, or None if the goal is refused; a goal sent
                    while another is active replaces it (Nav2 preemption: no stop in between)
  status(goal)      None while the goal runs, then 'SUCCEEDED', 'ABORTED' or 'CANCELED'
  cancel(goal)      ask the goal to stop
  sleep(seconds)    wait; True means the run must end now (STOP or shutdown)
  elapse(seconds)   decision time: the offline robot advances its clock, the live robot does nothing
and optionally map_snapshot() -> (map, received, header stamp) read together, and stop_reason().

Recorder writes the run folder: events.jsonl, maps/NNNN.npz (every published map), truth.jsonl,
progress.json while running and summary.json at the end. scripts/score_exploration.py scores it.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time

import numpy as np

from .exploration import Decision, Explorer, Settings, still_open, map_signature

STOPPED = 'stopped by the console STOP'


@dataclass(frozen=True)
class RunSettings:
    """Run rules of the live loop; frozen with the explorer's Settings before the scored runs."""
    poll_s: float = 0.05               # wait between checks of a running goal
    resolved_check_s: float = 1.0      # how often the target frontier is checked while driving
    max_speed_mps: float = 0.26        # nav2_mppi.yaml vx_max, for the goal time limit
    goal_timeout_base_s: float = 60.0  # a goal may take 60 s plus three times its path at full speed
    goal_timeout_factor: float = 3.0
    arrival_map_wait_s: float = 7.0    # SLAM Toolbox publishes the map every 5 s: wait for one after arriving
    time_limit_s: float = 1800.0       # 30 minutes of simulation time per run
    return_reserve_s: float = 90.0     # exploration ends at 1710 s; remaining time is for home
    map_stall_s: float = 120.0         # unchanged map contents, even if publications keep arriving
    max_rejections: int = 5            # Nav2 refusing this many goals in a row ends the run
    lost_limit_s: float = 20.0         # no map or robot pose for this long ends the run
    truth_period_s: float = 0.5        # truth and SLAM pose samples, 2 Hz
    progress_period_s: float = 2.0     # progress.json refresh while driving


def settings_digest(settings, run_settings):
    """SHA256 of both settings objects (sorted JSON): the value pinned when the settings are frozen."""
    text = json.dumps({'explorer': asdict(settings), 'run': asdict(run_settings)}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def plain(value):
    """JSON-safe copy: numpy scalars and arrays become Python values, non-finite floats None."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    return value


def map_frame(world_x, world_y, world_yaw, spawn):
    """Gazebo world pose -> map frame. SLAM's map starts at the spawn pose: (0, 0) at spawn, x along its yaw."""
    sx, sy, syaw = spawn
    dx, dy = world_x - sx, world_y - sy
    c, s = math.cos(syaw), math.sin(syaw)
    yaw = math.atan2(math.sin(world_yaw - syaw), math.cos(world_yaw - syaw))
    return c * dx + s * dy, -s * dx + c * dy, yaw


class Recorder:
    """The run folder. One writer thread per file: events and progress from the loop, maps and truth from
    the callbacks that receive them."""

    def __init__(self, out):
        self.out = Path(out)
        if self.out.exists() and any(self.out.iterdir()):
            raise FileExistsError(f'The exploration folder {self.out} already holds records; choose a new one.')
        (self.out / 'maps').mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._events = (self.out / 'events.jsonl').open('a', buffering=1)
        self._truth = (self.out / 'truth.jsonl').open('a', buffering=1)
        self.snapshots = 0
        self.truth_samples = 0
        self.closed = False

    def event(self, name, **fields):
        line = json.dumps(plain({'event': name, **fields}), allow_nan=False)
        with self.lock:
            if not self.closed:
                self._events.write(line + '\n')

    def snapshot(self, data, resolution, origin, stamp, **extra):
        """Save one published map (int8 grid, row 0 at the lowest y) as maps/NNNN.npz; returns its number.
        stamp is the simulation time the map was received; extra holds other scalars (header stamp)."""
        buffer = io.BytesIO()
        np.savez_compressed(buffer, data=np.asarray(data, dtype=np.int8), resolution=float(resolution),
                            origin=np.asarray(origin[:2], dtype=float), stamp=float(stamp),
                            **{key: float(value) for key, value in extra.items()})
        with self.lock:
            if self.closed:
                return None
            index = self.snapshots
            self.snapshots += 1
        _atomic_bytes(self.out / 'maps' / f'{index:04d}.npz', buffer.getvalue())
        return index

    def truth(self, sample):
        line = json.dumps(plain(sample), allow_nan=False)
        with self.lock:
            if not self.closed:
                self._truth.write(line + '\n')
                self.truth_samples += 1

    def progress(self, **state):
        _atomic_bytes(self.out / 'progress.json',
                      (json.dumps(plain(dict(state, updated_wall=time.time())), indent=2, allow_nan=False)
                       + '\n').encode())

    def summary(self, value):
        _atomic_bytes(self.out / 'summary.json',
                      (json.dumps(plain(value), indent=2, allow_nan=False) + '\n').encode())

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self._events.close()
                self._truth.close()


class NullRecorder:
    """Records nothing: for offline runs that need only the loop's counts."""
    snapshots = truth_samples = 0

    def event(self, name, **fields):
        pass

    def snapshot(self, data, resolution, origin, stamp, **extra):
        return None

    def truth(self, sample):
        pass

    def progress(self, **state):
        pass

    def summary(self, value):
        pass

    def close(self):
        pass


def _atomic_bytes(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


class ExplorationLoop:
    """Goals from the explorer, driven by the robot, outcomes reported back; every step is recorded."""

    def __init__(self, explorer, robot, recorder, settings=RunSettings(), log=None):
        self.explorer, self.robot, self.recorder, self.settings = explorer, robot, recorder, settings
        self.log = log or (lambda text, level='info': None)
        self.active = None              # the goal still driving, if any
        self.goals = 0                  # goals sent (frontier and home)
        self.results = Counter()        # outcome -> count
        self.rejected = 0
        self.t0 = None
        self.end = None
        self.compute_s = []
        self._progress_at = -math.inf
        self.last_map_identity = None
        self.map_changed_at = None
        self.return_home = None

    def _exploration_end(self):
        """Observe actual map changes while driving as well as between goals."""
        grid = self.robot.grid()
        if grid is not None:
            identity = map_signature(grid)
            if identity != self.last_map_identity:
                self.last_map_identity = identity
                self.map_changed_at = self.robot.now()
        elapsed = self.robot.now() - self.t0
        explore_limit = max(0., self.settings.time_limit_s - self.settings.return_reserve_s)
        if elapsed >= explore_limit:
            return (f'time limit: exploration stopped at {explore_limit:.0f} s; '
                    f'{self.settings.return_reserve_s:.0f} s reserved for return')
        if (self.map_changed_at is not None
                and self.robot.now() - self.map_changed_at >= self.settings.map_stall_s):
            return f'stalled: map content unchanged for {self.settings.map_stall_s:.0f} s'
        return None

    def _stopped(self):
        reason = getattr(self.robot, 'stop_reason', None)
        return reason() if reason else STOPPED

    def elapsed(self):
        return None if self.t0 is None else self.robot.now() - self.t0

    def run(self):
        r, rs = self.robot, self.settings
        self.t0 = r.now()
        pose = r.pose()
        self.recorder.event('start', t=self.t0, method=self.explorer.method, pose=pose,
                            map_stamp=r.map_stamp())
        self.log(f'Exploring with {self.explorer.method} from '
                 + (f'({pose[0]:.2f}, {pose[1]:.2f}).' if pose else 'an unknown pose.'))
        lost_since, end = None, None
        while end is None:
            if r.sleep(0.):
                end = self._stopped()
                break
            end = self._exploration_end()
            if end is not None:
                break
            grid, pose = r.grid(), r.pose()
            if grid is None or pose is None:
                lost_since = r.now() if lost_since is None else lost_since
                if r.now() - lost_since >= rs.lost_limit_s:
                    end = 'no map or robot pose for %.0f s' % rs.lost_limit_s
                elif r.sleep(.5):
                    end = self._stopped()
                continue
            lost_since = None
            started = time.monotonic()
            decision = self.explorer.next_goal(grid, pose[:2])
            compute = time.monotonic() - started
            self.compute_s.append(compute)
            r.elapse(compute)
            if decision.kind == 'done':
                self._decision(decision, compute, None)
                end = decision.reason
                break
            self.goals += 1
            self._decision(decision, compute, self.goals)
            if r.sleep(0.):                     # STOP came while the goal was chosen: do not send it
                self._result(self.goals, 'NOT_SENT', 0.)
                end = self._stopped()
                break
            end = self._drive(decision, self.goals)
        # STOP and readiness/navigation faults do not authorize a new movement.
        # Finish, stall and the reserved-time boundary do return every method.
        if (end.startswith('stalled:') or end.startswith('time limit:')) and not r.sleep(0.):
            if self.return_home is None:
                return_end = self._go_home(end)
                if return_end is not None:
                    end = return_end
        self._finish(end)
        return end

    def _record_return(self, outcome, reason):
        pose = self.robot.pose()
        error = math.dist(pose[:2], self.explorer.home) if pose is not None else None
        self.return_home = dict(t=self.robot.now(), outcome=outcome, reason=reason,
                                arrival_error_m=error, pose=pose,
                                success=bool(outcome == 'SUCCEEDED' and error is not None
                                             and error <= self.explorer.settings.home_tolerance_m))
        self.recorder.event('return', **self.return_home)

    def _go_home(self, reason):
        r = self.robot
        if self.active is not None:
            r.cancel(self.active)
            self.active = None
        if r.sleep(0.):
            return
        self.explorer.phase = 'home'
        self.goals += 1
        decision = Decision('home', *self.explorer.home, 0., method=self.explorer.method,
                            phase='home', reason=f'return home after {reason}')
        self._decision(decision, 0., self.goals)
        return self._drive(decision, self.goals)

    def _decision(self, decision, compute, index):
        r = self.robot
        self.recorder.event('decision', t=r.now(), index=index, kind=decision.kind, x=decision.x, y=decision.y,
                            yaw=decision.yaw, path_m=decision.path_m, gain_m2=decision.gain_m2,
                            frontier_cells=len(decision.frontier_xy), frontiers_left=decision.frontiers_left,
                            phase=decision.phase, reason=decision.reason, compute_s=compute, pose=r.pose())
        if index is not None:
            self.log(f'Goal {index}: {decision.reason}, {decision.path_m:.1f} m away ({decision.phase} phase).')
        self._progress(force=True)

    def _result(self, index, outcome, duration, **extra):
        self.results[outcome] += 1
        self.recorder.event('result', t=self.robot.now(), index=index, outcome=outcome, duration_s=duration,
                            pose=self.robot.pose(), **extra)
        self.log(f'Goal {index}: {outcome} after {duration:.1f} s.', 'info' if outcome in (
            'SUCCEEDED', 'RESOLVED') else 'warn')
        self._progress(force=True)

    def _progress(self, force=False, state='exploring'):
        now = self.robot.now()
        if force or now - self._progress_at >= self.settings.progress_period_s:
            self._progress_at = now
            self.recorder.progress(state=state, method=self.explorer.method, phase=self.explorer.phase,
                                   goals=self.goals, results=dict(self.results), elapsed_s=self.elapsed(),
                                   end=self.end)

    def _drive(self, decision, index):
        """Drive one goal; None to go on, or the reason the run ends."""
        r, rs, s = self.robot, self.settings, self.explorer.settings
        previous, self.active = self.active, None
        returning = decision.kind == 'home'
        before = r.grid()
        before_signature = map_signature(before) if before is not None else None
        goal = r.send(decision.x, decision.y, decision.yaw)
        if goal is None:
            if previous is not None:            # the refused goal did not replace the old one: stop it
                r.cancel(previous)
            if r.sleep(0.):                     # STOP came while Nav2 was asked: not a refusal
                self._result(index, 'NOT_SENT', 0.)
                if returning:
                    self._record_return('NOT_SENT', self._stopped())
                return self._stopped()
            self.rejected += 1
            self._result(index, 'REJECTED', 0.)
            if returning:
                self._record_return('REJECTED', decision.reason)
                return None
            if self.rejected >= rs.max_rejections:
                return f'Nav2 refused {self.rejected} goals in a row'
            return self._stopped() if r.sleep(1.) else None
        self.rejected = 0
        self.active = goal
        sent = r.now()
        limit = (rs.return_reserve_s if returning else
                 rs.goal_timeout_base_s + rs.goal_timeout_factor * decision.path_m / rs.max_speed_mps)
        check = sent + rs.resolved_check_s
        while True:
            status = r.status(goal)
            if status is not None:
                break
            now = r.now()
            if returning and now - self.t0 >= rs.time_limit_s:
                r.cancel(goal)
                self.active = None
                self._result(index, 'INTERRUPTED', now - sent)    # _finish cancels it
                self._record_return('INTERRUPTED', 'total run time limit reached during return')
                return None
            if not returning:
                end = self._exploration_end()
                if end is not None:
                    self._result(index, 'INTERRUPTED', now - sent)
                    return end
            if now - sent >= limit:
                r.cancel(goal)
                self.active = None
                if not returning:
                    self.explorer.report_failure(decision.x, decision.y)
                self._result(index, 'TIMEOUT', now - sent, limit_s=limit)
                if returning:
                    self._record_return('TIMEOUT', decision.reason)
                return None
            if decision.kind == 'frontier' and now >= check:
                check = now + rs.resolved_check_s
                grid = r.grid()
                if grid is not None and still_open(decision.frontier_xy, grid)[0] < s.min_frontier_cells:
                    # Keep driving: the next goal replaces this one without a stop.
                    self._result(index, 'RESOLVED', now - sent)
                    return None
            self._progress()
            if r.sleep(rs.poll_s):
                self._result(index, 'INTERRUPTED', r.now() - sent)  # _finish cancels it
                if returning:
                    self._record_return('INTERRUPTED', self._stopped())
                return self._stopped()
        self.active = None
        arrived = r.now()
        if returning:
            self._result(index, status, arrived - sent)
            self._record_return(status, decision.reason)
            return None
        if status == 'SUCCEEDED':
            # The map judged, with when it was received and its own stamp (SLAM Toolbox: its latest scan).
            grid, received, header = self._fresh_grid(arrived)
            before = len(self.explorer.given_up)
            map_changed = grid is not None and map_signature(grid) != before_signature
            still = bool(grid is not None and self.explorer.report_arrival(
                decision, grid, map_changed=map_changed))
            self._result(index, 'SUCCEEDED', arrived - sent, still_open=still,
                         given_up=len(self.explorer.given_up) > before, arrived_t=arrived,
                         map_received_t=received, map_header_t=header, map_changed=map_changed)
        else:
            self.explorer.report_failure(decision.x, decision.y)
            self._result(index, status, arrived - sent)
        return None

    def _fresh_grid(self, after):
        """The first map received after `after`, or the latest one after the wait limit, as
        (map, received, header stamp), read together when the robot offers map_snapshot()."""
        r = self.robot
        deadline = r.now() + self.settings.arrival_map_wait_s
        while (r.map_stamp() is None or r.map_stamp() < after) and r.now() < deadline:
            if self._exploration_end() is not None:
                break
            if r.sleep(self.settings.poll_s):
                break
        snapshot = getattr(r, 'map_snapshot', None)
        return snapshot() if snapshot else (r.grid(), r.map_stamp(), None)

    def _finish(self, end):
        r = self.robot
        if self.active is not None:
            r.cancel(self.active)
            self.active = None
        self.end = end
        self.recorder.event('finish', t=r.now(), elapsed_s=self.elapsed(), end=end, goals=self.goals,
                            results=dict(self.results), phase=self.explorer.phase, pose=r.pose(),
                            given_up=len(self.explorer.given_up), failed=len(self.explorer.failed))
        self._progress(force=True, state='finished')
        self.log(f'Exploration finished: {end} ({self.goals} goals, {self.elapsed():.0f} s).')

    def summary(self, **extra):
        """The summary.json content; the caller adds the map files and its own context."""
        compute = sorted(self.compute_s)
        return dict(schema=1, method=self.explorer.method, t0=self.t0, elapsed_s=self.elapsed(), end=self.end,
                    goals=self.goals, results=dict(self.results), given_up=len(self.explorer.given_up),
                    failed_goals=len(self.explorer.failed), phase=self.explorer.phase,
                    return_home=self.return_home,
                    decision_compute_s={'count': len(compute),
                                        'median': compute[len(compute) // 2] if compute else None,
                                        'max': compute[-1] if compute else None},
                    settings=asdict(self.explorer.settings), run_settings=asdict(self.settings),
                    settings_sha256=settings_digest(self.explorer.settings, self.settings),
                    snapshots=self.recorder.snapshots, truth_samples=self.recorder.truth_samples, **extra)


def new_explorer(method, settings=Settings()):
    """The explorer every run uses: home is the map origin, where the robot starts."""
    return Explorer(method, settings, home=(0., 0.))
