"""Test-only "stations" world for the generic delivery-command adapter tests.

The adapter tests use a small SLAM map of the TurtleBot3 sandbox with four stations (home,
east, north, south). It is test data only, not a product world: install() copies it into a
temporary repository and registers its profile for the duration of one test.
"""
from pathlib import Path
import shutil

import yaml

DATA = Path(__file__).resolve().parent / 'data/stations'
MAP = 'traversal_20260930_121704'
PROFILE = {'map_id': 'stations', 'approval': 'stations_places',
           'locations': 'src/amr_physical_ai/config/named_locations.yaml',
           'grounding_results': 'config/calibration/stations/grounding/results.json'}
APPROVED = 'stations_places: true\n'


def install(tmp_path, monkeypatch):
    """Copy the stations files into tmp_path, confirm and approve them, and register the profile."""
    import typed_commands
    import registry
    for source, target in (('named_locations.yaml', PROFILE['locations']),
                           (f'{MAP}.yaml', f'src/amr_physical_ai/maps/{MAP}.yaml'),
                           (f'{MAP}.pgm', f'src/amr_physical_ai/maps/{MAP}.pgm')):
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DATA / source, path)
    path = tmp_path / PROFILE['locations']
    config = yaml.safe_load(path.read_text())
    config['approval_status'] = 'confirmed'
    config['map_yaml'] = f'../maps/{MAP}.yaml'
    path.write_text(yaml.safe_dump(config))
    approval = tmp_path / 'tools/amr_console/approvals.yaml'
    approval.parent.mkdir(parents=True, exist_ok=True)
    approval.write_text(APPROVED)
    monkeypatch.setitem(registry.COMMAND_MAPS, 'stations', {k: v for k, v in PROFILE.items() if k != 'map_id'})
    monkeypatch.setitem(registry._OPTIONAL_APPROVALS, PROFILE['approval'], False)
    monkeypatch.setattr(typed_commands, 'DEFAULT_PROFILE', dict(PROFILE))
    monkeypatch.setattr(typed_commands, 'GROUNDING_RESULTS', PROFILE['grounding_results'])
    return tmp_path
