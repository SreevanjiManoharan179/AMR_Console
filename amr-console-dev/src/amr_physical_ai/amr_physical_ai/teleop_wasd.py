#!/usr/bin/env python3
"""Conservative keyboard control for the simulation's Twist bridge."""

import select
import sys
import termios
import time
import tty

from geometry_msgs.msg import Twist
import rclpy
from rclpy.node import Node

HELP = """
WASD simulation teleop — focus this terminal; hold a movement key.
w/s: forward/backward    a/d: turn left/right
b or Space: stop         q or Ctrl+C: stop and quit
+/-: adjust speed (initial 0.15 m/s, 0.6 rad/s)
Stops after 0.3 seconds without a movement key; keyboard repeat may pause initially.
"""


class WasdTeleop(Node):
    def __init__(self):
        super().__init__('wasd_teleop')
        self.pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.lin = 0.15
        self.ang = 0.6

    def send(self, x, z):
        msg = Twist()
        msg.linear.x = x
        msg.angular.z = z
        self.pub.publish(msg)


def main(args=None):
    if not sys.stdin.isatty():
        raise SystemExit('Open teleop in an interactive terminal.')
    settings = termios.tcgetattr(sys.stdin)
    rclpy.init(args=args)
    node = WasdTeleop()
    print(HELP, flush=True)
    command = (0.0, 0.0)
    last_motion = 0.0
    try:
        tty.setraw(sys.stdin.fileno())
        while rclpy.ok():
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            key = sys.stdin.read(1).lower() if ready else ''
            if key in ('q', '\x03'):
                break
            motions = {'w': (node.lin, 0.0), 's': (-node.lin, 0.0),
                       'a': (0.0, node.ang), 'd': (0.0, -node.ang)}
            if key in motions:
                command = motions[key]
                last_motion = time.monotonic()
            elif key in ('b', ' '):
                command = (0.0, 0.0)
            elif key in ('+', '-'):
                delta = 0.05 if key == '+' else -0.05
                node.lin = min(0.30, max(0.05, node.lin + delta))
                node.ang = min(1.0, max(0.2, node.ang + 2 * delta))
                command = (0.0, 0.0)
            if time.monotonic() - last_motion > 0.3:
                command = (0.0, 0.0)
            node.send(*command)
    finally:
        # Stop before destroying the publisher and restore the terminal even on error.
        if rclpy.ok():
            node.send(0.0, 0.0)
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
