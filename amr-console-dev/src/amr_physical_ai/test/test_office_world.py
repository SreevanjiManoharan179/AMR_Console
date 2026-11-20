"""Office worlds, exact maps and named places: the proposed_system office.

The compact corridor layout shape is checked with a test-only layout (data/office_v1_layout.yaml).

Offline: no ROS, simulator or network.
"""
from collections import deque
import copy
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / 'scripts'))
import office_world
from amr_physical_ai.command_pipeline import CommandParser, Locations

OFFICES = ('proposed_system',)
CORRIDOR_LAYOUT = Path(__file__).resolve().parent / 'data/office_v1_layout.yaml'   # test-only corridor shape
LAYOUTS = {**{office: PKG / f'worlds/{office}_layout.yaml' for office in OFFICES}, 'office_v1': CORRIDOR_LAYOUT}
PLACES_V2 = PKG / 'config/named_locations_proposed_system.yaml'
PLACE_FILES = {'proposed_system': PLACES_V2}
ROBOT_RADIUS = .22   # nav2_mppi.yaml robot_radius


def test_every_office_layout_is_known():
    assert [path.name for path in office_world.layouts()] == [LAYOUTS[office].name for office in OFFICES]


@pytest.mark.parametrize('office', OFFICES)
def test_generated_world_and_map_match_the_layout(office):
    for path, data in office_world.outputs(office_world.load_layout(LAYOUTS[office])).items():
        assert path.read_bytes() == data, f'{path.name} is out of date: run office_world.py generate'


@pytest.mark.parametrize('office', OFFICES)
def test_world_keeps_the_frames_ground_truth_and_amcl_rely_on(office):
    layout = office_world.load_layout(LAYOUTS[office])
    text = (PKG / f'worlds/{office}.sdf.xacro').read_text()
    assert f'from worlds/{office}_layout.yaml.' in text.splitlines()[1]
    world = ET.fromstring(text).find('world')
    assert world.get('name') == 'default'
    plugins = {plugin.get('name') for plugin in world.iter('plugin')}
    assert {'gz::sim::systems::Physics', 'gz::sim::systems::UserCommands', 'gz::sim::systems::SceneBroadcaster',
            'gz::sim::systems::Sensors', 'gz::sim::systems::Imu'} <= plugins
    model = next(model for model in world.findall('model') if model.get('name') == office)
    assert model.find('static').text == '1'
    assert model.find('pose').text.split()[:2] == ['-2', '-0.5']   # map (0, 0) = world (-2, -0.5)
    link = model.find('link')
    collisions = {c.get('name').removesuffix('_collision') for c in link.findall('collision')}
    visuals = {v.get('name').removesuffix('_visual') for v in link.findall('visual')}
    assert collisions == {box.name for box in office_world.boxes(layout)}
    assert visuals - collisions == {tile.name for tile in office_world.floor_tiles(layout)}


def test_proposed_system_map_has_its_corridor_doors_and_unknown_outside():
    layout = office_world.load_layout(LAYOUTS['proposed_system'])
    occupied, free, res, origin = office_world.read_map(PKG / 'maps/proposed_system.yaml')

    def cell(x, y):
        return math.floor((y - origin[1]) / res), math.floor((x - origin[0]) / res)

    assert occupied.shape == (260, 380) and origin[:2] == [-3.5, -6.5]
    for x in (3.0, 7.0, 11.0, 14.0):      # 1.8 m clear between the corridor wall faces
        assert all(free[cell(x, -.9 + (k + .5) * res)] for k in range(36))
        assert occupied[cell(x, .925)] and occupied[cell(x, -.925)]
    doors = 0
    for wall in layout['walls']:
        axis, fixed, _ = office_world._wall_segments(wall)
        for a, b in wall.get('doors', []):
            along = [a + (k + .5) * res for k in range(round((b - a) / res))]
            points = [(v, fixed) if axis == 'x' else (fixed, v) for v in along]
            edges = [(a - .025, fixed), (b + .025, fixed)] if axis == 'x' else [(fixed, a - .025), (fixed, b + .025)]
            assert all(free[cell(*p)] for p in points), wall['name']       # the doorway is open
            assert all(occupied[cell(*p)] for p in edges), wall['name']    # and framed by the wall
            doors += 1
    assert doors == 8
    for x, y in ((-3.3, 0.), (15.3, 0.), (6., 6.3), (6., -6.3)):
        assert not free[cell(x, y)] and not occupied[cell(x, y)]          # unknown outside the walls
    assert free[cell(-1.6, -4.5)] and occupied[cell(-.025, -4.5)]         # storage room and its east wall


@pytest.mark.parametrize('office', OFFICES)
def test_places_are_free_clear_and_reachable_from_home(office):
    locations = Locations(PLACE_FILES[office])   # also checks the 0.28 m conservative clearance of every place
    grid = locations.static_grid

    def cell(x, y):
        return (math.floor((y - grid.origin[1]) / grid.resolution),
                math.floor((x - grid.origin[0]) / grid.resolution))

    passable = grid.clearance >= ROBOT_RADIUS + grid.resolution
    start = cell(0., 0.)
    seen, queue = {start}, deque([start])
    while queue:
        row, col = queue.popleft()
        for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (row + step[0], col + step[1])
            if (0 <= nxt[0] < passable.shape[0] and 0 <= nxt[1] < passable.shape[1]
                    and passable[nxt] and nxt not in seen):
                seen.add(nxt)
                queue.append(nxt)
    for place in locations.by_name.values():
        assert cell(place.x, place.y) in seen, place.name
        assert grid.validate_point(place.x, place.y, .5)['free']   # at least 0.5 m from anything


def test_proposed_system_names_every_room_and_the_dock(tmp_path):
    proposed = yaml.safe_load(PLACES_V2.read_text())
    proposed['approval_status'] = 'proposed'
    proposed['map_yaml'] = str((PLACES_V2.parent / proposed['map_yaml']).resolve())
    fixture = tmp_path / 'named_locations_proposed_system.yaml'
    fixture.write_text(yaml.safe_dump(proposed, sort_keys=False))
    locations = Locations(fixture)
    assert list(locations.by_name) == ['home', 'reception', 'conference_hall', 'demo_area', 'cafeteria',
                                       'open_office', 'team_office', 'manager_cabin', 'storage']
    assert not locations.approved                     # fixture is explicitly proposed
    layout = office_world.load_layout(LAYOUTS['proposed_system'])

    def area(room):
        return (room['x'][1] - room['x'][0]) * (room['y'][1] - room['y'][0])

    for name, place in locations.by_name.items():
        if name == 'home':
            assert (place.x, place.y, place.yaw) == (0., 0., 0.)   # the spawn point and AMCL's initial pose
            continue
        around = [room for room in layout['rooms']
                  if room['x'][0] < place.x < room['x'][1] and room['y'][0] < place.y < room['y'][1]]
        innermost = min(around, key=area)['label'].replace(' ', '_')   # storage lies inside the open office
        assert innermost == ('lobby' if name == 'reception' else name), name


def test_proposed_system_names_ground_and_the_office_pair_needs_a_question():
    locations = Locations(PLACES_V2)
    parser = CommandParser(locations, mode='fallback')
    for name, place in locations.by_name.items():
        for words in (name.replace('_', ' '), *place.aliases):
            assert parser.parse('go to the ' + words)['location'] == name, words
    for text, name in (('go to the meeting room', 'conference_hall'), ('take me to the front desk', 'reception'),
                       ('head to the canteen', 'cafeteria'), ('return to base', 'home')):
        assert parser.parse(text)['location'] == name, text
    for text in ('go to the office', 'go to the cafetria', 'go to the library'):
        assert parser.parse(text)['clarification_needed'], text


@pytest.mark.parametrize('office, pair_text, pair', [
    ('proposed_system', 'go to the office', ('open office', 'team office')),
])
def test_token_scorer_keeps_every_office_name_selectable(office, pair_text, pair):
    sys.path.insert(0, str(PKG.parents[1] / 'tools/grounding_calibration'))
    from grounding_calibration.registry import load_registry
    from grounding_calibration.token_scorer import TokenScorer, content_tokens, token_similarity
    entries = load_registry(str(PLACE_FILES[office]))
    scorer = TokenScorer(entries)
    for entry in entries:
        probabilities = scorer.probabilities('go to the ' + entry.name)
        assert max(probabilities, key=probabilities.get) == entry.name and probabilities[entry.name] > .9
    both = scorer.probabilities(pair_text)
    assert both[pair[0]] == pytest.approx(both[pair[1]]) and both[pair[0]] > .45
    owners = {}
    for entry in entries:
        for variant in entry.variants:
            for token in content_tokens(variant):
                owners.setdefault(token, set()).add(entry.name)
    if office == 'proposed_system':   # one shared word, on purpose
        assert {token: names for token, names in owners.items() if len(names) > 1} == {'office': set(pair)}
    tokens = sorted(owners)
    for i, a in enumerate(tokens):
        for b in tokens[i + 1:]:
            if owners[a] != owners[b]:
                assert token_similarity(a, b) == 0, (a, b)   # no spelling match across places


@pytest.mark.parametrize('office', OFFICES)
def test_map_comparison_is_perfect_on_itself_and_catches_a_shift(tmp_path, office):
    exact = PKG / f'maps/{office}.yaml'
    same = office_world.compare_maps(exact, exact)
    assert same['exact_faces_seen_by_slam']['within_tolerance'] == 1.
    assert same['slam_occupied_on_exact_walls']['within_tolerance'] == 1.
    meta = yaml.safe_load(exact.read_text())
    meta['origin'] = [meta['origin'][0] + .3, meta['origin'][1], 0]
    meta['image'] = str(PKG / f'maps/{office}.pgm')
    shifted = tmp_path / 'shifted.yaml'
    shifted.write_text(yaml.safe_dump(meta))
    moved = office_world.compare_maps(shifted, exact)
    assert moved['exact_faces_seen_by_slam']['within_tolerance'] < .8


@pytest.mark.parametrize('change', [
    lambda layout: layout.update(doors_x=[[0., 1.], [1.6, 2.4], [6., 7.]]),           # door through a partition
    lambda layout: layout['furniture'][0].update(y=[.5, 1.5]),                         # furniture in the corridor
    lambda layout: layout['furniture'][0].update(x=[.42, 1.8]),                        # off the 0.05 m grid
    lambda layout: layout.update(world_name='office'),                                 # breaks the truth reader
    lambda layout: layout.update(map_to_world=[0., 0.]),                               # breaks the truth offset
])
def test_layout_mistakes_are_refused(change):
    layout = copy.deepcopy(office_world.load_layout(CORRIDOR_LAYOUT))
    change(layout)
    with pytest.raises(ValueError):
        office_world.validate_layout(layout)


@pytest.mark.parametrize('change', [
    lambda layout: layout['walls'][0].update(to=[15., 1.5]),                           # diagonal wall
    lambda layout: layout['walls'][0].update(doors=[[3.4, 4.2]]),                      # door narrower than 0.9 m
    lambda layout: layout['walls'][0].update(doors=[[14.5, 15.5]]),                    # door past the wall's end
    lambda layout: layout['walls'][0].update(doors=[[7.6, 9.6], [3.4, 8.0]]),          # overlapping doors
    lambda layout: layout['walls'][0].update(to=[16., .95]),                           # wall outside the interior
    lambda layout: layout['furniture'][0].update(y=[3.6, 4.2], x=[1.0, 2.0]),          # furniture through a wall
    lambda layout: layout['furniture'][0].update(x=[-3.5, -2.5]),                      # outside the interior
    lambda layout: layout['furniture'][1].update(name=layout['furniture'][0]['name']),  # duplicate name
    lambda layout: layout['walls'][2].update({'from': [1.52, .95], 'to': [1.52, 6.]}),  # off the 0.05 m grid
    lambda layout: layout['furniture'][0].update(color='purple'),                      # unknown colour
    lambda layout: layout.update(world_name='office'),                                 # breaks the truth reader
    lambda layout: layout.update(map_to_world=[-2., 0.]),                              # breaks the truth offset
    lambda layout: layout['walls'][2].update(to=[1.5, .95]),                           # wall with no length
    lambda layout: layout['walls'][7].update(doors=[[-6., -3.5]]),                     # wall that is all door
    lambda layout: layout['walls'][0].update(doors=[[3.4, 4.6], [4.6, 5.6]]),          # doors touching
    lambda layout: layout['furniture'][0].update(height=.1),                           # below the LiDAR
    lambda layout: layout.update(doors_x=[[0., 1.]]),                                  # both layout shapes
    lambda layout: layout['furniture'].append(dict(layout['furniture'][0], name='crate', x=[3.5, 4.5],
                                                   y=[1.0, 1.4])),                     # in the conference door
    lambda layout: layout['furniture'].append(dict(layout['furniture'][0], name='crate', x=[9.6, 10.4],
                                                   y=[-1.6, -1.0])),                   # inside the team office door
    lambda layout: layout['walls'].append({'name': 'extra', 'from': [3.0, .95], 'to': [5.0, .95]}),  # over a door
    lambda layout: layout['walls'][1].update(doors=[[.4, 2.4], [5., 6.6], [8.5, 9.4], [12.5, 13.5]]),  # door edge
    lambda layout: layout['furniture'].append(dict(layout['furniture'][0], name='crate', x=[7.0, 7.5],
                                                   y=[-.85, .85])),                    # corridor blocked
])
def test_proposed_system_layout_mistakes_are_refused(change):
    layout = copy.deepcopy(office_world.load_layout(LAYOUTS['proposed_system']))
    change(layout)
    with pytest.raises(ValueError):
        office_world.validate_layout(layout)


def test_a_blocked_corridor_is_reported_by_room():
    layout = copy.deepcopy(office_world.load_layout(LAYOUTS['proposed_system']))
    layout['furniture'].append(dict(layout['furniture'][0], name='crate', x=[7.0, 7.5], y=[-.85, .85]))
    with pytest.raises(ValueError, match='cannot reach demo_area'):
        office_world.validate_layout(layout)


def _shifted_office_v1(layout):
    layout['interior']['x'] = [3.5, 13.5]            # 5 m east: map (0, 0) is now outside the office
    layout['doors_x'] = [[a + 5, b + 5] for a, b in layout['doors_x']]
    layout['partitions_x'] = [x + 5 for x in layout['partitions_x']]
    for item in layout['rooms'] + layout['furniture']:
        item['x'] = [v + 5 for v in item['x']]


def _crate(layout, x, y):
    layout['furniture'].append(dict(layout['furniture'][0], name='crate', x=x, y=y))


@pytest.mark.parametrize('office, change, message', [
    ('office_v1', _shifted_office_v1, 'spawn point'),
    ('office_v1', lambda layout: _crate(layout, [-1.5, 1.95], [1.2, 1.5]), 'cannot reach reception'),
    ('proposed_system', lambda layout: _crate(layout, [1.55, 6.45], [1.5, 1.8]), 'cannot reach conference_hall'),
    ('proposed_system', lambda layout: _crate(layout, [-2.5, -0.05], [-6.0, -4.05]), 'cannot reach storage'),
])
def test_rooms_sealed_beyond_their_door_or_outside_reach_are_refused(office, change, message):
    layout = copy.deepcopy(office_world.load_layout(LAYOUTS[office]))
    change(layout)                                   # a barrier inside a door, or a room full of furniture
    with pytest.raises(ValueError, match=message):
        office_world.validate_layout(layout)


def test_furniture_flush_against_a_corridor_wall_is_accepted():
    layout = copy.deepcopy(office_world.load_layout(LAYOUTS['proposed_system']))
    layout['furniture'].append(dict(layout['furniture'][0], name='bench', x=[3.0, 4.0], y=[-.9, -.6]))
    office_world.validate_layout(layout)       # touches the wall face at y = -0.9 exactly; no overlap


def test_layout_file_must_carry_its_own_name(tmp_path):
    copied = tmp_path / 'office_v3_layout.yaml'
    copied.write_text(LAYOUTS['proposed_system'].read_text())        # still says name: proposed_system
    with pytest.raises(ValueError, match='after the name inside it'):
        office_world.load_layout(copied)
    assert office_world.main(['check', '--layout', str(copied)]) == 1   # only worlds/*_layout.yaml


def test_compare_needs_the_exact_map_named():
    with pytest.raises(SystemExit):
        office_world.main(['compare', str(PKG / 'maps/proposed_system.yaml')])


def test_proposed_system_floors_do_not_flicker_and_every_room_has_its_colour():
    tiles = {tile.name: tile for tile in office_world.floor_tiles(office_world.load_layout(LAYOUTS['proposed_system']))}
    assert tiles['floor_storage'].height == .003                   # drawn above the open office around it
    rooms = [tile for name, tile in tiles.items() if name != 'floor_corridor']
    assert all(tile.height == .002 for tile in rooms if tile.name != 'floor_storage')
    assert len({tile.color for tile in rooms}) == len(rooms) == 8
