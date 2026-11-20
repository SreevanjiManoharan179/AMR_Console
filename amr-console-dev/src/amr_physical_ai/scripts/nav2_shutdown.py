#!/usr/bin/env python3
"""Finalize owned Nav2 lifecycle nodes before terminating the launch session."""
import argparse
import json
import time

import rclpy
from nav2_msgs.srv import ManageLifecycleNodes
from rclpy.signals import SignalHandlerOptions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=12)
    args = parser.parse_args()
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('amr_orderly_shutdown')
    records = []
    try:
        for manager in ('lifecycle_manager_navigation', 'lifecycle_manager_localization'):
            client = node.create_client(ManageLifecycleNodes, f'/{manager}/manage_nodes')
            record = {'manager': manager, 'success': False}
            try:
                if not client.wait_for_service(timeout_sec=3):
                    record['error'] = 'service unavailable'
                else:
                    request = ManageLifecycleNodes.Request()
                    request.command = request.SHUTDOWN
                    future = client.call_async(request)
                    end = time.monotonic() + args.timeout
                    while not future.done() and time.monotonic() < end:
                        rclpy.spin_once(node, timeout_sec=.1)
                    if future.done():
                        record['success'] = bool(future.result().success)
                    else:
                        record['error'] = 'shutdown response timeout'
            except Exception as exc:
                record['error'] = str(exc)
            finally:
                node.destroy_client(client)
                records.append(record)
        # Drain final bond/costmap callbacks while DDS is still alive.
        end = time.monotonic() + .5
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=.05)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    print(json.dumps({'lifecycle_shutdown': records}), flush=True)
    return 0 if all(r['success'] for r in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
