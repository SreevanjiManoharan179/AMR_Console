"""Proposed semantic tags and conservative static-map validation; no ROS."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

import yaml

from src.amr_physical_ai.amr_physical_ai.command_pipeline import Grid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TAGS = ROOT / 'config/tags_proposed_system.yaml'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Destination:
    key: str
    room: str
    x: float
    y: float
    yaw: float
    person: str | None = None

    @property
    def pose(self):
        return {'x': self.x, 'y': self.y, 'yaw': self.yaw}


class Tags:
    def __init__(self, path=DEFAULT_TAGS):
        self.path = Path(path).resolve()
        self.data = yaml.safe_load(self.path.read_text())
        self.map_path = (self.path.parent / self.data['map_yaml']).resolve()
        self.layout_path = (self.path.parent / self.data['layout_yaml']).resolve()
        self.rooms = self._index('rooms', 'name')
        self.seats = self._index('seats', 'id')
        self.people = self._index('people', 'name')
        self.objects = self._index('objects', 'name')
        if self.data['frame_id'] != 'map' or self.data['map_id'] != 'proposed_system':
            raise ValueError('Only the proposed_system map frame is supported')
        if self.data['approval_status'] not in ('proposed', 'confirmed'):
            raise ValueError('Invalid approval status')
        if self.data['minimum_clearance_m'] < .52:
            raise ValueError('Delivery tags require at least 0.52 m endpoint clearance')
        self.grid = Grid.from_map(self.map_path)
        for person in self.people.values():
            if person['seat'] not in self.seats:
                raise ValueError('Person references an unknown seat')
        if len({p['seat'] for p in self.people.values()}) != len(self.people):
            raise ValueError('The fictional roster requires distinct seats')
        for item in list(self.seats.values()) + list(self.objects.values()):
            if item['room'] not in self.rooms:
                raise ValueError('Unknown room')
        for item in self.pose_items().values():
            if not all(math.isfinite(float(item[k])) for k in ('x', 'y', 'yaw')):
                raise ValueError('Nonfinite tag pose')
        self.sha256 = digest(self.path)

    def _index(self, field, key):
        entries = self.data[field]
        result = {e[key]: e for e in entries}
        if len(result) != len(entries):
            raise ValueError(f'Duplicate {field}')
        return result

    def pose_items(self):
        return {**{f'room:{k}': v for k, v in self.rooms.items()},
                **{f'seat:{k}': v for k, v in self.seats.items()},
                **{f'object:{k}': v for k, v in self.objects.items()}}

    def semantic_digest(self):
        data = dict(self.data)
        data.pop('approval_status')
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def fingerprints(self):
        image = self.map_path.parent / yaml.safe_load(self.map_path.read_text())['image']
        return {'tags': digest(self.path), 'map_yaml': digest(self.map_path),
                'map_image': digest(image), 'layout': digest(self.layout_path)}

    def destination(self, kind, key):
        person = None
        if kind in ('person', 'role'):
            person = key
            kind, key = 'seat', self.people[key]['seat']
        items = {'room': self.rooms, 'seat': self.seats, 'object': self.objects}[kind]
        item = items[key]
        return Destination(f'{kind}:{key}', item.get('room', key),
                           *(float(item[k]) for k in ('x', 'y', 'yaw')), person=person)

    def occupant(self, seat):
        return next((name for name, p in self.people.items() if p['seat'] == seat), None)

    def validate(self):
        """Office-world four-neighbour flood fill; endpoints additionally require 0.52 m."""
        if self.grid.origin[2] != 0:
            raise ValueError('This office reachability check requires an unrotated map')
        def cell(x, y):
            return (math.floor((y - self.grid.origin[1]) / self.grid.resolution),
                    math.floor((x - self.grid.origin[0]) / self.grid.resolution))
        home = self.rooms['home']
        self.grid.validate_point(home['x'], home['y'], .52)
        start = cell(home['x'], home['y'])
        passable = self.grid.clearance >= .22 + self.grid.resolution
        seen, queue = {start}, deque([start])
        while queue:
            r, c = queue.popleft()
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = r + dr, c + dc
                if (0 <= nxt[0] < passable.shape[0] and 0 <= nxt[1] < passable.shape[1]
                        and passable[nxt] and nxt not in seen):
                    seen.add(nxt)
                    queue.append(nxt)
        evidence = {}
        for key, item in self.pose_items().items():
            point = self.grid.validate_point(item['x'], item['y'], .52)
            if cell(item['x'], item['y']) not in seen:
                raise ValueError(f'{key} is not reachable from home')
            evidence[key] = dict(point, reachable=True)
        layout = yaml.safe_load(self.layout_path.read_text())
        source_rooms = yaml.safe_load((self.layout_path.parents[1] /
            'config/named_locations_proposed_system.yaml').read_text())['locations']
        if list(self.rooms.values()) != source_rooms:
            raise ValueError('Room tags must preserve the proposed_system place poses and aliases')
        desks = {f['name']: f for f in layout['furniture']
                 if f['name'].startswith(('desks_', 'team_desk_', 'manager_desk'))}
        if {s['desk'] for s in self.seats.values()} != set(desks):
            raise ValueError('Every desk must have at least one seat annotation')
        for seat in self.seats.values():
            desk = desks[seat['desk']]
            tx, ty = seat['target_xy']
            if not (desk['x'][0] <= tx <= desk['x'][1] and desk['y'][0] <= ty <= desk['y'][1]):
                raise ValueError('Seat target must lie on its desk')
            edge = seat['aisle_edge']
            edge_point = {'north': (seat['x'], desk['y'][1]), 'east': (desk['x'][1], seat['y']),
                          'west': (desk['x'][0], seat['y'])}[edge]
            if not (desk['x'][0] <= edge_point[0] <= desk['x'][1]
                    and desk['y'][0] <= edge_point[1] <= desk['y'][1]):
                raise ValueError('Serving pose must project onto the aisle edge')
            offset = math.hypot(seat['x'] - edge_point[0], seat['y'] - edge_point[1])
            if not .35 - 1e-9 <= offset <= .60 + 1e-9:
                raise ValueError('Serving offset outside 0.35–0.60 m')
            facing = math.atan2(ty - seat['y'], tx - seat['x'])
            if abs(math.atan2(math.sin(seat['yaw'] - facing), math.cos(seat['yaw'] - facing))) > 1e-5:
                raise ValueError('Serving yaw must face the tagged desk')
        for room in {s['room'] for s in self.seats.values()}:
            seats = [s for s in self.seats.values() if s['room'] == room]
            depths = sorted({round(-sum(desks[s['desk']]['y']) / 2, 6) for s in seats})
            for depth, row in zip(depths, range(1, len(depths) + 1)):
                members = [s for s in seats if round(-sum(desks[s['desk']]['y']) / 2, 6) == depth]
                # All three rooms are entered from the north, looking south: east is left.
                members.sort(key=lambda s: -s['target_xy'][0])
                if [(s['row'], s['col']) for s in members] != [(row, c) for c in range(1, len(members) + 1)]:
                    raise ValueError('Seat numbering changed relative to the door')
        return {'fingerprints': self.fingerprints(), 'poses': evidence,
                'minimum_clearance_m': min(v['conservative_clearance_m'] for v in evidence.values()),
                'reachable_cells': len(seen), 'route_clearance_rule': 'EDT >= 0.22 + resolution (office worlds)',
                'endpoint_clearance_rule': 'EDT minus sqrt(2)*resolution >= 0.52 m'}


if __name__ == '__main__':
    print(json.dumps(Tags().validate(), indent=2))
