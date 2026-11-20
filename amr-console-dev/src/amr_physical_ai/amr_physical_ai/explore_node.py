"""Autonomous exploration node: the shared explorer drives Nav2 on the live SLAM map and records the run.

tb3_mapping.launch.py starts it with explore:=<method> (console action map.explore), beside SLAM Toolbox
(online async, its default settings) and Nav2 (navigation_launch.py with nav2_mppi.yaml):

  python3 -m amr_physical_ai.explore_node --method ours --out <folder> [--world proposed_system]
      [--spawn -2.0 -0.5 0.0] [--nav2-params <yaml>] --ros-args -p use_sim_time:=true

It waits for the map, the robot's map pose, Nav2's global costmap and an active bt_navigator, then runs
exploration_run.ExplorationLoop (the loop the offline robot runs too) until no frontier is left, the
time limit passes or the console's STOP calls ~/stop. Every /map message is kept as maps/NNNN.npz,
Gazebo truth is sampled at 2 Hz with the SLAM pose, and at the end SLAM Toolbox saves final_map.yaml
and .pgm. The node never publishes velocity: Nav2 drives. On shutdown it cancels its goal and exits
with status 0; an internal error is logged, written to summary.json and exits with status 1.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import threading
import time
import traceback

import numpy as np

from .exploration import METHODS, Map, Settings
from .exploration_run import (STOPPED, ExplorationLoop, Recorder, RunSettings, file_digest, map_frame,
                              new_explorer)

NAV2_STATUS = {4: 'SUCCEEDED', 5: 'CANCELED', 6: 'ABORTED'}
READY_TIMEOUT_S = 180.        # wall seconds for SLAM and Nav2 to come up
ACCEPT_TIMEOUT_S = 10.        # wall seconds for Nav2 to accept or refuse a goal
CANCEL_WAIT_S = 5.            # wall seconds for a cancelled goal to end
SAVE_TIMEOUT_S = 30.          # wall seconds for SLAM Toolbox to save the final map
SOURCES = ('exploration.py', 'exploration_run.py', 'explore_node.py')
RECOVERIES = ('spin', 'backup', 'drive_on_heading', 'wait', 'assisted_teleop')   # Nav2 behavior_server actions


def parse(argv):
    parser = argparse.ArgumentParser(description='Explore with Nav2 on the live SLAM map and record the run.')
    parser.add_argument('--method', choices=METHODS, required=True)
    parser.add_argument('--out', type=Path, required=True, help='new folder for the run records')
    parser.add_argument('--world', default='', help='world label recorded with the run')
    parser.add_argument('--spawn', nargs=3, type=float, default=(-2., -.5, 0.), metavar=('X', 'Y', 'YAW'),
                        help='Gazebo spawn pose; the SLAM map starts there')
    parser.add_argument('--nav2-params', default='', help='Nav2 parameter file, recorded with its hash')
    return parser.parse_args(argv)


def grid_from_message(message):
    """exploration.Map from a nav_msgs/OccupancyGrid in the map frame with an unrotated origin, else None."""
    info = message.info
    q = info.origin.orientation
    yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
    if message.header.frame_id != 'map' or abs(yaw) > 1e-6 or info.width == 0 or info.height == 0:
        return None
    data = np.array(message.data, dtype=np.int8).reshape(info.height, info.width)
    return Map(data, float(info.resolution), (float(info.origin.position.x), float(info.origin.position.y)))


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class ExploreNode:
    """The ROS side: the robot interface of ExplorationLoop over /map, TF and NavigateToPose."""

    def __init__(self, args, settings=Settings(), run_settings=RunSettings()):
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from action_msgs.msg import GoalStatusArray
        from lifecycle_msgs.srv import GetState
        from nav2_msgs.action import NavigateToPose
        from nav_msgs.msg import OccupancyGrid
        from slam_toolbox.srv import SaveMap
        from std_srvs.srv import Trigger
        from tf2_msgs.msg import TFMessage
        from tf2_ros import Buffer, TransformListener

        self.rclpy, self.args = rclpy, args
        self.settings, self.run_settings = settings, run_settings
        self.recorder = Recorder(args.out)
        self.node = rclpy.create_node('amr_explorer')
        self.logger = self.node.get_logger()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = None                      # (Map, simulation time received, map header stamp)
        self.costmap_seen = False
        self.truth_at = -math.inf
        self.warned = set()
        self.loop = None
        self.summary_written = False
        self.started_wall = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.Goal, self.SaveRequest, self.StateRequest = NavigateToPose.Goal, SaveMap.Request, GetState.Request
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.node.create_subscription(OccupancyGrid, '/map', self._on_map, latched)
        self.node.create_subscription(OccupancyGrid, '/global_costmap/costmap', self._on_costmap, latched)
        self.node.create_subscription(TFMessage, '/amr/ground_truth', self._on_truth, 20)
        # Each Nav2 recovery behavior goal (spin, backup, ...) is recorded once, for the safety score.
        self.recovery_goals = set()
        for name in RECOVERIES:
            self.node.create_subscription(GoalStatusArray, f'/{name}/_action/status',
                                          lambda message, name=name: self._on_recovery(name, message), latched)
        self.buffer = Buffer(node=self.node)
        self.listener = TransformListener(self.buffer, self.node)
        self.action = ActionClient(self.node, NavigateToPose, 'navigate_to_pose')
        self.save_client = self.node.create_client(SaveMap, '/slam_toolbox/save_map')
        self.bt_state = self.node.create_client(GetState, '/bt_navigator/get_state')
        self.node.create_service(Trigger, '~/stop', self._on_stop)

    # ---- callbacks (executor thread); an error is reported once and never stops the executor
    def _warn_once(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            self.logger.warning(text)

    def _on_map(self, message):
        try:
            grid = grid_from_message(message)
            if grid is None:
                self._warn_once('map', 'A /map message was not an unrotated map-frame grid; it is ignored.')
                return
            received = self.now()
            stamp = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
            with self.lock:
                self.latest = (grid, received, stamp)
            self.recorder.snapshot(grid.data, grid.resolution, grid.origin, received, header_stamp=stamp)
        except Exception as exc:                # noqa: BLE001 - keep the executor alive, report once
            self._warn_once('map_error', f'Map handling failed: {exc}')

    def _on_costmap(self, _message):
        self.costmap_seen = True

    def _on_truth(self, message):
        try:
            for transform in message.transforms:
                if transform.child_frame_id != 'turtlebot3_waffle' or transform.header.frame_id != 'default':
                    continue
                stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9
                if stamp - self.truth_at < self.run_settings.truth_period_s:
                    return
                self.truth_at = stamp
                p, q = transform.transform.translation, transform.transform.rotation
                x, y, yaw = map_frame(p.x, p.y, yaw_of(q), self.args.spawn)
                slam, slam_t = self._lookup()
                self.recorder.truth({'t': stamp, 'x': x, 'y': y, 'yaw': yaw, 'world': [p.x, p.y],
                                     'slam': slam, 'slam_t': slam_t, 'source': '/amr/ground_truth'})
                return
        except Exception as exc:                # noqa: BLE001
            self._warn_once('truth_error', f'Truth recording failed: {exc}')

    def _on_recovery(self, name, message):
        try:
            for status in message.status_list:
                key = (name, tuple(int(b) for b in status.goal_info.goal_id.uuid))
                if key not in self.recovery_goals:
                    self.recovery_goals.add(key)
                    self.recorder.event('recovery', t=self.now(), behavior=name, status=int(status.status))
        except Exception as exc:                # noqa: BLE001
            self._warn_once('recovery_error', f'Recovery recording failed: {exc}')

    def _on_stop(self, _request, response):
        finished = self.loop is not None and self.loop.end is not None
        self.stop_event.set()
        response.success = True
        response.message = 'Exploration had already finished.' if finished else 'Exploration stop requested.'
        return response

    # ---- the robot interface of ExplorationLoop (main thread)
    def now(self):
        return self.node.get_clock().now().nanoseconds * 1e-9

    def _lookup(self):
        try:
            transform = self.buffer.lookup_transform('map', 'base_footprint', self.rclpy.time.Time())
        except Exception:                       # noqa: BLE001 - no transform yet
            return None, None
        t, q = transform.transform.translation, transform.transform.rotation
        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec * 1e-9
        return [t.x, t.y, yaw_of(q)], stamp

    def pose(self):
        pose, _ = self._lookup()
        return tuple(pose) if pose else None

    def grid(self):
        with self.lock:
            return self.latest[0] if self.latest else None

    def map_stamp(self):
        with self.lock:
            return self.latest[1] if self.latest else None

    def map_snapshot(self):
        """(map, simulation time received, header stamp) of the latest map, read together."""
        with self.lock:
            return self.latest if self.latest else (None, None, None)

    def _wait(self, future, timeout, stoppable=False):
        end = time.monotonic() + timeout
        while not future.done() and time.monotonic() < end and self.rclpy.ok():
            if stoppable and self.stop_event.is_set():
                break
            time.sleep(.01)
        return future.done()

    def send(self, x, y, yaw):
        goal = self.Goal()
        goal.pose.header.frame_id = 'map'
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x, goal.pose.pose.position.y = float(x), float(y)
        goal.pose.pose.orientation.z, goal.pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        state = {'abandoned': False}
        accepted = self.action.send_goal_async(goal)

        def cancel_if_abandoned(future):
            # A goal accepted after we stopped waiting must not drive the robot.
            if state['abandoned'] and not future.exception():
                handle = future.result()
                if handle is not None and handle.accepted:
                    handle.cancel_goal_async()
        accepted.add_done_callback(cancel_if_abandoned)
        if not self._wait(accepted, ACCEPT_TIMEOUT_S, stoppable=True):
            state['abandoned'] = True           # STOP, or no answer: a late acceptance is cancelled
            return None
        if accepted.exception() is not None or not accepted.result().accepted:
            return None
        handle = accepted.result()
        return {'handle': handle, 'result': handle.get_result_async()}

    def status(self, goal):
        future = goal['result']
        if not future.done():
            return None
        if future.exception() is not None:
            return 'ABORTED'
        return NAV2_STATUS.get(future.result().status, 'ABORTED')

    def cancel(self, goal):
        try:
            goal['handle'].cancel_goal_async()
        except Exception as exc:                # noqa: BLE001 - context already shut down
            self._warn_once('cancel', f'Goal cancellation failed: {exc}')
            return False
        return self._wait(goal['result'], CANCEL_WAIT_S)

    def sleep(self, seconds):
        if seconds > 0:
            self.stop_event.wait(seconds)
        return self.stop_event.is_set() or not self.rclpy.ok()

    def stop_reason(self):
        return STOPPED if self.stop_event.is_set() else 'ROS shut down'

    def elapse(self, seconds):
        pass                                    # simulation time ran on while the decision was computed

    # ---- the run
    def _log(self, text, level='info'):
        (self.logger.info if level == 'info' else self.logger.warning)(text)

    def _bt_navigator_active(self):
        if not self.bt_state.service_is_ready():
            return False
        future = self.bt_state.call_async(self.StateRequest())
        return self._wait(future, 5.) and future.exception() is None and future.result().current_state.id == 3

    def _wait_ready(self):
        """Map, map pose, global costmap, NavigateToPose and an active bt_navigator; False on timeout or STOP."""
        end, report = time.monotonic() + READY_TIMEOUT_S, time.monotonic() + 15.
        while not self.sleep(.25):
            missing = [name for name, ready in (
                ('map', self.grid() is not None), ('map pose', self.pose() is not None),
                ('global costmap', self.costmap_seen), ('NavigateToPose', self.action.server_is_ready()),
                ('active bt_navigator', self._bt_navigator_active())) if not ready]
            if not missing:
                return True
            if time.monotonic() >= end:
                self.logger.error('Not ready after %.0f s; missing: %s.' % (READY_TIMEOUT_S, ', '.join(missing)))
                return False
            if time.monotonic() >= report:
                report = time.monotonic() + 15.
                self.logger.info('Waiting for ' + ', '.join(missing) + '.')
        return False

    def run(self):
        self.recorder.progress(state='waiting', method=self.args.method, phase=None, goals=0, results={},
                               elapsed_s=None, end=None)
        self.logger.info(f'Explorer {self.args.method}: waiting for SLAM Toolbox and Nav2.')
        if not self._wait_ready():
            end = STOPPED if self.stop_event.is_set() else f'SLAM or Nav2 not ready within {READY_TIMEOUT_S:.0f} s'
            self.recorder.event('finish', t=self.now(), elapsed_s=None, end=end, goals=0)
            self.write_summary(end)
            return end
        self.recorder.event('ready', t=self.now(), world=self.args.world, spawn=list(self.args.spawn))
        self.loop = ExplorationLoop(new_explorer(self.args.method, self.settings), self, self.recorder,
                                    self.run_settings, log=self._log)
        end = self.loop.run()
        stopped = self.stop_event.is_set() or not self.rclpy.ok()
        self.write_summary(end, final_map=self._save_final_map() if not stopped else
                           {'saved': False, 'reason': 'stopped before the end; the last maps/*.npz is the record'})
        return end

    def _save_final_map(self):
        stem = (self.args.out / 'final_map').resolve()
        request = self.SaveRequest()
        request.name.data = str(stem)
        if not self.save_client.wait_for_service(timeout_sec=5.):
            return {'saved': False, 'reason': 'SLAM Toolbox save_map service unavailable'}
        future = self.save_client.call_async(request)
        if not self._wait(future, SAVE_TIMEOUT_S) or future.exception() is not None:
            return {'saved': False, 'reason': 'save_map did not answer in time'}
        result = future.result().result
        files = [stem.with_suffix('.yaml'), stem.with_suffix('.pgm')]
        deadline = time.monotonic() + 5.
        while not all(f.is_file() for f in files) and time.monotonic() < deadline:
            time.sleep(.1)
        saved = result == 0 and all(f.is_file() for f in files)
        if saved:
            self.logger.info(f'Final map saved: {files[0]}')
        return {'saved': saved, 'result': int(result), 'yaml': files[0].name, 'pgm': files[1].name}

    def write_summary(self, end, error=None, **extra):
        root = Path(__file__).resolve().parent
        base = self.loop.summary() if self.loop is not None else {
            'schema': 1, 'method': self.args.method, 'end': end, 'goals': 0,
            'snapshots': self.recorder.snapshots, 'truth_samples': self.recorder.truth_samples}
        base.update(end=end, error=error, world=self.args.world, spawn=list(self.args.spawn),
                    out=str(self.args.out), started_wall=self.started_wall,
                    finished_wall=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    slam='slam_toolbox online_async_launch.py with its default parameters',
                    nav2_params=self.args.nav2_params or None,
                    nav2_params_sha256=file_digest(self.args.nav2_params) if self.args.nav2_params else None,
                    sources={name: file_digest(root / name) for name in SOURCES},
                    transport={key: os.environ.get(key) for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION',
                                                                    'RMW_IMPLEMENTATION')},
                    **extra)
        self.recorder.summary(base)
        self.summary_written = True

    def close(self, error=None):
        """Cancel a goal still driving, write the summary if the run did not, close the records."""
        if self.loop is not None and self.loop.active is not None:
            self.cancel(self.loop.active)
            self.loop.active = None
        if not self.summary_written:
            end = (self.loop.end if self.loop is not None and self.loop.end else
                   ('error' if error else 'shut down before the run finished'))
            try:
                self.write_summary(end, error=error)
            except Exception as exc:            # noqa: BLE001 - the records are best effort at shutdown
                self.logger.warning(f'Summary could not be written: {exc}')
        self.recorder.close()


def _spin(executor):
    try:
        executor.spin()
    except BaseException:                       # noqa: BLE001 - shutdown ends the spin; nothing to report
        pass


def main(argv=None):
    import rclpy
    from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
    from rclpy.utilities import remove_ros_args

    argv = list(sys.argv if argv is None else argv)
    args = parse(remove_ros_args(argv)[1:])
    rclpy.init(args=argv)
    explorer, executor, spin, code, error = None, None, None, 0, None
    try:
        explorer = ExploreNode(args)
        executor = SingleThreadedExecutor()
        executor.add_node(explorer.node)
        spin = threading.Thread(target=_spin, args=(executor,), daemon=True)
        spin.start()
        explorer.run()
        while not explorer.sleep(.2):           # finished: stay up (map.save still works) until STOP
            pass
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception as exc:                    # noqa: BLE001 - reported, recorded and the exit status says so
        code = 1
        text = ''.join(traceback.format_exception(exc))
        if explorer is not None:
            explorer.logger.error('Explorer failed: ' + text)
        else:
            print('Explorer failed: ' + text, file=sys.stderr)
        error = f'{type(exc).__name__}: {exc}'
    finally:
        if explorer is not None:
            try:
                explorer.close(error=error)
            except Exception as exc:            # noqa: BLE001
                print(f'Explorer close failed: {exc}', file=sys.stderr)
        # The spin thread ends before the node and the context go, so no thread is still inside
        # rclpy's wait set when the interpreter exits.
        if executor is not None:
            executor.shutdown(timeout_sec=1.)
        if spin is not None:
            spin.join(timeout=2.)
        if explorer is not None:
            explorer.node.destroy_node()
        rclpy.try_shutdown()
    return code


if __name__ == '__main__':
    sys.exit(main())
