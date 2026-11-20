"""Offline drive-pad bumper tests; no ROS node or OS process is started."""
import math
from types import SimpleNamespace
import unittest

import test_support
from ros_adapter import Deadman, ForwardGuard, forward_obstacle

# TurtleBot3 Waffle: base_scan sits 0.064 m behind base_footprint, zero yaw.
WAFFLE_LASER = (-.064, 0., 0.)


def setUpModule():
    test_support.assert_guard_active()


def scan(points, *, count=360, angle_min=0., range_min=.12, range_max=3.5):
    """LDS-style scan (0 to 2*pi); `points` maps beam index to range, others inf."""
    increment = 2 * math.pi / count
    ranges = [math.inf] * count
    for index, value in points.items():
        ranges[index] = value
    return SimpleNamespace(ranges=ranges, angle_min=angle_min, angle_increment=increment,
                           range_min=range_min, range_max=range_max)


class Clock:
    def __init__(self):
        self.value = 100.
    def __call__(self):
        return self.value


class ForwardObstacleTests(unittest.TestCase):
    def test_wall_straight_ahead_reports_gap_from_robot_front(self):
        gap = forward_obstacle(scan({0: .25}), WAFFLE_LASER)
        self.assertAlmostEqual(gap, .25 - .064 - .08, places=6)

    def test_distant_wall_side_points_and_rear_points_are_ignored(self):
        self.assertIsNone(forward_obstacle(scan({0: 1.0}), WAFFLE_LASER))
        self.assertIsNone(forward_obstacle(scan({90: .30}), WAFFLE_LASER))
        self.assertIsNone(forward_obstacle(scan({180: .15}), WAFFLE_LASER))

    def test_wraparound_beam_just_right_of_forward_is_detected(self):
        self.assertIsNotNone(forward_obstacle(scan({355: .25}), WAFFLE_LASER))

    def test_invalid_ranges_are_ignored(self):
        values = scan({0: math.nan, 1: .05, 2: 9.0, 359: -math.inf})
        self.assertIsNone(forward_obstacle(values, WAFFLE_LASER))

    def test_laser_yaw_is_applied(self):
        # A laser mounted backwards sees the robot's front at its own angle pi.
        self.assertIsNotNone(forward_obstacle(scan({180: .25}), (0., 0., math.pi)))
        self.assertIsNone(forward_obstacle(scan({0: .25}), (0., 0., math.pi)))


class ForwardGuardTests(unittest.TestCase):
    def make(self, values, *, age=0., offset=WAFFLE_LASER):
        clock = Clock()
        state = {'scan': values, 'received': clock() - age, 'offset': offset}
        guard = ForwardGuard(lambda: (state['scan'], state['received'], state['offset']), clock=clock)
        return guard, state, clock

    def test_forward_blocked_near_obstacle_but_turn_and_reverse_allowed(self):
        guard, _, _ = self.make(scan({0: .25}))
        allowed, reason = guard.check(.15)
        self.assertEqual(allowed, 0.)
        self.assertIn('forward motion is blocked', reason)
        self.assertEqual(guard.check(0.), (0., None))
        self.assertEqual(guard.check(-.15), (-.15, None))

    def test_clear_path_allows_forward(self):
        guard, _, _ = self.make(scan({0: 1.5}))
        self.assertEqual(guard.check(.26), (.26, None))

    def test_stale_or_missing_scan_fails_safe(self):
        guard, state, _ = self.make(scan({0: 1.5}), age=.6)
        allowed, reason = guard.check(.15)
        self.assertEqual(allowed, 0.)
        self.assertIn('no fresh laser scan', reason)
        state['scan'] = None
        self.assertEqual(guard.check(.15)[0], 0.)

    def test_unreadable_laser_transform_fails_safe(self):
        def broken():
            raise RuntimeError('frame does not exist')
        allowed, reason = ForwardGuard(broken, clock=Clock()).check(.15)
        self.assertEqual(allowed, 0.)
        self.assertIn('frame does not exist', reason)


class GuardedDeadmanTests(unittest.TestCase):
    def make(self, values):
        clock = Clock()
        state = {'scan': values}
        guard = ForwardGuard(lambda: (state['scan'], clock(), WAFFLE_LASER), clock=clock)
        published, notices = [], []
        deadman = Deadman(lambda v, w: published.append((v, w)), clock=clock,
                          guard=guard, notify=notices.append)
        return deadman, state, clock, published, notices

    def test_held_forward_into_wall_publishes_zero_and_notifies_once_per_hold(self):
        deadman, _, clock, published, notices = self.make(scan({0: .25}))
        for _ in range(5):
            deadman.request(.15, 0.)
            clock.value += .1
        self.assertEqual(published, [(0., 0.)] * 5)
        self.assertEqual(len(notices), 1)
        deadman.request(0., 0.)          # release
        deadman.request(.15, 0.)         # press forward again
        self.assertEqual(len(notices), 2)

    def test_turning_beside_wall_is_published_unchanged(self):
        deadman, _, _, published, notices = self.make(scan({0: .25}))
        deadman.request(0., .8)
        deadman.request(-.1, 0.)
        self.assertEqual(published, [(0., .8), (-.1, 0.)])
        self.assertEqual(notices, [])

    def test_tick_stops_forward_motion_when_obstacle_appears(self):
        deadman, state, clock, published, notices = self.make(scan({0: 1.5}))
        deadman.request(.15, 0.)
        self.assertEqual(published[-1], (.15, 0.))
        state['scan'] = scan({0: .25})
        clock.value += .05
        self.assertFalse(deadman.tick())
        self.assertEqual(published[-1], (0., 0.))
        self.assertEqual(len(notices), 1)
        self.assertFalse(deadman.moving)

    def test_deadman_timeout_still_zeroes_after_clear_forward_drive(self):
        deadman, _, clock, published, _ = self.make(scan({0: 1.5}))
        deadman.request(.15, 0.)
        clock.value += .31
        self.assertTrue(deadman.tick())
        self.assertEqual(published[-1], (0., 0.))

    def test_failing_notify_never_blocks_zero_publication(self):
        clock = Clock()
        guard = ForwardGuard(lambda: (scan({0: .25}), clock(), WAFFLE_LASER), clock=clock)
        published = []
        def notify(_text):
            raise BrokenPipeError('protocol pipe closed')
        deadman = Deadman(lambda v, w: published.append((v, w)), clock=clock,
                          guard=guard, notify=notify)
        deadman.request(.15, 0.)
        self.assertEqual(published, [(0., 0.)])

    def test_unguarded_deadman_behaviour_is_unchanged(self):
        clock = Clock()
        published = []
        deadman = Deadman(lambda v, w: published.append((v, w)), clock=clock)
        deadman.request(.5, 3.)
        self.assertEqual(published, [(.26, 1.82)])
        clock.value += .31
        self.assertTrue(deadman.tick())
        self.assertEqual(published[-1], (0., 0.))
        deadman.stop()
        self.assertEqual(published[-1], (0., 0.))


if __name__ == '__main__':
    unittest.main()
