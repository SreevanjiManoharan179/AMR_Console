#!/usr/bin/env python3
"""Build office Gazebo worlds and their exact maps from layout files.

Offline only: no ROS, no simulator, no network. From the repository root (amr-console-dev):

  python3 src/amr_physical_ai/scripts/office_world.py generate [--layout FILE]
      write worlds/<name>.sdf.xacro, maps/<name>.pgm and maps/<name>.yaml for every
      worlds/*_layout.yaml (or only FILE)
  python3 src/amr_physical_ai/scripts/office_world.py check [--layout FILE]
      regenerate in memory; exit 1 if any file on disk differs from its layout
  python3 src/amr_physical_ai/scripts/office_world.py compare <saved SLAM map .yaml> --exact MAP
      compare a map saved in the console with the exact map of its office (JSON on stdout)

Two layout shapes are accepted. The compact corridor shape (an earlier office) has
one corridor, doors along both corridor walls and room partitions. proposed_system
lists its walls explicitly: each wall is a line, centred on it, with door gaps.
Both shapes must keep every room reachable from the spawn point; the walls shape also
keeps 0.5 m clear on both sides of every door.

The world and the map are made from the same boxes, so the map is exact by
construction: a cell is occupied when it overlaps a wall or furniture box,
unknown outside the outer walls, and free everywhere else. Floor colours are
visual only (2 or 3 mm high, no collision) and never reach the map or the LiDAR,
whose rays are horizontal at about 0.17 m.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from xml.sax.saxutils import quoteattr

import numpy as np
import yaml

PKG = Path(__file__).resolve().parents[1]
LAYOUT = PKG / 'worlds/proposed_system_layout.yaml'
FREE, UNKNOWN, OCCUPIED = 254, 205, 0
COLORS = {'wall': (0.93, 0.92, 0.89), 'wood': (0.55, 0.38, 0.22), 'steel': (0.55, 0.58, 0.62),
          'fabric': (0.30, 0.40, 0.55), 'white': (0.92, 0.92, 0.92), 'dark': (0.20, 0.20, 0.22),
          'green': (0.25, 0.50, 0.30)}
FLOOR_COLORS = [(0.80, 0.86, 0.93), (0.93, 0.90, 0.78), (0.82, 0.91, 0.80),
                (0.84, 0.84, 0.90), (0.90, 0.84, 0.91), (0.92, 0.88, 0.82),
                (0.80, 0.91, 0.91), (0.95, 0.85, 0.83)]
CORRIDOR_COLOR = (0.86, 0.86, 0.86)
MIN_FURNITURE_HEIGHT = .20   # the LiDAR's rays are horizontal at about 0.17 m
ROBOT_CLEARANCE = .27        # nav2_mppi.yaml robot_radius 0.22 m plus one 0.05 m cell
DOOR_KEEP_CLEAR = .50        # m kept clear on both sides of every door (walls shape)


def layouts():
    """Every office layout in the package, sorted by name."""
    return sorted((PKG / 'worlds').glob('*_layout.yaml'))


@dataclass(frozen=True)
class Box:
    name: str
    kind: str          # wall, furniture or floor (floor is visual only)
    x0: float
    x1: float
    y0: float
    y1: float
    height: float
    color: tuple


def load_layout(path=LAYOUT):
    """Read and check one layout. Its file is called <name>_layout.yaml after the name inside it,
    because the generated world and map files are named after it."""
    path = Path(path)
    layout = yaml.safe_load(path.read_text())
    if not isinstance(layout, dict) or path.name != f"{layout.get('name')}_layout.yaml":
        raise ValueError(f'{path.name} must be called <name>_layout.yaml after the name inside it.')
    validate_layout(layout)
    return layout


def _cells(value, resolution):
    count = round(value / resolution)
    if abs(count * resolution - value) > 1e-9:
        raise ValueError(f'{value} m is not a multiple of the {resolution} m map resolution.')
    return count


def _cell_rect(x0, x1, y0, y1, resolution):
    """A rectangle in whole map cells, so faces that touch compare exactly."""
    return tuple(_cells(v, resolution) for v in (x0, x1, y0, y1))


def _overlap(p, q):
    """True when two cell rectangles (x0, x1, y0, y1) share some area."""
    return p[0] < q[1] and q[0] < p[1] and p[2] < q[3] and q[2] < p[3]


def _origin_cells(layout):
    """The exact map's origin in whole cells: outside the outer walls by the margin."""
    res = float(layout['resolution'])
    t, margin = float(layout['wall_thickness']), float(layout['map_margin'])
    return (_cells(layout['interior']['x'][0] - t - margin, res),
            _cells(layout['interior']['y'][0] - t - margin, res))


def _check_common(layout):
    if layout['world_name'] != 'default':
        raise ValueError('The ground-truth reader needs the Gazebo world to be called "default".')
    if [float(v) for v in layout['map_to_world']] != [-2.0, -0.5]:
        raise ValueError('The ground-truth reader and AMCL assume map (0, 0) = world (-2.0, -0.5), the spawn point.')
    for item in layout['furniture']:
        if item['color'] not in COLORS:
            raise ValueError(f"{item['name']} has an unknown colour.")
        if float(item['height']) < MIN_FURNITURE_HEIGHT:
            raise ValueError(f"{item['name']} must be at least {MIN_FURNITURE_HEIGHT} m tall: the exact map "
                             "would show it, but the LiDAR (about 0.17 m) would not.")
    names = [box.name for box in boxes(layout) + floor_tiles(layout)]
    if len(set(names)) != len(names):
        raise ValueError('Every wall, furniture item and floor tile needs a unique name.')
    for box in boxes(layout) + floor_tiles(layout):
        for value in (box.x0, box.x1, box.y0, box.y1):
            _cells(value, float(layout['resolution']))
    _check_reachable(layout)


def _check_reachable(layout):
    """The robot reaches every room and corridor from the spawn point; no floor it fits on is cut off."""
    from scipy.ndimage import distance_transform_edt, label
    res = float(layout['resolution'])
    image, _ = exact_map(layout)
    free = np.flipud(image) == FREE                       # row 0 = lowest y
    clearance = distance_transform_edt(np.pad(free, 1, constant_values=False))[1:-1, 1:-1] * res
    parts, _ = label(clearance >= ROBOT_CLEARANCE)       # 4-connected areas the robot fits in
    i0, j0 = _origin_cells(layout)
    row, col = -j0, -i0                                  # the cell of map (0, 0)
    inside = 0 <= row < parts.shape[0] and 0 <= col < parts.shape[1]
    spawn = parts[row, col] if inside else 0
    if not spawn:
        raise ValueError('The spawn point, map (0, 0), needs room for the robot inside the office.')
    # Each tile minus a strip along its edges, so that a doorway alone never counts as the room.
    edge = float(layout['wall_thickness']) / 2 + DOOR_KEEP_CLEAR
    for tile in floor_tiles(layout):
        place = tile.name.removeprefix('floor_')
        a0, a1, b0, b1 = _cell_rect(tile.x0 + edge, tile.x1 - edge, tile.y0 + edge, tile.y1 - edge, res)
        if a0 >= a1 or b0 >= b1:
            raise ValueError(f'{place} is too small to drive into.')
        if not np.any(parts[b0 - j0:b1 - j0, a0 - i0:a1 - i0] == spawn):
            raise ValueError(f'The robot cannot reach {place} from the spawn point.')
    if np.any((parts > 0) & (parts != spawn)):
        raise ValueError('Part of the floor the robot fits on cannot be reached from the spawn point.')


def _wall_segments(wall):
    """Axis-aligned wall line -> (axis, fixed coordinate, [(start, end), ...]) with door gaps removed."""
    (x0, y0), (x1, y1) = wall['from'], wall['to']
    if x0 != x1 and y0 != y1:
        raise ValueError(f"Wall {wall['name']} must be horizontal or vertical.")
    if (x0, y0) == (x1, y1):
        raise ValueError(f"Wall {wall['name']} has no length.")
    axis = 'x' if y0 == y1 else 'y'
    fixed = y0 if axis == 'x' else x0
    start, end = sorted((x0, x1) if axis == 'x' else (y0, y1))
    edges, previous = [start], None
    for a, b in sorted(tuple(door) for door in wall.get('doors', [])):
        # A door may start at the wall's end; two doors need some wall between them.
        if not ((start <= a if previous is None else previous < a) and a < b <= end) or b - a < .9:
            raise ValueError(f"Door {a}..{b} in wall {wall['name']} must be at least 0.9 m wide, on the wall "
                             "and apart from its other doors.")
        edges += [a, b]
        previous = b
    edges.append(end)
    segments = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2) if edges[i + 1] > edges[i]]
    if not segments:
        raise ValueError(f"Wall {wall['name']} is all door; leave it out instead.")
    return axis, fixed, segments


def validate_layout(layout):
    if 'walls' in layout:
        mixed = [key for key in ('corridor', 'doors_x', 'partitions_x') if key in layout]
        if mixed:
            raise ValueError('A layout lists its walls or uses the corridor shape, not both: ' + ', '.join(mixed) + '.')
        res, t = float(layout['resolution']), float(layout['wall_thickness'])
        (x_min, x_max), (y_min, y_max) = layout['interior']['x'], layout['interior']['y']
        for wall in layout['walls']:
            _wall_segments(wall)
            (a, b), (c, d) = sorted([wall['from'][0], wall['to'][0]]), sorted([wall['from'][1], wall['to'][1]])
            if not (x_min <= a <= b <= x_max and y_min <= c <= d <= y_max):
                raise ValueError(f"Wall {wall['name']} must lie inside the interior.")
        solid = [(w.name, _cell_rect(w.x0, w.x1, w.y0, w.y1, res)) for w in walls(layout)]
        items = []
        for item in layout['furniture']:
            (a, b), (c, d) = item['x'], item['y']
            if not (x_min <= a < b <= x_max and y_min <= c < d <= y_max):
                raise ValueError(f"{item['name']} must stand inside the interior.")
            rect = _cell_rect(a, b, c, d, res)
            if any(_overlap(rect, w) for _, w in solid):
                raise ValueError(f"{item['name']} overlaps a wall.")
            items.append((item['name'], rect))
        reach = t / 2 + DOOR_KEEP_CLEAR
        for wall in layout['walls']:
            axis, fixed, _ = _wall_segments(wall)
            for a, b in wall.get('doors', []):
                zone = (a, b, fixed - reach, fixed + reach) if axis == 'x' else (fixed - reach, fixed + reach, a, b)
                zone = _cell_rect(*zone, res)
                for name, rect in solid + items:
                    if _overlap(zone, rect):
                        raise ValueError(f"{name} blocks door {a}..{b} in wall {wall['name']}: keep "
                                         f"{DOOR_KEEP_CLEAR} m clear on both sides of every door.")
        _check_common(layout)
        return
    resolution = float(layout['resolution'])
    thickness = float(layout['wall_thickness'])
    (x_min, x_max), (y_min, y_max) = layout['interior']['x'], layout['interior']['y']
    c_min, c_max = layout['corridor']['y']
    if not (x_min < x_max and y_min < c_min < c_max < y_max):
        raise ValueError('The corridor must lie inside the interior.')
    doors = sorted(tuple(d) for d in layout['doors_x'])
    previous = x_min
    for a, b in doors:
        if not (previous < a < b < x_max) or b - a < .9:
            raise ValueError(f'Door {a}..{b} must be at least 0.9 m wide, ordered and inside the interior.')
        previous = b
    for px in layout['partitions_x']:
        if not x_min < px < x_max or any(a - thickness / 2 < px < b + thickness / 2 for a, b in doors):
            raise ValueError(f'Partition at x={px} must be inside the interior and clear of every door.')
    for item in layout['furniture']:
        (a, b), (c, d) = item['x'], item['y']
        if not (x_min <= a < b <= x_max and y_min <= c < d <= y_max):
            raise ValueError(f"{item['name']} must stand inside the interior.")
        if c < c_max + thickness and d > c_min - thickness:
            raise ValueError(f"{item['name']} must stay out of the corridor.")
    _check_common(layout)


def _outer_walls(layout):
    t, h = float(layout['wall_thickness']), float(layout['wall_height'])
    (x_min, x_max), (y_min, y_max) = layout['interior']['x'], layout['interior']['y']
    wall = COLORS['wall']
    return [Box('wall_west', 'wall', x_min - t, x_min, y_min - t, y_max + t, h, wall),
            Box('wall_east', 'wall', x_max, x_max + t, y_min - t, y_max + t, h, wall),
            Box('wall_south', 'wall', x_min, x_max, y_min - t, y_min, h, wall),
            Box('wall_north', 'wall', x_min, x_max, y_max, y_max + t, h, wall)]


def _furniture(layout):
    return [Box(item['name'], 'furniture', *item['x'], *item['y'], float(item['height']), COLORS[item['color']])
            for item in layout['furniture']]


def walls(layout):
    """Outer and inner walls in the map frame."""
    t, h = float(layout['wall_thickness']), float(layout['wall_height'])
    wall = COLORS['wall']
    result = _outer_walls(layout)
    if 'walls' in layout:              # proposed_system shape: explicit wall lines, centred
        for item in layout['walls']:
            axis, fixed, segments = _wall_segments(item)
            for k, (a, b) in enumerate(segments):
                name = item['name'] if len(segments) == 1 else f"{item['name']}_{k}"
                if axis == 'x':
                    result.append(Box(name, 'wall', a, b, fixed - t / 2, fixed + t / 2, h, wall))
                else:
                    result.append(Box(name, 'wall', fixed - t / 2, fixed + t / 2, a, b, h, wall))
        return result
    (x_min, x_max), (y_min, y_max) = layout['interior']['x'], layout['interior']['y']
    c_min, c_max = layout['corridor']['y']
    edges = [x_min] + [v for door in sorted(layout['doors_x']) for v in door] + [x_max]
    segments = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2)]
    for side, (y0, y1) in (('north', (c_max, c_max + t)), ('south', (c_min - t, c_min))):
        for k, (a, b) in enumerate(segments):
            if b > a:
                result.append(Box(f'corridor_{side}_{k}', 'wall', a, b, y0, y1, h, wall))
    for k, px in enumerate(layout['partitions_x']):
        result.append(Box(f'partition_north_{k}', 'wall', px - t / 2, px + t / 2, c_max + t, y_max, h, wall))
        result.append(Box(f'partition_south_{k}', 'wall', px - t / 2, px + t / 2, y_min, c_min - t, h, wall))
    return result


def boxes(layout):
    """Walls and furniture in the map frame; these make both the world and the map."""
    return walls(layout) + _furniture(layout)


def floor_tiles(layout):
    """Visual-only floor colours, one per room and one per corridor."""
    if 'walls' in layout:
        tiles = [Box(f"floor_{c['label'].replace(' ', '_')}", 'floor', *c['x'], *c['y'], .002, CORRIDOR_COLOR)
                 for c in layout.get('corridors', [])]
    else:
        (x_min, x_max) = layout['interior']['x']
        c_min, c_max = layout['corridor']['y']
        tiles = [Box('floor_corridor', 'floor', x_min, x_max, c_min, c_max, .002, CORRIDOR_COLOR)]
    for k, room in enumerate(layout['rooms']):
        label = room['label'].split(' (')[0].replace(' ', '_')
        # A room inside another room (proposed_system's storage) is drawn 1 mm higher, so the two
        # floors never flicker in Gazebo.
        nested = any(other is not room and _inside(room, other) for other in layout['rooms'])
        tiles.append(Box(f'floor_{label}', 'floor', *room['x'], *room['y'], .003 if nested else .002,
                         FLOOR_COLORS[k % len(FLOOR_COLORS)]))
    return tiles


def _inside(inner, outer):
    return (outer['x'][0] <= inner['x'][0] and inner['x'][1] <= outer['x'][1]
            and outer['y'][0] <= inner['y'][0] and inner['y'][1] <= outer['y'][1])


def exact_map(layout):
    """Return (image rows top to bottom as uint8, map YAML text)."""
    res = float(layout['resolution'])
    t, margin = float(layout['wall_thickness']), float(layout['map_margin'])
    (x_min, x_max), (y_min, y_max) = layout['interior']['x'], layout['interior']['y']
    ox, oy = x_min - t - margin, y_min - t - margin
    i0, j0 = _origin_cells(layout)
    width = _cells(x_max + t + margin, res) - i0
    height = _cells(y_max + t + margin, res) - j0
    grid = np.full((height, width), UNKNOWN, dtype=np.uint8)   # row 0 = lowest y

    def span(a, b, base):
        return _cells(a, res) - base, _cells(b, res) - base

    (a0, a1), (b0, b1) = span(x_min, x_max, i0), span(y_min, y_max, j0)
    grid[b0:b1, a0:a1] = FREE
    for box in boxes(layout):
        (a0, a1), (b0, b1) = span(box.x0, box.x1, i0), span(box.y0, box.y1, j0)
        grid[b0:b1, a0:a1] = OCCUPIED
    meta = (f"image: {layout['name']}.pgm\nmode: trinary\nresolution: {res:.3f}\n"
            f"origin: [{ox:.3f}, {oy:.3f}, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n")
    return np.flipud(grid), meta


def pgm_bytes(image):
    height, width = image.shape
    return f'P5\n{width} {height}\n255\n'.encode() + image.astype(np.uint8).tobytes()


def _fmt(value):
    text = f'{value:.4f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def _element(box, collision):
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    sx, sy = box.x1 - box.x0, box.y1 - box.y0
    pose = f'<pose>{_fmt(cx)} {_fmt(cy)} {_fmt(box.height / 2)} 0 0 0</pose>'
    geometry = f'<geometry><box><size>{_fmt(sx)} {_fmt(sy)} {_fmt(box.height)}</size></box></geometry>'
    lines = [f'        <visual name={quoteattr(box.name + "_visual")}>{pose}{geometry}'
             '<material><ambient>{0}</ambient><diffuse>{0}</diffuse><specular>0.1 0.1 0.1 1</specular>'
             '</material></visual>'.format(' '.join(_fmt(c) for c in box.color) + ' 1')]
    if collision:
        lines.insert(0, f'        <collision name={quoteattr(box.name + "_collision")}>{pose}{geometry}</collision>')
    return lines


def world_xacro(layout):
    ox, oy = layout['map_to_world']
    solid = [line for box in boxes(layout) for line in _element(box, True)]
    floor = [line for box in floor_tiles(layout) for line in _element(box, False)]
    return '\n'.join([
        '<?xml version="1.0"?>',
        f'<!-- Generated by src/amr_physical_ai/scripts/office_world.py from worlds/{layout["name"]}_layout.yaml.',
        '     Do not edit by hand: change the layout, then run "office_world.py generate".',
        '     Systems, light, ground and physics match nav2_minimal_tb3_sim/worlds/tb3_sandbox.sdf.xacro. -->',
        '<sdf version="1.6" xmlns:xacro="http://www.ros.org/wiki/xacro">',
        '  <xacro:arg name="headless" default="true"/>',
        f'  <world name={quoteattr(layout["world_name"])}>',
        '    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>',
        '    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>',
        '    <xacro:unless value="$(arg headless)">',
        '      <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>',
        '    </xacro:unless>',
        '    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">',
        '      <render_engine>ogre2</render_engine>',
        '    </plugin>',
        '    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>',
        '    <light name="sun" type="directional">',
        '      <cast_shadows>0</cast_shadows>',
        '      <pose>0 0 10 0 0 0</pose>',
        '      <diffuse>0.8 0.8 0.8 1</diffuse>',
        '      <specular>0.8 0.8 0.8 1</specular>',
        '      <attenuation><range>1000</range><constant>0.9</constant><linear>0.01</linear>'
        '<quadratic>0.001</quadratic></attenuation>',
        '      <direction>-0.5 0.1 -0.9</direction>',
        '    </light>',
        '    <model name="ground_plane">',
        '      <static>1</static>',
        '      <link name="link">',
        '        <collision name="collision"><geometry><plane><normal>0 0 1</normal><size>100 100</size>'
        '</plane></geometry><max_contacts>10</max_contacts></collision>',
        '        <visual name="visual"><geometry><plane><normal>0 0 1</normal><size>100 100</size></plane>'
        '</geometry><material><ambient>0.8 0.8 0.8 1</ambient><diffuse>0.8 0.8 0.8 1</diffuse>'
        '<specular>0.8 0.8 0.8 1</specular></material></visual>',
        '      </link>',
        '    </model>',
        '    <scene>',
        '      <shadows>0</shadows>',
        '    </scene>',
        '    <physics name="3ms" type="ode">',
        '      <max_step_size>0.003</max_step_size>',
        '      <real_time_factor>1</real_time_factor>',
        '    </physics>',
        f'    <model name={quoteattr(layout["name"])}>',
        '      <static>1</static>',
        f'      <pose>{_fmt(ox)} {_fmt(oy)} 0 0 0 0</pose>',
        '      <link name="structure">',
        *solid,
        *floor,
        '      </link>',
        '    </model>',
        '  </world>',
        '</sdf>',
        ''])


def outputs(layout):
    image, meta = exact_map(layout)
    name = layout['name']
    return {PKG / f'worlds/{name}.sdf.xacro': world_xacro(layout).encode(),
            PKG / f'maps/{name}.pgm': pgm_bytes(image),
            PKG / f'maps/{name}.yaml': meta.encode()}


def read_map(path):
    """Occupancy of a saved trinary map: (occupied, free, resolution, origin); row 0 = lowest y."""
    path = Path(path)
    meta = yaml.safe_load(path.read_text())
    raw = (path.parent / meta['image']).read_bytes()
    header, offset = [], 0
    while len(header) < 4:
        end = raw.index(b'\n', offset)
        line = raw[offset:end].split(b'#', 1)[0]
        header.extend(line.split())
        offset = end + 1
    if header[0] != b'P5' or int(header[3]) != 255:
        raise ValueError(f'{path.name}: only 8-bit binary PGM maps are supported.')
    width, height = int(header[1]), int(header[2])
    pixels = np.frombuffer(raw[offset:offset + width * height], dtype=np.uint8).reshape(height, width)
    occupancy = (255 - pixels.astype(float)) / 255 if not meta.get('negate', 0) else pixels / 255
    occupied = np.flipud(occupancy > float(meta['occupied_thresh']))
    free = np.flipud(occupancy < float(meta['free_thresh']))
    return occupied, free, float(meta['resolution']), [float(v) for v in meta['origin']]


def _distance_field(occupied, resolution):
    from scipy.ndimage import distance_transform_edt
    return distance_transform_edt(~occupied) * resolution


def _sample(field, resolution, origin, xs, ys):
    cols = np.floor((xs - origin[0]) / resolution).astype(int)
    rows = np.floor((ys - origin[1]) / resolution).astype(int)
    inside = (cols >= 0) & (cols < field.shape[1]) & (rows >= 0) & (rows < field.shape[0])
    out = np.full(xs.shape, np.inf)
    out[inside] = field[rows[inside], cols[inside]]
    return out


def _summary(distances, tolerance):
    finite = distances[np.isfinite(distances)]
    return {'cells': int(distances.size),
            'within_tolerance': round(float(np.mean(distances <= tolerance + 1e-9)), 4) if distances.size else None,
            'median_m': round(float(np.median(finite)), 3) if finite.size else None,
            'p95_m': round(float(np.percentile(finite, 95)), 3) if finite.size else None,
            'outside_other_map': int(np.sum(~np.isfinite(distances)))}


def compare_layers(s_occ, s_res, s_origin, e_occ, e_free, e_res, e_origin, tolerance=.10):
    """Agreement of SLAM occupancy with the exact map, from arrays (row 0 at the lowest y, origin x, y first)."""
    padded = np.pad(e_free, 1, constant_values=False)
    near_free = (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2] | padded[1:-1, 2:])
    surface = e_occ & near_free          # wall and furniture faces the LiDAR can see

    def centres(mask, res, origin):
        rows, cols = np.nonzero(mask)
        return origin[0] + (cols + .5) * res, origin[1] + (rows + .5) * res

    ex, ey = centres(surface, e_res, e_origin)
    sx, sy = centres(s_occ, s_res, s_origin)
    exact_to_slam = _sample(_distance_field(s_occ, s_res), s_res, s_origin, ex, ey)
    slam_to_exact = _sample(_distance_field(e_occ, e_res), e_res, e_origin, sx, sy)
    return {'tolerance_m': tolerance,
            'exact_faces_seen_by_slam': _summary(exact_to_slam, tolerance),
            'slam_occupied_on_exact_walls': _summary(slam_to_exact, tolerance)}


def compare_maps(slam_yaml, exact_yaml, tolerance=.10):
    """Agreement of a saved SLAM map with the exact map, both in the map frame."""
    s_occ, _, s_res, s_origin = read_map(slam_yaml)
    e_occ, e_free, e_res, e_origin = read_map(exact_yaml)
    if any(abs(v) > 1e-9 for v in (s_origin[2], e_origin[2])):
        raise ValueError('Rotated map origins are not supported by this check.')
    return {'slam_map': str(slam_yaml), 'exact_map': str(exact_yaml),
            **compare_layers(s_occ, s_res, s_origin, e_occ, e_free, e_res, e_origin, tolerance)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('generate', 'check'):
        sub.add_parser(name).add_argument('--layout', help='one layout file (default: every worlds/*_layout.yaml)')
    compare = sub.add_parser('compare')
    compare.add_argument('slam_yaml')
    compare.add_argument('--exact', required=True,
                         help='the exact map of the office the SLAM map was made in, '
                              'e.g. src/amr_physical_ai/maps/proposed_system.yaml')
    compare.add_argument('--tolerance', type=float, default=.10)
    args = parser.parse_args(argv)
    if args.command == 'compare':
        print(json.dumps(compare_maps(args.slam_yaml, args.exact, args.tolerance), indent=2))
        return 0
    chosen = [Path(args.layout)] if args.layout else layouts()
    if args.layout and Path(args.layout).resolve() not in [path.resolve() for path in layouts()]:
        print('--layout must be one of src/amr_physical_ai/worlds/*_layout.yaml.', file=sys.stderr)
        return 1
    files = {}
    for path in chosen:
        for target, data in outputs(load_layout(path)).items():
            if target in files:      # load_layout already ties each name to its file; belt and braces
                print(f'Two layouts would write {target.name}.', file=sys.stderr)
                return 1
            files[target] = data
    if args.command == 'generate':
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f'wrote {path.relative_to(PKG.parents[1])}')
        return 0
    stale = [str(path.relative_to(PKG.parents[1])) for path, data in files.items()
             if not path.is_file() or path.read_bytes() != data]
    if stale:
        print('Out of date with the layout: ' + ', '.join(stale), file=sys.stderr)
        return 1
    print('Office worlds and maps match their layouts: ' + ', '.join(sorted(
        path.stem.removesuffix('_layout') for path in chosen)) + '.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
