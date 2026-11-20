from pathlib import Path
import pytest
import yaml

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _module_records_in_tmp(tmp_path,monkeypatch):
    """Records the module writes by default (learning suggestions) go to the test's own folder,
    never into this module's records/, which holds the owner's real review queue."""
    monkeypatch.setenv('AMR_LANGUAGE_RECORDS_DIR',str(tmp_path/'module_records'))


@pytest.fixture
def measurement_record():
    """Path of a saved measurement file under records/.

    The development repository keeps the measurement records and these tests check them. A copy
    without records/ (the public release) skips them; a missing file inside an existing records/
    is still a failure."""
    def path(relative):
        if not (ROOT/'records').is_dir():
            pytest.skip('needs the measurement records (records/), which this copy does not include')
        return ROOT/relative
    return path


@pytest.fixture
def names():
    value=yaml.safe_load(Path('reference/tags_example.yaml').read_text())
    # Explicit mock map facts; never modify the reference/console files.
    value['robot_capability']=yaml.safe_load(Path('tests/fixtures/robot_capability.yaml').read_text())
    for room in value['rooms']:room.update(on_map=True,reachable=True)
    for person in value['people']:person['usual_place']=person['seat']
    return value


@pytest.fixture
def form():
    def make(action='go', language='en', out_of_scope=False, reason=None, **slots):
        step = {'action': action, **{key: slots.get(key) for key in ('place', 'person', 'object', 'item')}}
        return {'language': language, 'steps': [step], 'out_of_scope': out_of_scope, 'reason': reason}
    return make
