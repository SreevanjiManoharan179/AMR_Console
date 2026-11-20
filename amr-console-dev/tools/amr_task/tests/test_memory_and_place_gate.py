"""Delivery memory and place gate: out-of-roster place gate, truth error at arrival, persistent spatial memory."""
import json

import pytest

from tools.amr_task.clarify import Grounder
from tools.amr_task.executor import EventLog, Executor, NavigationResult
from tools.amr_task.memory import SpatialMemory
from tools.amr_task.tests.test_executor import Clock, Navigator


def events(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


@pytest.mark.parametrize('command', ['go to the gym', 'go to the lunchroom', 'go to the parking lot',
                                     'go to the swimming pool', 'go to the server room', 'go to the gym room'])
def test_place_not_on_the_map_is_refused_not_asked(conversation, command):
    assert conversation.begin(command).kind == 'reject'


def test_gate_refuses_what_the_scorer_would_have_asked_about(conversation):
    # "server room": the room scorer offers {conference hall, none}, so without the gate it asked.
    decision = conversation.begin('go to the server room')
    assert decision.kind == 'reject' and "I don't know a place called server room" in decision.question


@pytest.mark.parametrize('command', ['go to the cafeteria', 'go to the cafetria', 'go to the conference hall',
                                     'go to the open office', 'go to the store room', 'go to the meeting room'])
def test_known_or_misspelt_places_still_reach_the_conformal_step(conversation, command):
    assert conversation.begin(command).kind in ('ask', 'confirm')


def test_generic_words_alone_are_left_to_the_calibrated_question(tags):
    assert Grounder(tags).unknown_place('the office') is False


def test_unknown_person_is_still_refused(conversation):
    decision = conversation.begin('take this to Karthik')
    assert decision.kind == 'reject' and 'Karthik' in decision.question


@pytest.fixture
def memory_task(tmp_path, conversation):
    clock, nav, wall = Clock(), Navigator(), Clock()
    wall.now = 1_000_000.
    log = EventLog(tmp_path/'session', clock)
    memory = SpatialMemory(tmp_path/'memory/proposed_system.json', 'proposed_system', clock=wall)
    e = Executor(conversation, nav, log, clock=clock, memory=memory, run_id='run-1', navigation_timeout=10.)
    yield e, nav, clock, wall, log, memory
    log.close()


def deliver(e, nav, ack, command='take this to Priya'):
    e.command(command)
    e.answer('yes')
    nav.arrive()
    e.tick()
    e.acknowledge(ack)
    nav.arrive()
    e.tick()
    assert e.state == 'DONE'


def test_not_here_is_remembered_and_changes_the_next_dialogue(memory_task, tmp_path):
    e, nav, clock, wall, log, memory = memory_task
    deliver(e, nav, 'not here')
    stored = json.loads((tmp_path/'memory/proposed_system.json').read_text())
    assert stored['entries']['person:Priya']['outcome'] == 'RETURNED_NOT_HERE'
    assert stored['entries']['person:Priya']['run_id'] == 'run-1'
    wall.advance(600)
    question = e.command('take this to Priya')
    assert question.startswith('Last time (10 min ago) Priya: not here. Try again?')
    assert any(ev['type'] == 'memory' for ev in events(log))
    e.answer('yes')                      # the operator chooses to try again
    assert e.state == 'NAVIGATE'


def test_memory_survives_a_restart_and_expires(memory_task, tmp_path, conversation):
    e, nav, clock, wall, log, memory = memory_task
    deliver(e, nav, 'not here')
    reloaded = SpatialMemory(tmp_path/'memory/proposed_system.json', 'proposed_system', clock=wall)
    assert reloaded.entries['person:Priya']['outcome'] == 'RETURNED_NOT_HERE'
    wall.advance(3600)
    log2 = EventLog(tmp_path/'session2', clock)
    e2 = Executor(conversation, nav, log2, clock=clock, memory=reloaded, run_id='run-2')
    assert not e2.command('take this to Priya').startswith('Last time')
    log2.close()


def test_delivered_does_not_add_a_warning(memory_task):
    e, nav, clock, wall, log, memory = memory_task
    deliver(e, nav, 'taken')
    assert memory.entries['person:Priya']['outcome'] == 'DELIVERED'
    assert not e.command('take this to Priya').startswith('Last time')


def test_memory_for_another_map_is_refused(tmp_path):
    SpatialMemory(tmp_path/'m.json', 'proposed_system').record(
        type('D', (), {'person': 'Priya', 'key': 'seat:x', 'room': 'open_office'})(), 'DELIVERED', 'r')
    with pytest.raises(ValueError):
        SpatialMemory(tmp_path/'m.json', 'warehouse')


def test_truth_error_is_logged_next_to_the_estimate(tmp_path, conversation):
    clock, log = Clock(), EventLog(tmp_path/'s', Clock())
    class TruthNav(Navigator):
        def arrive(self, status='SUCCEEDED', offset=0.):
            goal = self.goals[-1]
            self.result = NavigationResult(status, goal.x+.1, goal.y, 'tf', goal.x+.2, goal.y, 'Gazebo truth')
    nav = TruthNav()
    e = Executor(conversation, nav, log, clock=clock)
    e.command('go to the cafeteria')
    e.answer('yes')
    nav.arrive()
    e.tick()
    arrival = [ev for ev in events(log) if ev['type'] == 'arrival'][0]
    assert arrival['error_m'] == pytest.approx(.1) and arrival['truth_error_m'] == pytest.approx(.2)
    assert e.outcome == 'NAVIGATED'
    log.close()


def test_without_truth_the_field_is_empty(tmp_path, conversation):
    log, nav = EventLog(tmp_path/'s', Clock()), Navigator()
    e = Executor(conversation, nav, log, clock=Clock())
    e.command('go to the cafeteria')
    e.answer('yes')
    nav.arrive()
    e.tick()
    arrival = [ev for ev in events(log) if ev['type'] == 'arrival'][0]
    assert arrival['truth_error_m'] is None
    log.close()


def test_cancel_after_the_end_does_not_refresh_memory(memory_task):
    e, nav, clock, wall, log, memory = memory_task
    deliver(e, nav, 'not here')
    stamp = memory.entries['person:Priya']['epoch_s']
    wall.advance(100)
    e.cancel()
    assert memory.entries['person:Priya']['epoch_s'] == stamp
    assert memory.entries['person:Priya']['outcome'] == 'RETURNED_NOT_HERE'


def test_a_plain_go_does_not_erase_not_here(memory_task):
    e, nav, clock, wall, log, memory = memory_task
    deliver(e, nav, 'not here')
    e.command('go to the cafeteria'); e.answer('yes'); nav.arrive(); e.tick()
    assert memory.entries['person:Priya']['outcome'] == 'RETURNED_NOT_HERE'


@pytest.mark.parametrize('bad', [{'outcome': 'RETURNED_NOT_HERE'}, 'junk', {'outcome': 'RETURNED_NOT_HERE', 'epoch_s': float('nan')}])
def test_malformed_memory_never_breaks_a_task(memory_task, bad):
    e, nav, clock, wall, log, memory = memory_task
    memory.entries['person:Priya'] = bad
    question = e.command('take this to Priya')
    assert not question.startswith('Last time') and e.state == 'CONFIRM'


def test_unreadable_memory_file_is_refused_at_open(tmp_path):
    path = tmp_path/'m.json'
    path.write_text('[]')
    with pytest.raises(ValueError):
        SpatialMemory(path, 'proposed_system')
