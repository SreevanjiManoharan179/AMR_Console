"""Gazebo window settings for each world: grid over its saved map, camera on it (display only)."""
import importlib.util
from pathlib import Path
import sys
import types
import xml.etree.ElementTree as ET

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
LAUNCH = PACKAGE / 'launch/tb3_mapping.launch.py'
MAPS = PACKAGE / 'maps'
SPAWN = [-2.0, -0.5, 0.0]
ROS_MODULES = ('ament_index_python', 'ament_index_python.packages', 'launch', 'launch.actions',
               'launch.conditions', 'launch.launch_description_sources', 'launch.substitutions',
               'launch_ros', 'launch_ros.actions', 'xacro')
USER_FILE = '''<?xml version="1.0"?>
<!-- Window -->
<window><width>1000</width></window>
<plugin filename="MinimalScene" name="3D View">
  <engine>ogre2</engine>
  <camera_pose>-6 0 6 0 0.5 0</camera_pose>
</plugin>
<plugin filename="GridConfig" name="Old grid"><insert><pose>0 0 0 0 0 0</pose></insert></plugin>
<plugin filename="EntityTree" name="Entity tree"/>
'''


def stub_attribute(name):
    if name.startswith('__'):
        raise AttributeError(name)
    return object


@pytest.fixture
def launch_file(monkeypatch):
    """The launch file as a module; ROS modules are stubbed only where this Python lacks them."""
    for name in ROS_MODULES:
        try:
            # The package's own launch/ folder can look like a namespace package "launch"; that is not ROS.
            missing = getattr(importlib.import_module(name), '__file__', None) is None
        except ImportError:
            missing = True
        if missing:
            stub = types.ModuleType(name)
            stub.__getattr__ = stub_attribute
            monkeypatch.setitem(sys.modules, name, stub)
    spec = importlib.util.spec_from_file_location('tb3_mapping_launch_under_test', LAUNCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def window(launch_file, tmp_path, world='proposed_system', user_file=USER_FILE):
    home = tmp_path / 'home'
    if user_file is not None:
        (home / '.gz/sim/8').mkdir(parents=True)
        (home / '.gz/sim/8/gui.config').write_text(user_file)
    out = tmp_path / 'out'
    out.mkdir(parents=True)
    target = launch_file.gazebo_window(MAPS / f'{world}.yaml', SPAWN, out, home=home)
    if target is None:
        return None
    text = target.read_text()
    assert text.startswith('<?xml')
    return ET.fromstring('<root>' + text.split('?>', 1)[1] + '</root>')


def test_proposed_system_grid_is_centred_on_the_office_on_whole_map_metres(launch_file, tmp_path):
    root = window(launch_file, tmp_path)
    grids = [p for p in root.findall('plugin') if p.get('filename') == 'GridConfig']
    assert len(grids) == 1                                     # the user's old grid entry is replaced
    insert = grids[0].find('insert')
    # proposed_system map: x -3.5..15.5, y -6.5..6.5 -> centre map (6, 0) = world (4, -0.5); 20 cells of 1 m
    assert insert.findtext('pose') == '4 -0.5 0.005 0 0 0'
    assert insert.findtext('horizontal_cell_count') == '20'
    assert insert.findtext('cell_length') == '1'


def test_camera_looks_at_the_office_and_the_rest_of_the_window_is_kept(launch_file, tmp_path):
    root = window(launch_file, tmp_path)
    x, y, z, roll, pitch, yaw = map(float, root.find("plugin[@filename='MinimalScene']/camera_pose").text.split())
    assert x == pytest.approx(4.0) and y < -0.5 and z > 5 and pitch == pytest.approx(.9)
    assert root.find('window/width').text == '1000'
    assert root.find("plugin[@filename='EntityTree']") is not None


@pytest.mark.parametrize('world', ['proposed_system', 'tb3_sandbox'])
def test_every_world_grid_covers_its_saved_map(launch_file, tmp_path, world):
    insert = window(launch_file, tmp_path, world).find("plugin[@filename='GridConfig']/insert")
    x, y = map(float, insert.findtext('pose').split()[:2])
    half = int(insert.findtext('horizontal_cell_count')) / 2
    x0, x1, y0, y1 = launch_file.map_extent(MAPS / f'{world}.yaml')
    to_world = SPAWN[:2]
    assert x - half <= x0 + to_world[0] and x1 + to_world[0] <= x + half
    assert y - half <= y0 + to_world[1] and y1 + to_world[1] <= y + half
    assert (x - to_world[0], y - to_world[1]) == (round(x - to_world[0]), round(y - to_world[1]))   # whole map metres


def test_without_a_map_or_the_user_file_gazebo_keeps_its_own_settings(launch_file, tmp_path):
    assert window(launch_file, tmp_path, 'no_such_world') is None
    assert window(launch_file, tmp_path / 'b', user_file=None) is None
