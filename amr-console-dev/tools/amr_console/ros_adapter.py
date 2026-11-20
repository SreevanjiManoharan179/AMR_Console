"""ROS operations inside one manual session worker; ROS imports are lazy."""
from __future__ import annotations

import math
from pathlib import Path
import threading
import time


def clamp_velocity(linear_x, angular_z):
    values = (linear_x, angular_z)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in values):
        raise ValueError('Drive-pad velocities must be finite numbers.')
    return max(-.26, min(.26, linear_x)), max(-1.82, min(1.82, angular_z))


# Drive-pad bumper (12 Oct 2026). Geometry is in base_footprint. The
# TurtleBot3 Waffle body ends about 0.08 m ahead of base_footprint and is
# about 0.155 m half-wide, so forward motion stops when a laser point lies in
# a corridor 0.20 m deep in front of the body and 0.19 m half-wide.
GUARD_FRONT_M = .08
GUARD_CLEARANCE_M = .20
GUARD_HALF_WIDTH_M = .19
GUARD_SCAN_MAX_AGE_S = .5


def forward_obstacle(scan, offset=(0., 0., 0.), *, front=GUARD_FRONT_M,
                     clearance=GUARD_CLEARANCE_M, half_width=GUARD_HALF_WIDTH_M):
    """Gap in metres from the robot's front to the nearest point ahead, or None.

    `offset` is the laser frame's (x, y, yaw) in base_footprint. Non-finite
    ranges and ranges outside [range_min, range_max] are ignored.
    """
    ox, oy, oyaw = offset
    cos_o, sin_o = math.cos(oyaw), math.sin(oyaw)
    nearest = None
    for index, value in enumerate(scan.ranges):
        if not math.isfinite(value) or value < scan.range_min or value > scan.range_max:
            continue
        angle = scan.angle_min + index * scan.angle_increment
        lx, ly = value * math.cos(angle), value * math.sin(angle)
        x = ox + cos_o * lx - sin_o * ly
        y = oy + sin_o * lx + cos_o * ly
        if 0. < x <= front + clearance and abs(y) <= half_width:
            gap = max(0., x - front)
            if nearest is None or gap < nearest:
                nearest = gap
    return nearest


class ForwardGuard:
    """Blocks forward drive-pad motion near an obstacle; turning and reversing stay allowed.

    `latest` returns (scan, received_monotonic, offset). Without a fresh scan
    or a laser-to-robot transform, forward motion is blocked too (fail safe).
    """
    def __init__(self, latest, *, clock=time.monotonic, max_age=GUARD_SCAN_MAX_AGE_S):
        self.latest = latest
        self.clock = clock
        self.max_age = max_age

    def check(self, linear_x):
        """Return (allowed_linear_x, reason); reason is None when nothing was blocked."""
        if linear_x <= 0:
            return linear_x, None
        try:
            scan, received, offset = self.latest()
        except Exception as exc:
            return 0., ('Drive-pad bumper cannot read the laser position (' + str(exc)
                        + '); forward motion is blocked. Turning and reversing still work.')
        if scan is None or received is None or self.clock() - received > self.max_age:
            return 0., ('Drive-pad bumper has no fresh laser scan; forward motion is blocked. '
                        'Turning and reversing still work.')
        gap = forward_obstacle(scan, offset)
        if gap is not None:
            return 0., (f'Drive-pad bumper: obstacle {gap:.2f} m in front of the robot; '
                        'forward motion is blocked. Turn (A/D) or reverse (S).')
        return linear_x, None


def goal_geometry(x, y, yaw, *, origin_x, origin_y, origin_yaw, width, height, resolution):
    """Validate finite map coordinates and derive a unit yaw quaternion."""
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (x, y, yaw)):
        raise ValueError('Goal x, y and yaw must be finite numbers.')
    dx, dy = x-origin_x, y-origin_y
    gx = math.cos(origin_yaw)*dx + math.sin(origin_yaw)*dy
    gy = -math.sin(origin_yaw)*dx + math.cos(origin_yaw)*dy
    if not (0 <= gx < width*resolution and 0 <= gy < height*resolution):
        raise ValueError('The navigation goal lies outside the loaded map.')
    return math.sin(yaw/2), math.cos(yaw/2)


def validate_saved_map(result, target):
    """A successful response alone is insufficient map-save evidence."""
    target = Path(target)
    yaml_path, pgm_path = target.with_suffix('.yaml'), target.with_suffix('.pgm')
    if result != 0:
        raise RuntimeError(f'SLAM Toolbox SaveMap returned result={result}.')
    if not yaml_path.is_file() or not pgm_path.is_file():
        raise RuntimeError('SLAM Toolbox did not create both saved YAML and PGM files.')
    return {'result': 0, 'yaml': str(yaml_path), 'pgm': str(pgm_path)}


def quiesce_mapping_lifecycle(query, change, records=None):
    """Deactivate SLAM threads before cleanup while its ROS context is valid."""
    records = [] if records is None else records
    report = {'node': 'slam_toolbox', 'success': False, 'records': records}

    def state():
        record = {'operation': 'get_state', 'service': '/slam_toolbox/get_state',
                  'success': False}
        records.append(record)
        try:
            state_id, label = query()
            record.update(state_id=int(state_id), state_label=str(label), success=True)
            return int(state_id)
        except Exception as exc:
            record['error'] = str(exc)
            raise

    def transition(transition_id, label, expected):
        record = {'operation': 'change_state', 'service': '/slam_toolbox/change_state',
                  'transition_id': transition_id, 'transition_label': label,
                  'success': False}
        records.append(record)
        try:
            record['success'] = bool(change(transition_id))
            if not record['success']:
                raise RuntimeError('SLAM Toolbox rejected lifecycle ' + label + '.')
            actual = state()
            if actual != expected:
                raise RuntimeError(f'SLAM Toolbox lifecycle {label} expected state '
                                   f'{expected}, received {actual}.')
            return actual
        except Exception as exc:
            record['error'] = str(exc)
            raise

    try:
        current = state()
        if current == 3:
            current = transition(4, 'deactivate', 2)
        if current == 2:
            current = transition(2, 'cleanup', 1)
        if current != 1:
            raise RuntimeError(f'SLAM Toolbox cannot be quiesced from lifecycle state {current}.')
        report['success'] = True
        report['final_state_id'] = current
    except Exception as exc:
        report['error'] = str(exc)
    return report


class Deadman:
    """Wall-clock timeout remains effective if simulation time stops.

    An optional ForwardGuard zeroes forward speed near an obstacle, on each
    request and again on each tick while driving forward. `notify` receives
    one message per blocked episode.
    """
    def __init__(self, publish, *, clock=time.monotonic, timeout=.300, guard=None, notify=None):
        self.publish = publish
        self.clock = clock
        self.timeout = timeout
        self.guard = guard
        self.notify = notify
        self.last_request = None
        self.moving = False
        self.published = (0., 0.)
        self.blocked = False
        self.lock = threading.Lock()

    def _guarded(self, linear_x):
        if self.guard is None or linear_x <= 0:
            self.blocked = False
            return linear_x
        allowed, reason = self.guard.check(linear_x)
        if reason is None:
            self.blocked = False
        elif not self.blocked:
            self.blocked = True
            if self.notify:
                try:
                    self.notify(reason)
                except Exception:
                    pass
        return allowed

    def _send(self, linear_x, angular_z):
        self.published = (linear_x, angular_z)
        self.moving = bool(linear_x or angular_z)
        self.publish(linear_x, angular_z)

    def request(self, linear_x, angular_z):
        linear_x, angular_z = clamp_velocity(linear_x, angular_z)
        with self.lock:
            self.last_request = self.clock()
            self._send(self._guarded(linear_x), angular_z)

    def tick(self):
        with self.lock:
            if (self.moving and self.last_request is not None
                    and self.clock() - self.last_request >= self.timeout):
                self._send(0., 0.)
                return True
            linear_x, angular_z = self.published
            if self.moving and linear_x > 0 and self.guard is not None:
                allowed = self._guarded(linear_x)
                if allowed != linear_x:
                    self._send(allowed, angular_z)
        return False

    def stop(self):
        with self.lock:
            self.blocked = False
            self._send(0., 0.)


class RosSession:
    """One node/context in the supervisor's selected domain, never in HTTP."""
    def __init__(self, kind, stop_requested, controller='mppi', notify=None, record_truth=False, explore=False):
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.parameter import Parameter
        from rclpy.signals import SignalHandlerOptions
        from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy, qos_profile_sensor_data
        from geometry_msgs.msg import Twist
        from nav2_msgs.action import NavigateToPose
        from nav_msgs.msg import OccupancyGrid
        from sensor_msgs.msg import LaserScan
        from slam_toolbox.srv import SaveMap
        from tf2_ros import Buffer, TransformListener

        self.rclpy = rclpy
        self.kind = kind
        self.controller = controller
        self.stop_requested = stop_requested
        self.context = Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node('amr_console_adapter', context=self.context,
            parameter_overrides=[Parameter('use_sim_time', Parameter.Type.BOOL, True)])
        self.map = self.scan = None
        self.truth = None
        self.scan_received = None
        self.scan_offset = None
        self.buffer = Buffer(node=self.node)
        self.tf_listener = TransformListener(self.buffer, self.node)
        self.node.create_subscription(OccupancyGrid, '/map', self._map,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                       reliability=ReliabilityPolicy.RELIABLE))
        self.node.create_subscription(LaserScan, '/scan', self._scan, qos_profile_sensor_data)
        if record_truth:
            from tf2_msgs.msg import TFMessage
            self.node.create_subscription(TFMessage, '/amr/ground_truth', self._truth, 20)
        self.Twist = Twist
        self.publisher = self.node.create_publisher(Twist, '/cmd_vel', 10) if kind == 'mapping' else None
        self.guard = ForwardGuard(self._guard_input) if self.publisher else None
        self.deadman = (Deadman(self._publish, guard=self.guard, notify=notify)
                        if self.publisher else None)
        self.goal_type = NavigateToPose
        self.action = ActionClient(self.node, NavigateToPose, '/navigate_to_pose') if kind == 'navigation' else None
        self.save_service = self.node.create_client(SaveMap, '/slam_toolbox/save_map') if kind == 'mapping' else None
        self.save_type = SaveMap
        # Autonomous exploration sessions: the explorer node stops its own Nav2 goal when STOP asks it to.
        self.explore_stop = None
        if explore and kind == 'mapping':
            from std_srvs.srv import Trigger
            self.trigger_type = Trigger
            self.explore_stop = self.node.create_client(Trigger, '/amr_explorer/stop')
        self.active_goal = None
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.spin_thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.spin_thread.start()

    def _map(self, message):
        self.map = message

    def _truth(self, message):
        from typed_commands import truth_sample
        self.truth = truth_sample(message, time.monotonic())

    def truth_pose(self, after=None):
        def fresh():
            sample = self.truth
            return (sample is not None and time.monotonic() - sample['received_monotonic'] < 1.
                    and (after is None or sample['received_monotonic'] > after))
        self.wait(fresh, 5, 'Fresh Gazebo ground truth')
        return dict(self.truth)

    def command_grid(self):
        import numpy as np
        from amr_physical_ai.command_pipeline import Grid
        message = self.map
        if message is None or message.header.frame_id != 'map':
            raise ValueError('The current occupancy map is unavailable or not in the map frame.')
        info = message.info
        q = info.origin.orientation
        yaw = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2*(q.y*q.y + q.z*q.z))
        return Grid(np.asarray(message.data).reshape(info.height, info.width), info.resolution,
                    (info.origin.position.x, info.origin.position.y, yaw))

    def _scan(self, message):
        self.scan = message
        self.scan_received = time.monotonic()

    def _guard_input(self):
        """Latest scan, its wall-clock receipt time, and the laser pose in base_footprint."""
        scan, received = self.scan, self.scan_received
        if scan is None:
            return None, None, None
        frame = scan.header.frame_id
        if self.scan_offset is None or self.scan_offset[0] != frame:
            transform = self.buffer.lookup_transform('base_footprint', frame, self.rclpy.time.Time())
            t, q = transform.transform.translation, transform.transform.rotation
            yaw = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2*(q.y*q.y + q.z*q.z))
            self.scan_offset = (frame, (t.x, t.y, yaw))
        return scan, received, self.scan_offset[1]

    def _publish(self, linear_x, angular_z):
        message = self.Twist()
        message.linear.x, message.angular.z = float(linear_x), float(angular_z)
        self.publisher.publish(message)

    def wait(self, condition, timeout, label, cancel=None):
        end = time.monotonic() + timeout
        while not condition():
            if self.stop_requested.is_set():
                raise InterruptedError('Session STOP interrupted ' + label)
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Office task cancel interrupted ' + label)
            if time.monotonic() >= end:
                raise TimeoutError(label + ' timed out.')
            time.sleep(.02)

    def result(self, future, timeout, label, cancel=None):
        self.wait(future.done, timeout, label, cancel)
        if future.exception():
            raise future.exception()
        return future.result()

    def readiness(self, timeout=120):
        self.wait(lambda: self.map is not None and self.scan is not None
                  and self.buffer.can_transform('map', 'base_footprint', self.rclpy.time.Time())
                  and self.buffer.can_transform('map', self.scan.header.frame_id, self.rclpy.time.Time()),
                  timeout, 'Map, scan and map-to-robot TF readiness')
        if self.kind == 'mapping':
            self.wait(lambda: self.save_service.service_is_ready(), 30, 'SLAM Toolbox SaveMap service')
        else:
            from lifecycle_msgs.srv import GetState
            from rcl_interfaces.srv import GetParameters
            for name in ('map_server', 'amcl', 'planner_server', 'controller_server', 'bt_navigator'):
                client = self.node.create_client(GetState, f'/{name}/get_state')
                try:
                    self.wait(client.service_is_ready, 30, name + ' lifecycle service')
                    def active():
                        state = self.result(client.call_async(GetState.Request()), 10, name + ' lifecycle query')
                        return state.current_state.id == 3
                    self.wait(active, 30, name + ' lifecycle activation')
                finally:
                    self.node.destroy_client(client)
            client = self.node.create_client(GetParameters, '/controller_server/get_parameters')
            try:
                self.wait(client.service_is_ready, 30, 'Controller parameter service')
                values = self.result(client.call_async(GetParameters.Request(names=['FollowPath.plugin'])),
                                     10, 'Controller plugin query').values
                expected = {'mppi': 'nav2_mppi_controller::MPPIController',
                            'dwb': 'dwb_core::DWBLocalPlanner',
                            'rpp': 'nav2_regulated_pure_pursuit_controller::RegulatedPurePursuitController'}[self.controller]
                if not values or values[0].string_value != expected:
                    raise RuntimeError('The navigation session loaded the wrong controller plugin.')
            finally:
                self.node.destroy_client(client)
            self.wait(self.action.server_is_ready, 30, 'NavigateToPose action')
        info = self.map.info
        return {'resolution': info.resolution, 'width': info.width, 'height': info.height,
                'origin_x': info.origin.position.x, 'origin_y': info.origin.position.y}

    def save_map(self, target):
        target = Path(target)
        yaml_path, pgm_path = target.with_suffix('.yaml'), target.with_suffix('.pgm')
        if yaml_path.exists() or pgm_path.exists():
            raise FileExistsError('A map with this name already exists in this run.')
        target.parent.mkdir(parents=True, exist_ok=True)
        request = self.save_type.Request()
        request.name.data = str(target)
        self.wait(self.save_service.service_is_ready, 10, 'SLAM Toolbox SaveMap service')
        response = self.result(self.save_service.call_async(request), 30, 'Saving the map')
        if response.result != 0:
            raise RuntimeError(f'SLAM Toolbox SaveMap returned result={response.result}.')
        self.wait(lambda: yaml_path.is_file() and pgm_path.is_file(), 5, 'Saved YAML/PGM files')
        return validate_saved_map(response.result, target)

    def navigate(self, params, *, before_send=None, cancel=None, timeout=90):
        # cancel: an office-task Cancel; it stops this goal but keeps the session running.
        if self.active_goal is not None:
            raise RuntimeError('A navigation goal is already running.')
        x, y, yaw = (params[name] for name in ('x', 'y', 'yaw'))
        info = self.map.info
        q = info.origin.orientation
        angle = math.atan2(2 * (q.w*q.z + q.x*q.y), 1 - 2*(q.y*q.y + q.z*q.z))
        z, w = goal_geometry(x, y, yaw, origin_x=info.origin.position.x,
            origin_y=info.origin.position.y, origin_yaw=angle,
            width=info.width, height=info.height, resolution=info.resolution)
        goal = self.goal_type.Goal()
        goal.pose.header.frame_id = 'map'
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x, goal.pose.pose.position.y = float(x), float(y)
        goal.pose.pose.orientation.z, goal.pose.pose.orientation.w = z, w
        self.wait(self.action.server_is_ready, 10, 'NavigateToPose action')
        if before_send is not None:
            before_send()
        accepted = self.action.send_goal_async(goal)
        def cancel_after_stop(future):
            # STOP may arrive before goal acceptance. Cancel an eventual
            # accepted handle even after the waiting operation has returned.
            stopping = self.stop_requested.is_set() or (cancel is not None and cancel.is_set())
            if stopping and not future.exception():
                eventual = future.result()
                if eventual and eventual.accepted:
                    eventual.cancel_goal_async()
        accepted.add_done_callback(cancel_after_stop)
        handle = (self.result(accepted, 20, 'Goal acceptance') if cancel is None
                  else self.result(accepted, 20, 'Goal acceptance', cancel))
        if not handle.accepted:
            raise RuntimeError('NavigateToPose rejected the goal.')
        self.active_goal = handle
        result_future = handle.get_result_async()
        def release_completed_goal(_future):
            if self.active_goal is handle:
                self.active_goal = None
        result_future.add_done_callback(release_completed_goal)
        try:
            result = (self.result(result_future, timeout, 'Navigation goal') if cancel is None
                      else self.result(result_future, timeout, 'Navigation goal', cancel))
            return {'status': result.status, 'navigation_outcome':
                    {4: 'SUCCEEDED', 5: 'CANCELED', 6: 'ABORTED'}.get(result.status, str(result.status))}
        finally:
            if not result_future.done():
                try:
                    handle.cancel_goal_async()
                except Exception as exc:
                    self.last_cancel_error = str(exc)
                # Keep ownership until the action result confirms termination.
                # A timeout/cancellation-request failure must not permit a
                # second goal while this one can still drive the robot.

    def halt(self):
        errors = []
        if self.deadman:
            try:
                self.deadman.stop()
            except Exception as exc:
                errors.append('Zero velocity publication failed: ' + str(exc))
        if self.active_goal:
            try:
                self.active_goal.cancel_goal_async()
            except Exception as exc:
                errors.append('Goal cancellation request failed: ' + str(exc))
        return errors

    def _shutdown_wait(self, condition, deadline, label):
        # STOP is already set here. Only these shutdown waits ignore it, and
        # every service/future shares one wall-clock deadline.
        while not condition():
            if not self.context.ok():
                raise RuntimeError('ROS context became invalid during ' + label)
            if time.monotonic() >= deadline:
                raise TimeoutError(label + ' timed out.')
            time.sleep(.02)

    def _shutdown_result(self, future, deadline, label):
        self._shutdown_wait(future.done, deadline, label)
        if future.exception():
            raise future.exception()
        return future.result()

    def stop_explorer(self, timeout=6.):
        """Ask the explorer to cancel its goal and record the run's end, before SLAM is quiesced.

        An explorer that is not running (never started, or already exited) is reported as not called;
        its process exit is audited from the launch log like every other process."""
        report = {'service': '/amr_explorer/stop', 'called': False, 'success': False}
        if self.explore_stop is None:
            report['error'] = 'This session has no explorer.'
            return report
        deadline = time.monotonic() + timeout
        try:
            self._shutdown_wait(self.explore_stop.service_is_ready, min(deadline, time.monotonic() + 2.),
                                'Explorer stop service availability')
        except Exception as exc:
            report['error'] = str(exc)
            return report
        report['called'] = True
        try:
            response = self._shutdown_result(self.explore_stop.call_async(self.trigger_type.Request()),
                                             deadline, 'Explorer stop')
            report.update(success=bool(response.success), message=response.message)
        except Exception as exc:
            report['error'] = str(exc)
        return report

    def quiesce_mapping(self, timeout=15, *, service_types=None):
        """Finish SLAM publication threads before Gazebo pause or SIGINT."""
        report = {'node': 'slam_toolbox', 'success': False, 'records': []}
        clients = []
        if self.kind != 'mapping':
            report['error'] = 'SLAM lifecycle shutdown is only for a mapping session.'
            return report
        deadline = time.monotonic() + timeout
        try:
            if service_types is None:
                from lifecycle_msgs.srv import GetState, ChangeState
            else:
                GetState, ChangeState = service_types
            get_client = self.node.create_client(GetState, '/slam_toolbox/get_state')
            clients.append(get_client)
            change_client = self.node.create_client(ChangeState, '/slam_toolbox/change_state')
            clients.append(change_client)
            for name, client in (('/slam_toolbox/get_state', get_client),
                                 ('/slam_toolbox/change_state', change_client)):
                record = {'operation': 'service_ready', 'service': name, 'success': False}
                report['records'].append(record)
                try:
                    self._shutdown_wait(client.service_is_ready, deadline, name + ' availability')
                    record['success'] = True
                except Exception as exc:
                    record['error'] = str(exc)
                    raise

            def query():
                response = self._shutdown_result(get_client.call_async(GetState.Request()),
                                                deadline, 'SLAM lifecycle state query')
                return response.current_state.id, response.current_state.label

            def change(transition_id):
                request = ChangeState.Request()
                request.transition.id = transition_id
                response = self._shutdown_result(change_client.call_async(request),
                                                deadline, 'SLAM lifecycle transition')
                return response.success

            report = quiesce_mapping_lifecycle(query, change, report['records'])
        except Exception as exc:
            report['error'] = str(exc)
        finally:
            for client in clients:
                try:
                    self.node.destroy_client(client)
                except Exception as exc:
                    report['success'] = False
                    report['error'] = 'Lifecycle client destruction failed: ' + str(exc)
        report['timeout_s'] = timeout
        return report

    def close(self):
        self.halt()
        self.executor.shutdown(timeout_sec=2)
        self.spin_thread.join(timeout=2)
        self.node.destroy_node()
        self.context.try_shutdown()
