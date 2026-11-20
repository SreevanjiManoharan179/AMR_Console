import copy
import math

import pytest
import yaml

from tools.amr_task.build_tags import proposed_tags
from tools.amr_task.tags import Tags


def test_tags_match_generator_and_cover_layout(tags):
    generated = proposed_tags()
    assert generated['approval_status'] == 'proposed'
    assert dict(tags.data, approval_status='proposed') == generated
    assert len(tags.rooms) == 9 and len(tags.seats) == 9 and len(tags.people) == 8 and len(tags.objects) == 3
    assert len({s['desk'] for s in tags.seats.values()}) == 7
    assert tags.people['Divya']['role'] == 'manager'
    assert tags.destination('person', 'Divya').room == 'manager_cabin'


def test_every_pose_clear_reachable_and_stably_numbered(tags):
    result = tags.validate()
    assert len(result['poses']) == 21
    assert all(p['free'] and p['reachable'] and p['conservative_clearance_m'] >= .52
               for p in result['poses'].values())
    assert result['minimum_clearance_m'] == pytest.approx(.5293)
    assert tags.seats['open_office_r2c2']['desk'] == 'desks_3'
    assert tags.seats['open_office_r1c1']['desk'] == 'desks_2'
    assert tags.seats['team_office_r1c1']['desk'] == 'team_desk_2'
    # One shared position, two opposite yaws, explicitly documented.
    a, b = (tags.destination('seat', f'open_office_r2c{i}') for i in (1, 2))
    assert (a.x, a.y) == (b.x, b.y) and abs(a.yaw-b.yaw) == pytest.approx(math.pi)


@pytest.mark.parametrize('mutation', ['occupied', 'numbering', 'facing', 'offset', 'room_pose', 'missing_desk'])
def test_bad_tag_geometry_is_refused(tmp_path, tags, mutation):
    data = copy.deepcopy(tags.data)
    data['map_yaml'], data['layout_yaml'] = str(tags.map_path), str(tags.layout_path)
    if mutation == 'occupied':
        data['seats'][0]['y'] = -3.
    elif mutation == 'numbering':
        data['seats'][0]['col'] = 2
    elif mutation == 'facing':
        data['seats'][0]['yaw'] = 0.
    elif mutation == 'offset':
        data['seats'][0]['y'] = -1.8
    elif mutation == 'room_pose':
        data['rooms'][0]['x'] = .1
    elif mutation == 'missing_desk':
        data['seats'][4]['desk'] = 'desks_3'
    path = tmp_path/'tags.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        Tags(path).validate()
