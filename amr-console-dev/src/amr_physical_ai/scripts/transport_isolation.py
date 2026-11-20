"""Read-only process ownership and transport isolation checks (Linux).

Ownership always means a matching (PID, process start tick) recorded by the
launch owner. No function in this module signals a process. Transport probes
cover this host/network namespace; the runner separately probes the ROS graph.
Only ROS_DOMAIN_ID and GZ_PARTITION are retained from process environments.
"""
import hashlib
from pathlib import Path
import re

from process_ownership import process_table


PROC = Path('/proc')
TRANSPORT_KEYS = ('ROS_DOMAIN_ID', 'GZ_PARTITION')
ENVIRONMENT_KEYS = TRANSPORT_KEYS + ('ROS_LOCALHOST_ONLY', 'ROS_AUTOMATIC_DISCOVERY_RANGE')
DOMAIN_MIN, DOMAIN_MAX = 20, 99
ROS_EXECUTABLES = {
    'amcl', 'map_server', 'map_saver_server', 'rviz2', 'robot_state_publisher',
    'parameter_bridge', 'static_bridge', 'image_bridge', 'bridge',
    'planner_server', 'controller_server', 'smoother_server', 'behavior_server',
    'bt_navigator', 'waypoint_follower', 'velocity_smoother', 'collision_monitor',
    'lifecycle_manager', 'route_server', 'docking_server', 'recoveries_server',
    'costmap_2d_node', 'nav2_costmap_2d', 'slam_toolbox', 'async_slam_toolbox_node',
    'sync_slam_toolbox_node', 'localization_slam_toolbox_node',
    'component_container', 'component_container_isolated', 'component_container_mt',
}
GAZEBO_EXECUTABLES = {'gz', 'gzserver', 'gzclient', 'gz-sim'}


def _identity(pid):
    try:
        fields = (PROC / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[19]) if fields[0] != 'Z' else None
    except (OSError, ValueError, IndexError):
        return None


def _metadata(pid):
    directory = PROC / str(pid)
    comm = (directory / 'comm').read_text().strip()
    argv = (directory / 'cmdline').read_bytes().split(b'\0')
    argv = [part.decode(errors='replace') for part in argv if part]
    return comm, argv


def is_relevant(comm, argv):
    """Match executable identities, not arbitrary prose in command arguments."""
    if not argv:
        return (comm in ROS_EXECUTABLES or comm in GAZEBO_EXECUTABLES
                or any(name[:15] == comm for name in ROS_EXECUTABLES))
    executable = Path(argv[0]).name
    candidates = {executable}
    # Some ROS Python entry points run through python; only the actual script
    # argument is an executable candidate, never the contents of -c commands.
    if executable.startswith(('python', 'ruby')) and len(argv) > 1:
        if not argv[1].startswith('-'):
            candidates.add(Path(argv[1]).name)
    if candidates & ROS_EXECUTABLES:
        return True
    if any(name in GAZEBO_EXECUTABLES or name.startswith('gz-sim')
           for name in candidates):
        return True
    # gz can change its title to "gz sim ..." while Linux still calls it ruby.
    if comm.startswith('ruby'):
        early = [Path(arg).name for arg in argv[:5]]
        if 'gz' in early and 'sim' in early:
            return True
        if any(arg.startswith('gz sim ') for arg in argv[:2]):
            return True
    return False


def _owned_identities(owned):
    if owned is None:
        return set()
    owned.refresh()
    with owned.lock:
        return {(int(pid), rec['start_ticks']) for pid, rec in owned.records.items()}


def process_inventory(owned=None):
    """All live relevant host processes, including owned and foreign survivors."""
    owned_ids = _owned_identities(owned)
    result = []
    for pid, rec in process_table().items():
        if rec['state'] == 'Z':
            continue
        inspection_error = None
        try:
            comm, argv = _metadata(pid)
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            # A relevant app must not disappear from an audit just because its
            # command line is hidden. comm is ordinarily world-readable.
            try:
                comm = (PROC / str(pid) / 'comm').read_text().strip()
            except OSError:
                raise RuntimeError(f'Cannot inspect live process {pid}: {exc}') from exc
            if not is_relevant(comm, []):
                continue
            argv = ['<command line unreadable>']
            inspection_error = 'Relevant process command line is unreadable'
        if not inspection_error and not is_relevant(comm, argv):
            continue
        if _identity(pid) != rec['start_ticks']:
            continue  # Exited/reused PID; never apply old ownership to it.
        result.append({**rec, 'comm': comm, 'cmd': ' '.join(argv),
                       'owned': (pid, rec['start_ticks']) in owned_ids})
        if inspection_error:
            result[-1]['inspection_error'] = inspection_error
    return sorted(result, key=lambda item: item['pid'])


def _transport_environment(pid):
    """Do not return, print or persist other environment values."""
    raw = (PROC / str(pid) / 'environ').read_bytes()
    result = {}
    for part in raw.split(b'\0'):
        key, separator, value = part.partition(b'=')
        if separator and key in tuple(name.encode() for name in ENVIRONMENT_KEYS):
            result[key.decode()] = value.decode(errors='replace')
    return result


def _udp_ports():
    """Bound UDP ports in the current network namespace, IPv4 and IPv6."""
    result = set()
    for name in ('udp', 'udp6'):
        path = PROC / 'net' / name
        try:
            lines = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == 'udp6':
                continue
            raise
        for line in lines:
            columns = line.split()
            if len(columns) >= 2:
                result.add(int(columns[1].rsplit(':', 1)[1], 16))
    return result


def transport_snapshot():
    """Local reservation evidence; permission-limited processes are explicit."""
    processes, inaccessible = [], []
    for pid, rec in process_table().items():
        if rec['state'] == 'Z':
            continue
        try:
            transport = _transport_environment(pid)
        except FileNotFoundError:
            continue
        except PermissionError:
            inaccessible.append(pid)
            continue
        if _identity(pid) == rec['start_ticks'] and transport:
            processes.append({'pid': pid, 'start_ticks': rec['start_ticks'],
                              **transport})
    return {'processes': processes, 'udp_ports': sorted(_udp_ports()),
            'inaccessible_environment_pids': inaccessible,
            'scope': 'Local /proc and UDP sockets in this network namespace; '
                     'not proof of remote-host or future exclusivity.'}


def transport_collisions(environment, snapshot):
    domain = int(environment['ROS_DOMAIN_ID'])
    partition = environment['GZ_PARTITION']
    collisions = []
    for rec in snapshot['processes']:
        reasons = []
        try:
            if int(rec.get('ROS_DOMAIN_ID', '-1')) == domain:
                reasons.append('ROS_DOMAIN_ID in use')
        except ValueError:
            pass
        if rec.get('GZ_PARTITION') == partition:
            reasons.append('GZ_PARTITION in use')
        if reasons:
            collisions.append({**rec, 'reasons': reasons})
    # Standard RTPS domain port block. Any occupied port in this block is
    # conservatively rejected, including a non-DDS application using that port.
    base = 7400 + 250 * domain
    ports = [port for port in snapshot['udp_ports'] if base <= port < base + 250]
    if ports:
        collisions.append({'reason': 'UDP ports in reserved RTPS domain block',
                           'ports': ports})
    return collisions


def allocate_transport(run_id, map_name, ordinal, used_domains,
                       domain_min=DOMAIN_MIN, domain_max=DOMAIN_MAX):
    """Allocate a never-reused domain within this run and a unique partition.

    The caller owns the workspace run lock and records returned domain IDs in
    used_domains. A finite domain block is reused across independent runs only
    after an empty local check and the runner's separate empty-graph probe.
    """
    if not (0 <= domain_min <= domain_max <= 101):
        raise ValueError('Reserved ROS domain block must be within 0–101')
    if not isinstance(ordinal, int) or ordinal < 1:
        raise ValueError('Trial ordinal must be a positive integer')
    used = {int(value) for value in used_domains}
    count = domain_max - domain_min + 1
    if ordinal > count:
        raise RuntimeError(f'{ordinal} trials exceed the {count}-domain reserved block')
    identity = f'{map_name}\0{run_id}\0{ordinal}'
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    label = re.sub(r'[^a-zA-Z0-9_]', '_', f'{map_name}_{run_id}')[-70:]
    partition = f'amr_vis_{label}_{ordinal:03d}_{digest}'
    snapshot = transport_snapshot()
    # Different runs begin at different positions while avoiding all domains
    # already used during this run and all locally occupied candidates.
    start = int(hashlib.sha256(str(run_id).encode()).hexdigest()[:8], 16) % count
    rejected = []
    for offset in range(count):
        domain = domain_min + (start + ordinal - 1 + offset) % count
        if domain in used:
            continue
        env = {'ROS_DOMAIN_ID': str(domain), 'GZ_PARTITION': partition}
        collisions = transport_collisions(env, snapshot)
        if not collisions:
            return {**env, 'domain_block': [domain_min, domain_max],
                    'ordinal': ordinal, 'local_collision_checks': {
                        'collisions': [], 'rejected_domains': rejected,
                        'environment_processes_checked': len(snapshot['processes']),
                        'inaccessible_environment_pids': snapshot['inaccessible_environment_pids'],
                        'udp_ports_checked': len(snapshot['udp_ports']),
                        'scope': snapshot['scope']}}
        rejected.append({'domain': domain, 'collisions': collisions})
    raise RuntimeError('No unused, locally unoccupied ROS domain remains in reserved '
                       f'block {domain_min}–{domain_max}: {rejected}')


def assert_transport_available(environment):
    snapshot = transport_snapshot()
    collisions = transport_collisions(environment, snapshot)
    if collisions:
        raise RuntimeError(f'Trial transport is occupied; no process was signalled: {collisions}')
    return {'collisions': [], **snapshot}


def verify_transport(owned, expected_env):
    """Verify live launch leader and every relevant descendant's two keys."""
    inventory = process_inventory(owned)
    relevant = [record for record in inventory if record['owned']]
    table = process_table()
    leader = table.get(owned.proc.pid)
    own_ids = _owned_identities(owned)
    if leader and (leader['pid'], leader['start_ticks']) in own_ids:
        if not any(item['pid'] == leader['pid'] for item in relevant):
            relevant.append({**leader, 'comm': 'launch leader', 'owned': True})
    expected = {key: str(expected_env[key]) for key in ENVIRONMENT_KEYS
                if key in expected_env}
    if any(key not in expected for key in TRANSPORT_KEYS):
        raise ValueError('Both ROS_DOMAIN_ID and GZ_PARTITION are required')
    checks, mismatches = [], []
    if not relevant:
        mismatches.append({'reason': 'No live owned launch processes to verify'})
    for record in relevant:
        check = {'pid': record['pid'], 'start_ticks': record['start_ticks'],
                 'comm': record['comm']}
        try:
            check['actual'] = _transport_environment(record['pid'])
            if _identity(record['pid']) != record['start_ticks']:
                check['error'] = 'Process exited or PID identity changed during check'
            elif any(check['actual'].get(key) != value for key, value in expected.items()):
                check['error'] = 'Missing or mismatched transport environment'
        except OSError as exc:
            check['error'] = f'Cannot read transport environment: {exc}'
        if 'error' in check:
            mismatches.append(check)
        checks.append(check)
    foreign = [record for record in inventory if not record['owned']]
    if foreign:
        mismatches.append({'reason': 'Unowned relevant processes present', 'processes': foreign})
    return {'ok': not mismatches, 'expected': expected,
            'processes': checks, 'mismatches': mismatches,
            'scope': 'Launch leader and live relevant descendants; PID/start tick checked.'}
