"""Shutdown decisions and review records for process-isolated visual trials (no ROS imports).

Nav2 bond abort (``bond_abort``): a known, harmless Nav2 lifecycle "bond"
exception at shutdown. Waiving it requires evidence of a bond stack in the same
process after our shutdown marker. A generic publication error is only a
candidate, never a waiver. ``clean`` always means crash-free;
``continue_allowed`` additionally allows this narrowly defined, fully isolated
bond abort.

Bridge shutdown abort (``truth_bridge_abort``, accepted 12 Oct 2026 and widened
the same day to both bridges by the owner): a ros_gz_bridge ``parameter_bridge``
process (the main sensor/odometry bridge or the evaluation-only ground-truth
bridge) may abort with a glibc heap-corruption message during teardown. It is waived only when its single
death line is exit -6 after the shutdown marker, it printed a heap-corruption
diagnostic after the marker and nothing before it, and every other gate
passes. The waived process and its command are recorded. It is counted
separately and is never called crash-free.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path
import re
import shlex

from shutdown_audit import ANSI_ESCAPE, EXIT_CODE, FATAL_DIAGNOSTIC, audit_shutdown

SHUTDOWN_MARKER = 'AMR_CONSOLE_SHUTDOWN_BEGIN'
_PROCESS = re.compile(r'^\[(?:(?:INFO|WARN|ERROR|DEBUG)\]\s+\[)?([^\]]+)\]')
_PID = re.compile(r'process has died \[pid (\d+),')
_BOND = re.compile(r'bond::Bond::(?:publishStatus|bondStatusCB)')
_NAV2 = re.compile(
    r'^(?:planner_server|controller_server|map_server|amcl|smoother_server|'
    r'route_server|behavior_server|bt_navigator|velocity_smoother|collision_monitor|'
    r'opennav_docking|waypoint_follower|lifecycle_manager)-\d+$')
_PUBLISH = re.compile(r'failed to publish message:.*(?:failed to publish data|cannot publish data)')
_BRIDGE_PROCESS = re.compile(r'^parameter_bridge-\d+$')
_BRIDGE_DEATH = re.compile(
    r"^\[ERROR\] \[(parameter_bridge-\d+)\]: process has died \[pid (\d+), exit code -6, cmd '([^']*)'\]\.")
_HEAP_ABORT = re.compile(
    r'corrupted (?:double-linked list|size vs\.? prev_size)|double free or corruption|'
    r'(?:malloc|free|munmap_chunk)\(\):', re.IGNORECASE)


def _is_gz_bridge(command):
    """A ros_gz_bridge parameter_bridge launched by this trial; nothing else qualifies."""
    return 'ros_gz_bridge/parameter_bridge' in command


def _bridge_role(command):
    return 'ground-truth bridge' if '__node:=amr_truth_bridge' in command else 'main bridge'


def _truth_bridge_aborts(grouped, events):
    """Bridge shutdown abort: heap-corruption SIGABRT of a ros_gz_bridge process, after shutdown only."""
    proven = {}
    for process, process_lines in grouped.items():
        if not _BRIDGE_PROCESS.match(process):
            continue
        deaths = [item for item in process_lines if item.get('exit_code') is not None]
        if len(deaths) != 1:
            continue
        death = deaths[0]
        match = _BRIDGE_DEATH.match(death['line'])
        if not (match and death['after_shutdown_marker'] and death.get('exit_code') == -6
                and _is_gz_bridge(match.group(3))):
            continue
        process_events = [item for item in events if item['process'] == process]
        if any(not item['after_shutdown_marker'] for item in process_events):
            continue
        diagnostics = [item for item in process_events if item is not death]
        if not diagnostics or not all(_HEAP_ABORT.search(item['line']) for item in diagnostics):
            continue
        proven[process] = {'process': process, 'pid': death.get('pid'),
                           'bridge': _bridge_role(match.group(3)), 'command': match.group(3),
                           'evidence': 'ros_gz_bridge heap-corruption SIGABRT after the shutdown marker',
                           'lines': [item['line'] for item in process_events]}
    return proven


def _identity(process):
    return process.get('pid'), process.get('start_ticks')


def _inventory(post_inventory):
    """Normalize the explicit host inventory; absence is never an empty audit."""
    if isinstance(post_inventory, list):
        return post_inventory, [], True
    if not isinstance(post_inventory, dict):
        return [], ['No post-trial process inventory supplied'], False
    processes = post_inventory.get('processes', post_inventory.get('conflicts'))
    errors = list(post_inventory.get('scan_errors', post_inventory.get('errors', [])))
    complete = post_inventory.get('complete', processes is not None and not errors)
    if processes is None:
        errors.append('Post-trial inventory has no process list')
        processes = []
    return processes, errors, bool(complete and not errors)


def _matched_bond_evidence(evidence, process, pid, tracked, shutdown_at_epoch):
    """Only selected crash fields, tied to this PID incarnation and shutdown."""
    if shutdown_at_epoch is None:
        return False
    for item in evidence or []:
        if (item.get('process') != process or item.get('pid') != pid
                or item.get('signal') != 6
                or (pid, item.get('start_ticks')) not in tracked
                or item.get('start_ticks') is None
                or not item.get('evidence_file')):
            continue
        crash_time = item.get('crash_at_epoch')
        if not isinstance(crash_time, (int, float)):
            continue
        if not shutdown_at_epoch - 1 <= crash_time <= shutdown_at_epoch + 300:
            continue
        if any(_BOND.search(str(frame)) for frame in item.get('stack_functions', [])):
            return True
    return False



def selected_crash_bond_evidence(log_text, cleanup, shutdown_at_epoch,
                                 crash_dir='/var/crash'):
    """Read only selected fields from an already-created local Apport report.

    No authentication, report submission, core decoding or environment capture.
    A fresh report must match the observed dead PID and complete launch command
    (including its per-launch temporary parameter paths), then a bond symbol.
    PID start ticks are those retained by the owned-process monitor for that
    matched incarnation. Old reports and reports without a symbolic stack are
    insufficient, so the runner stops rather than guessing a bond exception.
    """
    if shutdown_at_epoch is None or SHUTDOWN_MARKER not in log_text:
        return []
    after = log_text.split(SHUTDOWN_MARKER, 1)[1]
    deaths = []
    for raw in after.splitlines():
        line = ANSI_ESCAPE.sub('', raw)
        match = re.search(r"^\[ERROR\] \[([^]]+)\]: process has died \[pid (\d+), exit code -6, cmd '(.*)'\]\.", line)
        if match and _NAV2.match(match.group(1)):
            deaths.append((match.group(1), int(match.group(2)), match.group(3)))
    if not deaths:
        return []
    tracked = {item['pid']: item for item in cleanup.get('tracked_processes', [])}
    fields_to_keep = {'Date', 'ExecutablePath', 'ProcCmdline', 'ProcStatus', 'Signal', 'Stacktrace'}
    result = []
    for path in sorted(Path(crash_dir).glob('*.crash')):
        fields = {}
        key = None
        consumed = 0
        try:
            with path.open(errors='replace') as stream:
                # Bound pathological reports; a missing symbolic stack fails closed.
                while consumed < 32 * 1024 * 1024:
                    line = stream.readline(65536)
                    if not line:
                        break
                    consumed += len(line)
                    if not line[0].isspace() and ':' in line:
                        key, value = line.rstrip('\n').split(':', 1)
                        if key in fields_to_keep:
                            fields[key] = value.strip()
                    elif key in fields_to_keep:
                        fields[key] += '\n' + line.rstrip('\n')
        except (PermissionError, FileNotFoundError, OSError):
            continue
        try:
            crash_at = datetime.strptime(fields['Date'], '%a %b %d %H:%M:%S %Y').timestamp()
            pid_match = re.search(r'^\s*Pid:\s*(\d+)\s*$', fields['ProcStatus'], re.MULTILINE)
            pid = int(pid_match.group(1)) if pid_match else None
            signal_number = int(fields['Signal'])
            command = shlex.split(fields['ProcCmdline'])
        except (KeyError, TypeError, ValueError):
            continue
        if signal_number != 6 or not shutdown_at_epoch - 1 <= crash_at <= shutdown_at_epoch + 300:
            continue
        frames = sorted(set(_BOND.findall(fields.get('Stacktrace', ''))))
        if not frames or pid not in tracked:
            continue
        for process, dead_pid, launch_command in deaths:
            try:
                command_matches = command == shlex.split(launch_command)
            except ValueError:
                command_matches = False
            if pid == dead_pid and command_matches and tracked[pid].get('start_ticks') is not None:
                result.append({'process': process, 'pid': pid,
                               'start_ticks': tracked[pid]['start_ticks'],
                               'signal': 6, 'crash_at_epoch': crash_at,
                               'stack_functions': frames, 'evidence_file': str(path),
                               'identity_evidence': 'PID, full launch command, fresh crash time, owned start ticks'})
    return result

def classify_shutdown(log_text, cleanup, post_inventory, window_result,
                      marker=SHUTDOWN_MARKER, bond_evidence=None,
                      shutdown_at_epoch=None):
    """Audit one fully stopped launch, preserving all failures and evidence.

    Write and flush ``marker + '\\n'`` to launch.log immediately before closing
    owned windows/signalling the stack. Pass a complete post-teardown host
    inventory (a list of relevant processes, or a dict with ``processes``,
    ``scan_errors``, ``complete``). ``cleanup`` is OwnedLaunch.shutdown output.

    Optional ``bond_evidence`` contains selected crash-report metadata only:
    process (launch label), pid, start_ticks, signal=6, crash_at_epoch,
    stack_functions, evidence_file. The caller must obtain start_ticks from the
    same process incarnation; a generic historical diagnosis is insufficient.
    No crash-report environment or core content belongs in these records.
    """
    cleanup = dict(cleanup or {})
    cleanup.setdefault('live_survivors', [])
    cleanup.setdefault('launch_returncode', None)
    audit = audit_shutdown(log_text, cleanup)
    processes, inventory_errors, complete = _inventory(post_inventory)
    tracked = {_identity(item) for item in cleanup.get('tracked_processes', [])}
    owned = list(cleanup['live_survivors'])
    owned_ids = {_identity(item) for item in owned}
    unowned = []
    for process in processes:
        identity = _identity(process)
        if identity in tracked:
            if identity not in owned_ids:
                owned.append(process)
                owned_ids.add(identity)
        else:
            unowned.append(process)
    isolation_pass = complete and not owned and not unowned

    lines = [ANSI_ESCAPE.sub('', line) for line in log_text.splitlines()]
    markers = [index for index, line in enumerate(lines) if line.strip() == marker]
    marker_index = markers[0] if len(markers) == 1 else None
    events = []
    grouped = {}
    for index, line in enumerate(lines):
        process_match = _PROCESS.match(line)
        process = process_match.group(1) if process_match else None
        code_match = EXIT_CODE.search(line) if 'process has died' in line else None
        code = int(code_match.group(1)) if code_match else None
        item = {'line_number': index + 1, 'process': process, 'line': line,
                'after_shutdown_marker': marker_index is not None and index > marker_index}
        if code is not None:
            item['exit_code'] = code
            pid_match = _PID.search(line)
            item['pid'] = int(pid_match.group(1)) if pid_match else None
        if FATAL_DIAGNOSTIC.search(line) or (code is not None and code != 0):
            events.append(item)
        if process:
            grouped.setdefault(process, []).append(item)

    proven = {}
    candidates = []
    for process, process_lines in grouped.items():
        if not _NAV2.match(process):
            continue
        post = [item for item in process_lines if item['after_shutdown_marker']]
        aborts = [item for item in post if item.get('exit_code') == -6]
        if not aborts:
            continue
        has_publish_error = any(_PUBLISH.search(item['line']) for item in post)
        has_rclcpp_terminate = any('terminate called' in item['line'] and
                                  'rclcpp::exceptions::' in item['line'] for item in post)
        if not (has_publish_error and has_rclcpp_terminate):
            continue
        has_stack = any(_BOND.search(item['line']) for item in post)
        has_metadata = any(_matched_bond_evidence(
            bond_evidence, process, item.get('pid'), tracked, shutdown_at_epoch)
            for item in aborts)
        if has_stack or has_metadata:
            proven[process] = {'process': process,
                               'pid': aborts[0].get('pid'),
                               'evidence': 'launch bond stack' if has_stack else 'matched selected crash metadata',
                               'lines': post}
        else:
            candidates.append({'process': process, 'pid': aborts[0].get('pid'),
                               'reason': 'Shutdown publication SIGABRT without matching bond stack evidence'})

    bridge_aborts = _truth_bridge_aborts(grouped, events)

    unwaived = []
    for event in events:
        # Even a proven bond process may have an unrelated heap or memory fault.
        allowed_event = (event['process'] in proven and event['after_shutdown_marker'] and
                         (event.get('exit_code') == -6 or
                          ('terminate called' in event['line'] and
                           'rclcpp::exceptions::' in event['line'])))
        # The truth-bridge set already requires every one of its events to be post-marker.
        allowed_event = allowed_event or (event['process'] in bridge_aborts
                                          and event['after_shutdown_marker'])
        if not allowed_event:
            unwaived.append(event)

    window_complete = bool(window_result and window_result.get('complete') is True
                           and not window_result.get('error'))
    reasons = []
    if len(markers) != 1:
        reasons.append('Exactly one shutdown marker is required to establish fault timing')
    if not complete:
        reasons.append('Post-trial process inventory is incomplete')
    if owned or unowned:
        reasons.append('Relevant processes survived teardown')
    if not window_complete:
        reasons.append('Owned window closure did not complete')
    if cleanup['launch_returncode'] != 0:
        reasons.append('Launch leader did not exit with status 0')
    if unwaived:
        reasons.append('Crash or abnormal exit outside the proven shutdown bond exception')
    clean = bool(not events and not reasons)
    exception = bool((proven or bridge_aborts) and not reasons)
    audit.update({
        'schema_version': 1,
        'shutdown_marker': marker,
        'shutdown_marker_count': len(markers),
        'live_owned_survivors': owned,
        'live_unowned_survivors': unowned,
        'survivors': owned + unowned,
        'post_inventory_complete': complete,
        'inventory_errors': inventory_errors,
        'window_close_complete': window_complete,
        'isolation_pass': isolation_pass,
        'bond_abort': bool(proven),
        'bond_abort_processes': list(proven.values()),
        'bond_unconfirmed_candidates': candidates,
        'truth_bridge_abort': bool(bridge_aborts),
        'truth_bridge_abort_processes': list(bridge_aborts.values()),
        'allowed_exception': exception,
        'clean': clean,
        'continue_allowed': clean or exception,
        'unwaived_events': unwaived,
        'stop_reasons': reasons,
        'scope': 'Launch log, owned cleanup, owned windows, and full relevant host process inventory; transport/readiness audited separately.',
    })
    return audit


VERDICT_COLUMNS = ('Followed the path?', 'Touched an obstacle?', 'Oscillated or spun?',
                   'Recovery seen?', 'Notes')
AUTO_COLUMNS = (
    ('Trial', 'record_id'), ('Map', 'map'), ('Controller', 'controller'),
    ('Tier', 'tier'), ('Goal ID', 'goal_id'), ('Outcome', 'outcome'),
    ('Time s', 'time_s'), ('True path m', 'true_path_length_m'),
    ('Odom path m', 'path_length_m'), ('True final error m', 'true_final_error_m'),
    ('AMCL goal error m', 'amcl_goal_error_m'), ('AMCL vs truth m', 'amcl_error_m'),
    ('Recoveries', 'recoveries'), ('Bond abort', 'bond_abort'),
    ('Bridge shutdown abort', 'truth_bridge_abort'),
    ('Isolation pass', 'isolation_pass'), ('Survivors', 'survivor_count'),
    ('GZ_PARTITION', 'gz_partition'), ('ROS_DOMAIN_ID', 'ros_domain_id'),
    ('Evidence', 'evidence'),
)


def build_record(trial, audit, *, map_name, controller, tier, goal_id,
                 trial_id, trial_dir, transport, artifacts=None, readiness=None):
    """Build a lossless-enough visual record from an existing collector result.

    `trial_dir` should be relative to run_dir for portable check-sheet links.
    `artifacts` accepts screenshots (stage -> file/list), video, and other links.
    The original collector result remains in trial.json and is never rewritten.
    """
    trial = trial or {}
    audit = audit or {}
    artifacts = artifacts or {}
    # Error of AMCL against truth is distinct from AMCL's remaining goal error.
    amcl_error = trial.get('final_localization_error_m')
    if amcl_error is None:
        ax, ay = trial.get('final_map_x'), trial.get('final_map_y')
        tx, ty = trial.get('final_true_x'), trial.get('final_true_y')
        if all(isinstance(value, (int, float)) for value in (ax, ay, tx, ty)):
            amcl_error = ((ax - tx) ** 2 + (ay - ty) ** 2) ** 0.5
    return {
        'schema_version': 1,
        'dataset_kind': 'visual_validation_only_not_controller_comparison_results',
        'record_id': f'{controller}/{tier}/{trial_id}',
        'map': map_name, 'controller': controller, 'tier': tier,
        'goal_id': str(goal_id), 'trial_id': str(trial_id),
        'outcome': trial.get('result', 'NOT_RUN'),
        'time_s': trial.get('time_s'), 'wall_time_s': trial.get('wall_time_s'),
        'path_length_m': trial.get('path_len_m'),
        'true_path_length_m': trial.get('true_path_len_m'),
        'true_final_error_m': trial.get('true_final_pos_error_m'),
        'amcl_goal_error_m': trial.get('final_pos_error_m'),
        'amcl_error_m': amcl_error,
        'recoveries': trial.get('recoveries'),
        'bond_abort': bool(audit.get('bond_abort')),
        'truth_bridge_abort': bool(audit.get('truth_bridge_abort')),
        'allowed_shutdown_exception': bool(audit.get('allowed_exception')),
        'shutdown_clean': bool(audit.get('clean')),
        'isolation_pass': bool(audit.get('isolation_pass')),
        'continue_allowed': bool(audit.get('continue_allowed')),
        'survivors': audit.get('survivors', []),
        'survivor_count': len(audit.get('survivors', [])),
        'gz_partition': transport.get('gz_partition', transport.get('GZ_PARTITION')),
        'ros_domain_id': transport.get('ros_domain_id', transport.get('ROS_DOMAIN_ID')),
        'screenshots': artifacts.get('screenshots', {}),
        'video': artifacts.get('video'),
        'trial_dir': str(trial_dir),
        'readiness': readiness,
        'artifacts': artifacts,
        'verdict': {column: '' for column in VERDICT_COLUMNS},
    }


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return f'{value:.4f}'
    return str(value).replace('&', '&amp;').replace('|', '&#124;').replace('\n', '<br>')


def _read_verdicts(path):
    """Read our simple Markdown table without interpreting user text as code."""
    if not path.exists():
        return {}
    lines = path.read_text().splitlines()
    expected = ['Trial'] + list(VERDICT_COLUMNS)
    result = {}
    in_table = False
    for line in lines:
        if not line.startswith('|'):
            in_table = False
            continue
        cells = [value.strip() for value in line.strip().strip('|').split('|')]
        if cells == expected:
            in_table = True
            continue
        if not in_table or len(cells) != len(expected) or all(set(value) <= {'-', ':'} for value in cells):
            continue
        values = [value.replace('&#124;', '|').replace('<br>', '\n').replace('&amp;', '&') for value in cells]
        result[values[0]] = dict(zip(VERDICT_COLUMNS, values[1:]))
    return result


def _evidence(row):
    directory = row.get('trial_dir', '')
    result = [f'[trial]({directory}/trial.json)',
              f'[readiness]({directory}/checks.json)',
              f'[shutdown]({directory}/shutdown_audit.json)',
              f'[cleanup]({directory}/process_cleanup.json)']
    artifacts = row.get('artifacts') or {}
    for stage, files in (row.get('screenshots') or {}).items():
        for index, path in enumerate(files if isinstance(files, list) else [files]):
            if path:
                result.append(f'[{stage}{index + 1}]({path})')
    if row.get('video'):
        result.append(f'[video]({row["video"]})')
    for key in ('transport', 'preflight', 'postflight', 'launch_log'):
        if artifacts.get(key):
            result.append(f'[{key}]({artifacts[key]})')
    return ' '.join(result)


def write_checksheet(run_dir, rows, metadata=None):
    """Render CHECKSHEET.md, records.csv and records.json, preserving verdicts.

    User verdicts edited in the dedicated Markdown table take precedence over
    existing CSV fields when nonblank. Neither table is used as instructions.
    All outputs belong to the new run directory supplied by the runner.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = metadata or {}
    md_path = run_dir / 'CHECKSHEET.md'
    csv_path = run_dir / 'records.csv'
    saved = {}
    if csv_path.exists():
        with csv_path.open(newline='') as stream:
            for old in csv.DictReader(stream):
                saved[old.get('record_id', '')] = {column: old.get(column, '') for column in VERDICT_COLUMNS}
    for key, values in _read_verdicts(md_path).items():
        current = saved.setdefault(key, {})
        for column, value in values.items():
            if value:
                current[column] = value
    records = []
    for source in rows:
        row = dict(source)
        row['verdict'] = dict(row.get('verdict') or {})
        row['verdict'].update(saved.get(row['record_id'], {}))
        row['evidence'] = _evidence(row)
        records.append(row)

    bond_count = sum(bool(row.get('bond_abort')) for row in records)
    truth_bridge_count = sum(bool(row.get('truth_bridge_abort')) for row in records)
    isolated = sum(bool(row.get('isolation_pass')) for row in records)
    clean = sum(bool(row.get('shutdown_clean')) for row in records)
    total = len(records)
    lines = [
        '# Visual validation check sheet', '',
        f'Map: {_cell(metadata.get("map", records[0]["map"] if records else ""))}. '
        f'Run: {_cell(metadata.get("run", run_dir.name))}. Robot: TurtleBot3 Waffle; simulation only.', '',
        '**Visual validation only. These records are not results of a clean paired controller comparison. '
        'Different maps remain separate datasets.**', '',
        f'Recorded trials: {total}; isolation passes: {isolated}/{total}; '
        f'crash-free shutdowns: {clean}/{total}; proven Nav2 bond abort trials: {bond_count}; '
        f'proven bridge shutdown abort trials: {truth_bridge_count}.', '',
        'MPPI selected, pending the re-run.', '',
        'Time is simulated navigation time; wall time is retained in records.json. '
        'True path and true final goal error use Gazebo ground truth. '
        'AMCL goal error measures distance to the goal; AMCL vs truth measures localisation error.', '',
        '| ' + ' | '.join(column for column, _ in AUTO_COLUMNS) + ' |',
        '| ' + ' | '.join('---' for _ in AUTO_COLUMNS) + ' |',
    ]
    for row in records:
        lines.append('| ' + ' | '.join(row.get(key, '') if key == 'evidence' else _cell(row.get(key))
                                      for _, key in AUTO_COLUMNS) + ' |')
    lines += ['', 'Enter visual verdicts below. Leave unknown observations blank; automatic '
              'navigation success does not establish obstacle clearance or visual path following.', '',
              '| Trial | ' + ' | '.join(VERDICT_COLUMNS) + ' |',
              '| --- | ' + ' | '.join('---' for _ in VERDICT_COLUMNS) + ' |']
    for row in records:
        lines.append('| ' + _cell(row['record_id']) + ' | ' + ' | '.join(
            _cell(row['verdict'].get(column, '')) for column in VERDICT_COLUMNS) + ' |')
    lines += ['', 'Isolation includes readiness and transport checks only where the runner records their '
              'success. A permitted Nav2 bond abort or bridge shutdown abort is counted separately and is never called '
              'crash-free.', '']
    md_path.write_text('\n'.join(lines))
    flat_fields = [key for _, key in AUTO_COLUMNS] + ['wall_time_s', 'shutdown_clean',
                   'allowed_shutdown_exception', 'continue_allowed', 'screenshots', 'video'] + list(VERDICT_COLUMNS)
    with csv_path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=flat_fields)
        writer.writeheader()
        for row in records:
            flat = {key: row.get(key) for key in flat_fields if key not in VERDICT_COLUMNS}
            flat['screenshots'] = json.dumps(row.get('screenshots', {}), sort_keys=True)
            flat.update({column: row['verdict'].get(column, '') for column in VERDICT_COLUMNS})
            writer.writerow(flat)
    (run_dir / 'records.json').write_text(json.dumps(records, indent=2) + '\n')
    return {'checksheet': str(md_path), 'records_csv': str(csv_path),
            'records_json': str(run_dir / 'records.json'), 'trial_count': total,
            'isolation_pass_count': isolated, 'bond_abort_count': bond_count,
            'truth_bridge_abort_count': truth_bridge_count}
