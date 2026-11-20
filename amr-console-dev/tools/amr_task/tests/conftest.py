import shutil

import pytest
import yaml

from tools.amr_task.clarify import Conversation, Grounder
from tools.amr_task.live_session import load_threshold
from tools.amr_task.tags import ROOT, Tags


@pytest.fixture(scope='session')
def tags():
    return Tags()


@pytest.fixture
def conversation(tags):
    return Conversation(Grounder(tags), load_threshold(tags))


@pytest.fixture
def live_repo(tmp_path):
    files = ['config/tags_proposed_system.yaml', 'src/amr_physical_ai/config/named_locations_proposed_system.yaml',
             'src/amr_physical_ai/worlds/proposed_system_layout.yaml', 'src/amr_physical_ai/maps/proposed_system.yaml',
             'src/amr_physical_ai/maps/proposed_system.pgm', 'config/calibration/proposed_system/tasks/results.json']
    for name in files:
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, path)
        if 'config/' in name and name.endswith('.yaml'):
            data = yaml.safe_load(path.read_text())
            data['approval_status'] = 'confirmed'
            path.write_text(yaml.safe_dump(data, sort_keys=False))
    approval = tmp_path/'tools/amr_console/approvals.yaml'
    approval.parent.mkdir(parents=True)
    approval.write_text('proposed_system_places: true\n')
    return tmp_path
