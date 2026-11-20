"""Read-only attachment and approval gates. No ROS imports, token or HTTP access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import yaml

from .clarify import Grounder, prediction_set
from .tags import ROOT, Tags, digest

TRANSPORT_KEYS = ('ROS_DOMAIN_ID', 'GZ_PARTITION', 'ROS_LOCALHOST_ONLY', 'ROS_AUTOMATIC_DISCOVERY_RANGE')
REQUIRED_PROCESSES = {'map_server', 'amcl', 'bt_navigator', 'controller_serv', 'launch leader'}
DEFAULT_RESULTS = ROOT / 'config/calibration/proposed_system/tasks/results.json'


def process_transport(pid, proc=Path('/proc')):
    """Select transport fields only; never return or persist the process environment."""
    directory = proc / str(pid)
    fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] == 'Z':
        raise RuntimeError('Attached process has exited')
    tick = int(fields[19])
    selected = {}
    for field in (directory / 'environ').read_bytes().split(b'\0'):
        key, sep, value = field.partition(b'=')
        if sep and key.decode(errors='replace') in TRANSPORT_KEYS:
            selected[key.decode()] = value.decode()
    after = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
    if after[0] == 'Z' or int(after[19]) != tick:
        raise RuntimeError('Attached process identity changed during inspection')
    return tick, selected


class SessionAttachment:
    def __init__(self, run_id, root=ROOT, process_reader=process_transport):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
            raise ValueError('Use a console run ID, not a path')
        self.root = Path(root).resolve()
        self.path = self.root / 'data/logs/console' / run_id
        if not self.path.resolve().is_relative_to((self.root / 'data/logs/console').resolve()):
            raise ValueError('Console session path escapes the workspace')
        self.run_id, self.reader = run_id, process_reader
        self.spec_hash = digest(self.path / 'spec.json')
        self.transport_hash = digest(self.path / 'transport_verification.json')
        self.check()

    def check(self):
        if (digest(self.path / 'spec.json') != self.spec_hash or
                digest(self.path / 'transport_verification.json') != self.transport_hash):
            raise RuntimeError('Console session identity changed')
        run = json.loads((self.path / 'run.json').read_text())
        spec = json.loads((self.path / 'spec.json').read_text())
        verification = json.loads((self.path / 'transport_verification.json').read_text())
        if (run.get('run_id') != self.run_id or spec.get('run_id') != self.run_id or
                run.get('action_id') != 'nav.start' or spec.get('kind') != 'navigation' or
                run.get('status') != 'running' or run.get('end_at') is not None or
                run.get('params', {}).get('map') != 'proposed_system' or spec.get('map_id') != 'proposed_system' or
                run.get('params', {}).get('controller') != 'mppi'):
            raise RuntimeError('Attach only to a running Console nav.start session on proposed_system with MPPI')
        if any(o.get('end_at') is None for o in run.get('operations', [])):
            raise RuntimeError('A Console operation is active')
        if not verification.get('ok'):
            raise RuntimeError('Console transport verification has not passed')
        expected = verification['expected']
        domain = expected.get('ROS_DOMAIN_ID', '')
        if not str(domain).isdigit() or not 0 <= int(domain) <= 101 or not expected.get('GZ_PARTITION'):
            raise RuntimeError('Invalid console transport pair')
        for key in ('ROS_DOMAIN_ID', 'GZ_PARTITION'):
            if any(str(record.get('env', {}).get(key)) != str(expected[key]) for record in (run, spec)):
                raise RuntimeError('Console records disagree on transport')
        records = verification.get('processes', [])
        if not REQUIRED_PROCESSES <= {r['comm'] for r in records}:
            raise RuntimeError('Missing required live navigation process identities')
        for record in records:
            if record['comm'] not in REQUIRED_PROCESSES:
                continue
            tick, env = self.reader(record['pid'])
            if tick != record['start_ticks'] or any(env.get(k) != v for k, v in expected.items()):
                raise RuntimeError('Console process exited, was reused, or has a different transport')
        self.environment = {k: str(v) for k, v in expected.items() if k in TRANSPORT_KEYS}
        return self.environment

    def configure_environment(self):
        self.check()
        for key in TRANSPORT_KEYS:
            os.environ.pop(key, None)
        os.environ.update(self.environment)
        os.environ['RMW_IMPLEMENTATION'] = 'rmw_cyclonedds_cpp'


def load_threshold(tags, path=DEFAULT_RESULTS):
    result = json.loads(Path(path).read_text())
    meta = result['meta']
    if (meta['tag_semantics_sha256'] != tags.semantic_digest() or
            meta['scorer'] != Grounder(tags).room_scorer.name or
            meta['temperature'] != .1 or meta['alpha'] != .1 or meta['aggregation'] != 'max'):
        raise RuntimeError('Tags or scorer no longer match the pilot calibration')
    for key in ('map_yaml', 'map_image', 'layout'):
        if meta['input_fingerprints'][key] != tags.fingerprints()[key]:
            raise RuntimeError('Pilot map/layout fingerprint mismatch')
    q = result['headline']['q_hat']
    prediction_set({}, q)
    return q


class ApprovalGate:
    def __init__(self, tags, root=ROOT, results=DEFAULT_RESULTS):
        self.tags, self.root, self.results = tags, Path(root), Path(results)
        self.named = self.root / 'src/amr_physical_ai/config/named_locations_proposed_system.yaml'
        self.approvals = self.root / 'tools/amr_console/approvals.yaml'
        self.original = {**tags.fingerprints(), 'named_locations': digest(self.named),
                         'results': digest(self.results)}
        tags.validate()
        self.q_hat = load_threshold(tags, self.results)
        self.check()

    def check(self):
        current = {**self.tags.fingerprints(), 'named_locations': digest(self.named),
                   'results': digest(self.results)}
        if current != self.original:
            raise RuntimeError('Tags, map, locations or calibration changed; start a fresh preview')
        if (yaml.safe_load(self.tags.path.read_text())['approval_status'] != 'confirmed' or
                yaml.safe_load(self.named.read_text())['approval_status'] != 'confirmed' or
                yaml.safe_load(self.approvals.read_text()).get('proposed_system_places') is not True):
            raise RuntimeError('Live motion requires approved delivery tags and both owner approvals of the proposed_system places')
