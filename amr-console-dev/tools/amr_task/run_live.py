"""Delivery task runner: attaches a terminal to a running console navigation session.

    python3 -m tools.amr_task.run_live --console-run <run_id>

Does not launch or shut down the console, Gazebo or Nav2, reset localization, read the
console token or use its HTTP API.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import math
from pathlib import Path
import select
import sys
import threading
import time
import uuid

from .clarify import Conversation, Grounder
from .executor import EventLog, Executor, NavigationResult
from .live_session import ApprovalGate, SessionAttachment
from .memory import SpatialMemory
from .tags import ROOT, Tags


class CallbackPump:
    """One executor owns all callbacks, including while stdin is idle."""
    def __init__(self, executor, node):
        self.executor, self.node, self.error = executor, node, None
        executor.add_node(node)
        self.thread = threading.Thread(target=self._run, name='amr-task-callbacks', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.executor.spin()
        except Exception as exc:
            self.error = exc

    def check(self):
        if self.error is not None:
            raise RuntimeError('ROS callback processing failed') from self.error
        if not self.thread.is_alive():
            raise RuntimeError('ROS callback processing stopped')

    def close(self):
        self.executor.shutdown(timeout_sec=2.)
        self.thread.join(timeout=2.)
        if self.thread.is_alive():
            raise RuntimeError('ROS callback thread did not stop')
        self.executor.remove_node(self.node)


class CommanderNavigator:
    """BasicNavigator's action client with bounded asynchronous dispatch/cancel.

    Its installed goToPose/cancelTask methods wait indefinitely. Using the
    same client's futures keeps operator cancellation and session checks live.
    """
    def __init__(self, tags, before_goal, audit=lambda *args, **kwargs: None, spawn=(-2.0, -0.5, 0.0)):
        import rclpy
        from action_msgs.msg import GoalStatusArray
        from nav_msgs.msg import OccupancyGrid
        from nav2_simple_commander.robot_navigator import BasicNavigator
        from rclpy.parameter import Parameter
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
        from tf2_ros import Buffer, TransformListener
        self.rclpy, self.tags, self.before_goal = rclpy, tags, before_goal
        self.nav = BasicNavigator(node_name='amr_task_' + uuid.uuid4().hex[:8])
        self.nav.set_parameters([Parameter('use_sim_time', value=True)])
        self.buffer = Buffer(node=self.nav)
        self.listener = TransformListener(self.buffer, self.nav)
        self.map = None
        self.statuses = []
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
        self.nav.create_subscription(OccupancyGrid, '/map', self._map, qos)
        self.nav.create_subscription(GoalStatusArray, '/navigate_to_pose/_action/status',
                                     lambda msg: setattr(self, 'statuses', msg.status_list), qos)
        # Simulation only: the Console's truth bridge (ground_truth:=true) publishes the robot's pose
        # in the Gazebo world frame. Map (0, 0) is the spawn point, so map = R(-yaw) (world - spawn).
        # On a real robot the topic is absent and every truth field stays empty.
        from tf2_msgs.msg import TFMessage
        self.spawn, self.truth_sample = spawn, None
        self.nav.create_subscription(TFMessage, '/amr/ground_truth', self._truth, 10)
        self.send_future = self.result_future = self.handle = None
        self.own_uuid = None
        self.completed_uuid = None
        self.dispatch_started = None
        self.audit = audit
        self.last_pose_age_s = None
        self.pump = CallbackPump(SingleThreadedExecutor(context=self.nav.context), self.nav)

    def _map(self, msg):
        from src.amr_physical_ai.amr_physical_ai.command_pipeline import Grid
        if msg.header.frame_id != 'map':
            self.map = None
            return
        import numpy as np
        p, q = msg.info.origin.position, msg.info.origin.orientation
        yaw = math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))
        self.map = Grid(np.asarray(msg.data).reshape(msg.info.height, msg.info.width),
                        msg.info.resolution, (p.x, p.y, yaw))

    def _truth(self, msg):
        for transform in msg.transforms:
            if transform.child_frame_id != 'turtlebot3_waffle':
                continue
            t, stamp = transform.transform.translation, transform.header.stamp
            seconds = stamp.sec + stamp.nanosec*1e-9
            if seconds <= 0:  # an unstamped bridge message: use the receive time on the node's clock
                seconds = self.nav.get_clock().now().nanoseconds*1e-9
            sx, sy, syaw = self.spawn
            dx, dy = t.x - sx, t.y - sy
            self.truth_sample = (math.cos(syaw)*dx + math.sin(syaw)*dy, -math.sin(syaw)*dx + math.cos(syaw)*dy,
                                 seconds)

    def truth(self, max_age_s=1.):
        """The latest truth pose in the map frame, or None when there is no fresh sample."""
        sample = getattr(self, 'truth_sample', None)  # absent on a real robot or a bare test double
        if sample is None:
            return None
        age = self.nav.get_clock().now().nanoseconds*1e-9 - sample[2]
        if not -.1 <= age <= max_age_s or not all(math.isfinite(v) for v in sample[:2]):
            return None
        return sample[0], sample[1], age

    def spin(self):
        self.pump.check()
        time.sleep(.02)

    def pose(self):
        from rclpy.time import Time
        transform = self.buffer.lookup_transform('map', 'base_footprint', Time())
        stamp = transform.header.stamp.sec + transform.header.stamp.nanosec*1e-9
        age = self.nav.get_clock().now().nanoseconds*1e-9 - stamp
        self.last_pose_age_s = age
        if stamp <= 0 or not -.1 <= age <= 1.:
            raise RuntimeError(f'Fresh map-to-base pose required (age <= 1 s); observed age={age:.6f} s')
        t = transform.transform.translation
        if not all(math.isfinite(v) for v in (t.x, t.y)):
            raise RuntimeError('Nonfinite live pose')
        return t.x, t.y

    def check_scene(self, *, idle=False):
        if self.map is None or not self.tags.grid.same_map(self.map):
            raise RuntimeError('Live /map does not match the calibrated proposed_system exact map')
        self.pose()
        for status in self.statuses:
            goal_id = list(status.goal_info.goal_id.uuid)
            # The result may arrive before the corresponding terminal status.
            # A completed own goal is safe to ignore; other clients are not.
            if goal_id == self.completed_uuid:
                continue
            if status.status in (1, 2, 3) and (idle or goal_id != self.own_uuid):
                raise RuntimeError('Another NavigateToPose goal is active; use one goal source at a time')

    def ready(self, timeout=15.):
        deadline, last = time.monotonic()+timeout, 'No live data'
        while time.monotonic() < deadline:
            self.before_goal()
            self.spin()
            try:
                self.check_scene(idle=True)
                if self.nav.nav_to_pose_client.server_is_ready():
                    return
            except Exception as exc:
                last = str(exc)
        raise RuntimeError('Session attachment readiness failed: '+last)

    def start(self, destination):
        from geometry_msgs.msg import PoseStamped
        from nav2_msgs.action import NavigateToPose
        if self.send_future is not None or self.result_future is not None:
            raise RuntimeError('A task goal is still active')
        self.spin()
        self.check_scene(idle=True)
        if not self.nav.nav_to_pose_client.server_is_ready():
            raise RuntimeError('NavigateToPose server is unavailable')
        self.before_goal()
        self.map.validate_point(destination.x, destination.y, .52)
        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = 'map'
        goal.pose.header.stamp = self.nav.get_clock().now().to_msg()
        goal.pose.pose.position.x, goal.pose.pose.position.y = destination.x, destination.y
        goal.pose.pose.orientation.z = math.sin(destination.yaw/2)
        goal.pose.pose.orientation.w = math.cos(destination.yaw/2)
        # Supply the UUID so our accepted status cannot look foreign before its
        # acceptance future is processed.
        from unique_identifier_msgs.msg import UUID
        self.own_uuid = list(uuid.uuid4().bytes)
        self.send_future = self.nav.nav_to_pose_client.send_goal_async(goal, goal_uuid=UUID(uuid=self.own_uuid))
        self.dispatch_started = time.monotonic()
        self.audit('dispatch', destination=destination.key, goal_uuid=self.own_uuid,
                   tf_age_s=self.last_pose_age_s)

    def poll(self):
        self.spin()
        self.check_scene()
        if self.send_future is not None:
            if not self.send_future.done():
                if time.monotonic()-self.dispatch_started > 10:
                    raise RuntimeError('Navigation acceptance timed out')
                return None
            self.handle = self.send_future.result()
            self.send_future = None
            if not self.handle.accepted:
                self.completed_uuid = self.own_uuid
                x, y = self.pose()
                return NavigationResult('REJECTED', x, y, 'map->base_footprint TF; localization estimate')
            self.result_future = self.handle.get_result_async()
        if self.result_future is None or not self.result_future.done():
            return None
        response = self.result_future.result()
        self.result_future = None
        self.completed_uuid = self.own_uuid
        self.nav.status = response.status
        status = self.nav.getResult().name
        x, y = self.pose()
        truth = self.truth()
        self.audit('navigation_result', status=status, x=x, y=y, tf_age_s=self.last_pose_age_s,
                   pose_source='map->base_footprint TF; localization estimate, not Gazebo truth',
                   truth=None if truth is None else {'x': truth[0], 'y': truth[1], 'age_s': truth[2]})
        return NavigationResult(status, x, y, 'map->base_footprint TF; localization estimate, not Gazebo truth',
                                *((truth[0], truth[1], 'Gazebo truth (simulation only), map frame')
                                  if truth else (None, None, None)))

    def cancel(self):
        def wait(future, seconds):
            end = time.monotonic()+seconds
            while not future.done() and time.monotonic() < end:
                self.spin()
            if not future.done():
                raise RuntimeError('Own-goal cancellation unresolved; use Console STOP and inspect the run')
            return future.result()
        if self.send_future is not None:
            self.handle = wait(self.send_future, 5.)
            self.send_future = None
            if self.handle.accepted:
                self.result_future = self.handle.get_result_async()
        if self.handle is not None and self.handle.accepted:
            if self.result_future is not None and not self.result_future.done():
                response = wait(self.handle.cancel_goal_async(), 5.)
                if response.return_code not in (0, 3):
                    raise RuntimeError('Own-goal cancellation was refused; use Console STOP')
                wait(self.result_future, 5.)
        self.completed_uuid = self.own_uuid
        self.result_future = self.handle = None

    def close(self):
        self.pump.close()
        self.nav.destroy_node()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console-run', required=True, help='Existing data/logs/console/<run_id> directory name')
    args = parser.parse_args(argv)
    tags = Tags()
    gate = ApprovalGate(tags)  # Refuses proposed tags before importing or initializing ROS.
    attachment = SessionAttachment(args.console_run)
    attachment.configure_environment()
    lock = (ROOT/'log/task_runner.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another task runner is attached')
    import os
    session_id = datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    session_dir = ROOT/'data/logs/task'/session_id
    log = EventLog(session_dir)
    os.environ['ROS_LOG_DIR'] = str(session_dir/'ros')
    navigator = executor = None
    import rclpy
    try:
        rclpy.init()
        def check():
            gate.check()
            attachment.check()
        navigator = CommanderNavigator(tags, check, log.emit)
        navigator.ready()
        log.emit('ready', tf_age_s=navigator.last_pose_age_s)
        try:
            memory = SpatialMemory(ROOT/'data/memory'/f"{tags.data['map_id']}.json", tags.data['map_id'])
            log.emit('memory_open', path=str(memory.path), entries=len(memory.entries))
        except Exception as exc:  # an unreadable memory file disables memory; tasks still run
            memory = None
            log.emit('memory_error', error=repr(exc)[:300])
            print('Memory file unreadable; running without memory:', exc)
        executor = Executor(Conversation(Grounder(tags), gate.q_hat), navigator, log,
                            before_goal=check, announce=print, memory=memory, run_id=session_id)
        print('Attached in simulation. Use only this terminal for goals; do not send Console/RViz goals concurrently.')
        print('Type a task; answer questions, then yes/no. Delivery acknowledgements: taken / not here / wrong person.')
        print('Type cancel to cancel this task; quit to detach. Events:', log.path)
        while rclpy.ok():
            check()
            navigator.spin()
            if executor.active:
                navigator.check_scene()
            before = executor.state
            executor.tick()
            if executor.state != before and executor.state in Executor.TERMINAL:
                print(executor.state, executor.outcome)
            if executor.cancellation_uncertain:
                raise RuntimeError('Cancellation uncertain; no further tasks accepted. Use Console STOP.')
            if not select.select([sys.stdin], [], [], .1)[0]:
                continue
            text = sys.stdin.readline()
            if not text or text.strip().lower() == 'quit':
                break
            text = text.strip()
            try:
                if text.lower() == 'cancel':
                    executor.cancel()
                elif executor.state == 'WAIT_ACK':
                    executor.acknowledge(text)
                elif executor.conversation.decision and executor.conversation.decision.kind in ('ask', 'confirm'):
                    print(executor.answer(text))
                else:
                    print(executor.command(text))
            except ValueError as exc:
                print(exc)
    except KeyboardInterrupt:
        print('Detaching; cancelling this terminal’s active goal.')
    finally:
        if executor and executor.state not in Executor.TERMINAL:
            executor.cancel()
        if navigator:
            navigator.close()
        if rclpy.ok():
            rclpy.shutdown()
        log.close()
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
