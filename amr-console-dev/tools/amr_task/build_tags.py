"""Reproduce the delivery task runner's proposed seat annotations; no world/map changes."""
import math
import yaml
from .tags import ROOT, DEFAULT_TAGS


def proposed_tags():
    rooms = yaml.safe_load((ROOT / 'src/amr_physical_ai/config/named_locations_proposed_system.yaml').read_text())['locations']
    # Two front-facing workstation annotations per front cluster, one per back cluster.
    definitions = [
        ('open_office', 1, 1, 'desks_2', 6.6, -2., 6.6, -3.2, 'north'),
        ('open_office', 1, 2, 'desks_2', 5.4, -2., 5.4, -3.2, 'north'),
        ('open_office', 1, 3, 'desks_1', 3., -2., 3., -3.2, 'north'),
        ('open_office', 1, 4, 'desks_1', 1.8, -2., 1.8, -3.2, 'north'),
        ('open_office', 2, 1, 'desks_4', 4.2, -5.4, 6., -5.4, 'west'),
        ('open_office', 2, 2, 'desks_3', 4.2, -5.4, 2.4, -5.4, 'east'),
        ('team_office', 1, 1, 'team_desk_2', 10.3, -4.7, 10.3, -5.65, 'north'),
        ('team_office', 1, 2, 'team_desk_1', 9.3, -4.7, 9.3, -5.65, 'north'),
        ('manager_cabin', 1, 1, 'manager_desk', 13.4, -3.2, 13.4, -4.2, 'north')]
    seats = [dict(id=f'{room}_r{row}c{col}', room=room, row=row, col=col, desk=desk,
                  x=x, y=y, yaw=math.atan2(ty-y, tx-x), target_xy=[tx, ty], aisle_edge=edge)
             for room, row, col, desk, x, y, tx, ty, edge in definitions]
    roster = [('Priya', 0), ('Neha', 1), ('Arun', 2), ('Meera', 3), ('Ravi', 5),
              ('Asha', 6), ('Rani', 7), ('Divya', 8)]
    return dict(schema_version=1, approval_status='proposed', frame_id='map', map_id='proposed_system',
                robot='TurtleBot3 Waffle simulation',
                map_yaml='../src/amr_physical_ai/maps/proposed_system.yaml',
                layout_yaml='../src/amr_physical_ai/worlds/proposed_system_layout.yaml',
                minimum_clearance_m=.52,
                annotation_notes=[
                    'Fictional seats/people, not detections. All room doors face south into these offices.',
                    'Rows follow desk-centre depth from the north doors; columns run east to west, as viewed from the door.',
                    'Two proposed workstation tags per front desk cluster; one per back cluster and individual desk.',
                    'Back-row seats share one aisle x/y and face opposite desks. The 1.0 m cross aisle cannot provide 0.52 m clearance.',
                    'open_office_r2c1 is vacant; all other seats have one fictional occupant.'],
                rooms=rooms, seats=seats,
                people=[dict(name=name, seat=seats[i]['id'], role='manager' if name == 'Divya' else 'staff') for name, i in roster],
                objects=[dict(name='printer', room='open_office', aliases=['office printer'], x=-1.7, y=-2.65, yaw=math.pi),
                         dict(name='fridge', room='cafeteria', aliases=['refrigerator'], x=11.85, y=5.3, yaw=math.pi),
                         dict(name='reception_desk', room='reception', aliases=['reception desk'], x=-.6, y=2.8, yaw=math.pi/2)])


if __name__ == '__main__':
    # Do not silently replace reviewed or approved tags.
    with DEFAULT_TAGS.open('x') as stream:
        yaml.safe_dump(proposed_tags(), stream, sort_keys=False)
