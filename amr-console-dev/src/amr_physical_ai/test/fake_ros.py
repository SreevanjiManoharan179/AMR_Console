"""A stand-in for the ROS 2 Python API that explore_node.py uses, backed by the offline robot (tests only).

FakeRos installs fake rclpy, message and tf2_ros modules into sys.modules. A world thread advances
exploration_sim.SimRobot in simulation time much faster than real time and plays SLAM Toolbox, Gazebo
truth, TF and Nav2 to the node: /map every 5 s, truth at 50 Hz, NavigateToPose goals driven by the
robot, an active bt_navigator, a global costmap and a SaveMap service that writes placeholder files.
"""
from __future__ import annotations

import math
from pathlib import Path
import sys
import threading
import time
import types

import numpy as np

from amr_physical_ai.exploration_sim import SimRobot

STATUS_CODE = {'SUCCEEDED': 4, 'CANCELED': 5, 'ABORTED': 6}


def ns(**fields):
    return types.SimpleNamespace(**fields)


def stamp(seconds):
    sec = int(math.floor(seconds))
    return ns(sec=sec, nanosec=int(round((seconds - sec) * 1e9)))


class Future:
    def __init__(self, done=lambda: True, value=None):
        self._done, self._value, self._callbacks = done, value, []

    def done(self):
        return bool(self._done())

    def exception(self):
        return None

    def result(self):
        return self._value() if callable(self._value) else self._value

    def add_done_callback(self, callback):
        if self.done():
            callback(self)
        else:
            self._callbacks.append(callback)


class World:
    """The simulated robot, its clock and the topics the node subscribes to."""

    def __init__(self, truth, *, spawn=(-2., -.5, 0.), speedup=400., step_s=.05, refuse_goals=False,
                 hold_acceptance=False):
        self.robot = SimRobot(truth, step_s=step_s)
        self.spawn, self.speedup, self.step_s = spawn, speedup, step_s
        self.lock = threading.RLock()
        self.subscriptions, self.services, self.logs = {}, {}, []
        self.saved, self.goals_sent = [], 0
        self.refuse_goals, self.hold_acceptance = refuse_goals, hold_acceptance
        self.shutdown = threading.Event()
        self.published_at = None
        self.truth_at = -1.
        self.thread = None

    def now(self):
        with self.lock:
            return self.robot.t

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _deliver(self, topic, message):
        for callback in list(self.subscriptions.get(topic, [])):
            callback(message)

    def _run(self):
        while not self.shutdown.is_set():
            with self.lock:
                self.robot._advance(self.step_s)
                published, grid, t = self.robot.published_at, self.robot.published, self.robot.t
                pose = self.robot.pose()
            if published != self.published_at:
                self.published_at = published
                self._deliver('/map', self.map_message(grid, t))
            if t - self.truth_at >= .02:
                self.truth_at = t
                self._deliver('/amr/ground_truth', self.truth_message(pose, t))
            self._deliver('/global_costmap/costmap', ns())
            time.sleep(self.step_s / self.speedup)

    def map_message(self, grid, t):
        rows, cols = grid.data.shape
        return ns(header=ns(frame_id='map', stamp=stamp(t)),
                  info=ns(width=cols, height=rows, resolution=grid.resolution,
                          origin=ns(position=ns(x=grid.origin[0], y=grid.origin[1]),
                                    orientation=ns(x=0., y=0., z=0., w=1.))),
                  data=grid.data.ravel().tolist())

    def truth_message(self, pose, t):
        sx, sy, syaw = self.spawn
        x, y, yaw = pose
        wx = sx + x * math.cos(syaw) - y * math.sin(syaw)
        wy = sy + x * math.sin(syaw) + y * math.cos(syaw)
        q = ns(x=0., y=0., z=math.sin((yaw + syaw) / 2), w=math.cos((yaw + syaw) / 2))
        transform = ns(child_frame_id='turtlebot3_waffle', header=ns(frame_id='default', stamp=stamp(t)),
                       transform=ns(translation=ns(x=wx, y=wy, z=0.), rotation=q))
        other = ns(child_frame_id='wheel', header=ns(frame_id='default', stamp=stamp(t)),
                   transform=ns(translation=ns(x=0., y=0., z=0.), rotation=ns(x=0., y=0., z=0., w=1.)))
        return ns(transforms=[other, transform])


class FakeRos:
    """Install the fake modules; use as a context manager around importing and running the node."""

    MODULES = ('rclpy', 'rclpy.action', 'rclpy.qos', 'rclpy.executors', 'rclpy.utilities', 'rclpy.time',
               'action_msgs', 'action_msgs.msg', 'lifecycle_msgs', 'lifecycle_msgs.srv', 'nav2_msgs',
               'nav2_msgs.action', 'nav_msgs', 'nav_msgs.msg', 'slam_toolbox', 'slam_toolbox.srv', 'std_srvs',
               'std_srvs.srv', 'tf2_msgs', 'tf2_msgs.msg', 'tf2_ros')

    def __init__(self, world):
        self.world = world
        self.saved_modules = {}

    def __enter__(self):
        for name in self.MODULES:
            self.saved_modules[name] = sys.modules.get(name)
        modules = self.build()
        sys.modules.update(modules)
        return self

    def __exit__(self, *exc):
        for name, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self.world.shutdown.set()

    def build(self):
        world = self.world
        m = {name: types.ModuleType(name) for name in self.MODULES}
        shutdown = world.shutdown

        class Time:
            def __init__(self, seconds=0.):
                self.nanoseconds = int(seconds * 1e9)

            def to_msg(self):
                return stamp(self.nanoseconds * 1e-9)

        class Clock:
            def now(self):
                return Time(world.now())

        class Logger:
            def info(self, text):
                world.logs.append(('info', text))

            def warning(self, text):
                world.logs.append(('warn', text))

            def error(self, text):
                world.logs.append(('error', text))

        class Client:
            def __init__(self, name):
                self.name = name

            def service_is_ready(self):
                return True

            def wait_for_service(self, timeout_sec=None):
                return True

            def call_async(self, request):
                if self.name == '/bt_navigator/get_state':
                    return Future(value=ns(current_state=ns(id=3, label='active')))
                if self.name == '/slam_toolbox/save_map':
                    stem = Path(request.name.data)
                    stem.with_suffix('.yaml').write_text('image: final_map.pgm\n')
                    stem.with_suffix('.pgm').write_bytes(b'P5\n1 1\n255\n\x00')
                    world.saved.append(str(stem))
                    return Future(value=ns(result=0))
                raise AssertionError(f'unexpected service {self.name}')

        class Node:
            def __init__(self, name):
                self.name = name

            def create_subscription(self, _type, topic, callback, _qos):
                world.subscriptions.setdefault(topic, []).append(callback)

            def create_client(self, _type, name):
                return Client(name)

            def create_service(self, _type, name, callback):
                world.services[name.replace('~', '/' + self.name)] = callback

            def get_clock(self):
                return Clock()

            def get_logger(self):
                return Logger()

            def destroy_node(self):
                pass

        class GoalHandle:
            def __init__(self, goal):
                self.goal, self.accepted = goal, True

            def get_result_async(self):
                return Future(done=lambda: world.robot.status(self.goal) is not None,
                              value=lambda: ns(status=STATUS_CODE[world.robot.status(self.goal)]))

            def cancel_goal_async(self):
                with world.lock:
                    world.robot.cancel(self.goal)
                return Future()

        class ActionClient:
            def __init__(self, _node, _type, name):
                assert name == 'navigate_to_pose'

            def server_is_ready(self):
                return True

            def send_goal_async(self, goal):
                if world.hold_acceptance:            # Nav2 never answers the goal request
                    return Future(done=lambda: False)
                if world.refuse_goals:
                    return Future(value=ns(accepted=False))
                p = goal.pose.pose
                yaw = 2 * math.atan2(p.orientation.z, p.orientation.w)
                assert goal.pose.header.frame_id == 'map'
                with world.lock:
                    world.goals_sent += 1
                    handle = GoalHandle(world.robot.send(p.position.x, p.position.y, yaw))
                return Future(value=handle)

        class Executor:
            """Spins until ROS shuts down or the executor is shut down, as rclpy's does."""

            def __init__(self, context=None):
                self.stopped = threading.Event()

            def add_node(self, node):
                pass

            def spin(self):
                while not shutdown.is_set() and not self.stopped.wait(.01):
                    pass

            def shutdown(self, timeout_sec=None):
                self.stopped.set()
                return True

        class Buffer:
            def __init__(self, node=None):
                pass

            def lookup_transform(self, target, source, _time):
                assert (target, source) == ('map', 'base_footprint')
                with world.lock:
                    x, y, yaw = world.robot.pose()
                    t = world.robot.t
                return ns(header=ns(stamp=stamp(t)), transform=ns(
                    translation=ns(x=x, y=y, z=0.), rotation=ns(x=0., y=0., z=math.sin(yaw / 2), w=math.cos(yaw / 2))))

        class Goal:
            def __init__(self):
                self.pose = ns(header=ns(frame_id='', stamp=None), pose=ns(
                    position=ns(x=0., y=0., z=0.), orientation=ns(x=0., y=0., z=0., w=1.)))

        m['rclpy'].init = lambda args=None: None
        m['rclpy'].create_node = Node
        m['rclpy'].ok = lambda: not shutdown.is_set()
        m['rclpy'].try_shutdown = shutdown.set
        m['rclpy'].time = m['rclpy.time']
        m['rclpy.time'].Time = Time
        m['rclpy.action'].ActionClient = ActionClient
        m['rclpy.qos'].QoSProfile = lambda **kw: ns(**kw)
        m['rclpy.qos'].DurabilityPolicy = ns(TRANSIENT_LOCAL='transient_local')
        m['rclpy.qos'].ReliabilityPolicy = ns(RELIABLE='reliable')
        m['rclpy.executors'].SingleThreadedExecutor = Executor
        m['rclpy.executors'].ExternalShutdownException = type('ExternalShutdownException', (Exception,), {})
        m['rclpy.utilities'].remove_ros_args = lambda args: args[:args.index('--ros-args')] if '--ros-args' in args \
            else list(args)
        m['action_msgs.msg'].GoalStatusArray = object
        m['lifecycle_msgs.srv'].GetState = ns(Request=lambda: ns())
        m['nav2_msgs.action'].NavigateToPose = ns(Goal=Goal)
        m['nav_msgs.msg'].OccupancyGrid = object
        m['slam_toolbox.srv'].SaveMap = ns(Request=lambda: ns(name=ns(data='')))
        m['std_srvs.srv'].Trigger = ns(Request=lambda: ns())
        m['tf2_msgs.msg'].TFMessage = object
        m['tf2_ros'].Buffer = Buffer
        m['tf2_ros'].TransformListener = lambda buffer, node: None
        return m

    def stop(self):
        """What the console's STOP does: call the explorer's ~/stop."""
        response = ns(success=None, message=None)
        return self.world.services['/amr_explorer/stop'](ns(), response)

    def recovery(self, name, goal_id):
        """A Nav2 recovery behavior goal appearing on its action status topic."""
        status = ns(goal_info=ns(goal_id=ns(uuid=np.array(goal_id, dtype=np.uint8))), status=2)
        for callback in self.world.subscriptions.get(f'/{name}/_action/status', []):
            callback(ns(status_list=[status]))
