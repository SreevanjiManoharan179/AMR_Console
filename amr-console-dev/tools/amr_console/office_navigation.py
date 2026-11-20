"""Office tasks: one drive to an approved stop inside the navigation session worker.

The pose was taken from the approved files by the console. Here it is checked again
against the live occupancy map (same saved map, free cell, endpoint clearance) right
before the goal is sent, and the drive is recorded with Gazebo ground truth.
"""
from datetime import datetime
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

LEG_TIMEOUT_S = 240.0


def _finite(*values):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def navigate_leg(root, session, params, cancel=None, *, clock=time.monotonic):
    """Drive to params['x'], params['y'], params['yaw']; return the leg record.

    params: x, y, yaw, place, label, job_id, nav_id, purpose, map_yaml, minimum_clearance_m.
    The record's nav2.status is SUCCEEDED, CANCELED, ABORTED, CLARIFICATION, ERROR or NOT_SENT.
    """
    from amr_physical_ai.command_pipeline import ClarificationRequired, Grid

    started = clock()
    record = {'type': 'office_leg', 'timestamp': datetime.now(ZoneInfo('Asia/Kolkata')).isoformat(),
              'robot': 'TurtleBot3 Waffle simulation',
              'place': params.get('place'), 'label': params.get('label'), 'purpose': params.get('purpose'),
              'job_id': params.get('job_id'), 'nav_id': params.get('nav_id'),
              'goal': {k: params.get(k) for k in ('x', 'y', 'yaw')},
              'validation': {'valid': False}, 'nav2': {'status': 'NOT_SENT', 'goal_sent': False},
              'truth_before': None, 'truth_after': None, 'position_change_m': None,
              'true_final_error_m': None, 'error': None}
    try:
        x, y, yaw = (params.get(k) for k in ('x', 'y', 'yaw'))
        clearance = params.get('minimum_clearance_m')
        if not _finite(x, y, yaw, clearance) or not .22 <= clearance <= 2:
            raise ClarificationRequired('The office stop pose is invalid.')
        if session.stop_requested.is_set():
            raise InterruptedError('Session STOP requested; no goal sent.')
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Office task cancelled; no goal sent.')
        if session.active_goal is not None:
            raise RuntimeError('A previous navigation goal is still active; wait or press STOP.')
        saved = Grid.from_map(Path(root) / params['map_yaml'])

        def check():
            grid = session.command_grid()
            if not saved.same_map(grid):
                raise ClarificationRequired('The live map differs from the office map. Check the launch.')
            return grid.validate_point(x, y, clearance)

        record['validation'] = {'valid': True, **check()}
        record['truth_before'] = session.truth_pose()

        def before_send():
            check()
            session.truth_pose()  # Missing or stale ground truth blocks the goal too.
            if session.stop_requested.is_set():
                raise InterruptedError('Session STOP requested; no goal sent.')
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Office task cancelled; no goal sent.')
            record['nav2']['goal_sent'] = True

        nav_started = clock()
        outcome = session.navigate({'x': x, 'y': y, 'yaw': yaw}, before_send=before_send,
                                   cancel=cancel, timeout=LEG_TIMEOUT_S)
        record['nav2'].update(status=outcome['navigation_outcome'], status_code=outcome['status'],
                              navigation_wall_s=round(clock() - nav_started, 4))
    except ClarificationRequired as exc:
        record['validation'] = {'valid': False, 'question': str(exc)}
        record['nav2']['status'] = 'CLARIFICATION'
        record['error'] = str(exc)
    except InterruptedError as exc:
        record['nav2']['status'] = 'CANCELED' if record['nav2']['goal_sent'] else 'NOT_SENT'
        record['error'] = str(exc)
    except Exception as exc:
        record['nav2']['status'] = 'ERROR'
        record['error'] = str(exc)
        if record['nav2']['goal_sent']:
            # A goal that may still be accepted is owned by the audited worker cleanup.
            session.stop_requested.set()
    try:
        if record['truth_before'] is not None:
            after = session.truth_pose(after=clock() + .5)
            before = record['truth_before']
            record['truth_after'] = after
            record['position_change_m'] = math.hypot(after['x'] - before['x'], after['y'] - before['y'])
            if record['nav2']['goal_sent']:
                record['true_final_error_m'] = math.hypot(after['x'] - x, after['y'] - y)
    except Exception as exc:
        record['evidence_error'] = str(exc)
    record['total_wall_s'] = round(clock() - started, 4)
    return record
