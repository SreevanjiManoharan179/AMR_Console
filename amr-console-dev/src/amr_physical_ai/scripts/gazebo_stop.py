"""Stop this launch's Gazebo server through its isolated transport, without signals.

Acceptance of the transport request and exit of the recorded PID incarnations
are separate evidence. The caller must still audit launcher exits and the full
post-teardown process inventory.
"""
import subprocess
import time

from transport_isolation import process_inventory


def stop_gazebo(owned, environment, *, inventory=process_inventory,
                run=subprocess.run, clock=time.monotonic, sleep=time.sleep,
                timeout=10):
    snapshot = inventory(owned)
    targets = [p for p in snapshot
               if p.get('owned') and p.get('cmd', '').startswith('gz sim ')]
    report = {'service': '/server_control', 'request': 'stop: true',
              'partition': environment['GZ_PARTITION'], 'targets': targets,
              'accepted': False, 'complete': not targets}
    if not targets:
        report['note'] = 'No live owned Gazebo processes; no request sent.'
        return report
    foreign = [p for p in snapshot if not p.get('owned')]
    if foreign:
        report.update(error='Unowned simulation processes appeared; server stop request refused.',
                      unowned=foreign)
        return report
    # Recheck immediately before crossing the transport mutation boundary.
    # Ownership of a numeric PID alone never authorizes a server request.
    confirmed = inventory(owned)
    report['pre_request_inventory'] = confirmed
    foreign = [p for p in confirmed if not p.get('owned')]
    if foreign:
        report.update(error='Unowned simulation processes appeared; server stop request refused.',
                      unowned=foreign)
        return report
    identities = {(p['pid'], p['start_ticks']) for p in targets}
    target_pids = {p['pid'] for p in targets}
    changed = [p for p in confirmed if p['pid'] in target_pids
               and (p['pid'], p['start_ticks']) not in identities]
    if changed:
        report.update(error='Recorded Gazebo PID identity changed; server stop request refused.',
                      changed_identities=changed)
        return report
    live_ids = {(p['pid'], p['start_ticks']) for p in confirmed if p.get('owned')}
    if not identities & live_ids:
        report.update(complete=True, remaining=[],
                      note='Recorded owned Gazebo processes already exited; no request sent.')
        return report
    try:
        result = run(['gz', 'service', '-s', '/server_control',
                      '--reqtype', 'gz.msgs.ServerControl', '--reptype', 'gz.msgs.Boolean',
                      '--timeout', '2000', '--req', 'stop: true'],
                     env=environment, capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired) as exc:
        report['error'] = f'Gazebo server stop request failed: {exc}'
        return report
    report.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                  accepted=result.returncode == 0 and 'data: true' in result.stdout)
    if not report['accepted']:
        report['error'] = 'Gazebo server stop request was not acknowledged.'
        return report
    start = clock()
    while True:
        live = [p for p in owned.refresh() if (p['pid'], p['start_ticks']) in identities]
        if not live or clock() - start >= timeout:
            report.update(complete=not live, remaining=live, wait_s=clock() - start)
            if live:
                report['error'] = 'Gazebo accepted stop but its owned processes did not exit.'
            return report
        sleep(.05)
