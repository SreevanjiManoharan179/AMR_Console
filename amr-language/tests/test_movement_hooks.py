"""Robot movement hooks: every move goes through arrive(); a robot caller overrides move_to()."""
import pytest

from amr_language.office_backend import MockOfficeBackend
from amr_language.rehearsal import Rehearsal, RehearsalStopped


class Robot(Rehearsal):
    """Records moves like a robot caller; places in `blocked` cannot be reached."""
    def __init__(self, *args, blocked=(), stop_at=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.moves = []
        self.blocked = set(blocked)
        self.stop_at = stop_at

    def move_to(self, place, purpose, target=None):
        self.moves.append((place, purpose, target))
        if place == self.stop_at:
            raise RehearsalStopped()
        return place not in self.blocked


def session(names, text, replies, cls=Robot, **kwargs):
    answers = iter(replies)
    return cls(names, {'backend': MockOfficeBackend([text] * 4, [{}] * 4)},
               input_fn=lambda _prompt: next(answers), output_fn=lambda _line: None,
               source='SCRIPTED', **kwargs)


@pytest.fixture
def office(names):
    names['requester_place'] = 'reception'
    return names


def test_delivery_picks_up_from_the_requester_then_drives_to_the_person(office):
    s = session(office, 'Give the file to Divya.', ['yes', 'taken'])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert result['jobs'][0]['outcomes'][0]['status'] == 'delivered'
    assert s.moves == [('reception', 'pickup', 'Bharathwaj'),
                       ('manager_cabin_r1c1', 'step', 'Divya'),
                       ('home', 'home', 'home')]
    spoken = [r['event'] for r in s.records if r['kind'] == 'robot']
    # Arrival speech comes only after the move to the person's desk.
    assert spoken.index('arrive_deliver') > spoken.index('start_step')


def test_unreachable_delivery_fails_and_brings_the_item_back(office):
    s = session(office, 'Give the file to Divya.', ['yes'], blocked={'manager_cabin_r1c1'})
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    outcome = result['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'failed' and outcome['reason'] == 'unreachable'
    assert outcome['returned_to_requester'] is True
    assert ('reception', 'return', 'Bharathwaj') in s.moves
    failures = [r for r in s.records if r.get('event') == 'job_failed']
    assert failures and 'could not reach' in failures[0]['text']
    assert not any(r.get('event') == 'arrive_deliver' for r in s.records)


def test_collection_drives_to_the_source_then_back_to_the_requester(office):
    s = session(office, 'Get the charger from Arun.', ['yes', 'done', 'taken'])
    result = s.run('Get the charger from Arun.', 'Bharathwaj', 'en')
    assert result['jobs'][0]['outcomes'][0]['status'] == 'retrieved'
    assert [m[0] for m in s.moves] == ['open_office_r1c3', 'reception', 'home']


def test_go_step_moves_and_reports_unreachable(office):
    s = session(office, 'Go to the cafeteria.', ['yes'], blocked={'cafeteria'})
    result = s.run('Go to the cafeteria.', 'Bharathwaj', 'en')
    outcome = result['jobs'][0]['outcomes'][0]
    assert outcome == {'status': 'failed', 'target': 'cafeteria', 'item': None, 'reason': 'unreachable'}
    assert [m[0] for m in s.moves] == ['cafeteria', 'home']


def test_stop_during_a_move_ends_the_job_without_more_moves(office):
    s = session(office, 'Go to the cafeteria.', ['yes'], stop_at='cafeteria')
    assert s.run('Go to the cafeteria.', 'Bharathwaj', 'en')['status'] == 'stopped'
    assert [m[0] for m in s.moves] == ['cafeteria']


def test_no_move_when_the_robot_is_already_there(office):
    s = session(office, 'Give the file to Divya.', ['yes', 'taken'])
    s.position = 'reception'
    s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert [m[0] for m in s.moves] == ['manager_cabin_r1c1', 'home']


def test_absent_recipient_item_returns_to_the_requester(office):
    s = session(office, 'Give the file to Divya.', ['yes', 'not here', ''])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    outcome = result['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'failed' and outcome['returned_to_requester'] is True
    assert [m[0] for m in s.moves] == ['reception', 'manager_cabin_r1c1', 'reception', 'home']


def test_text_rehearsal_records_moves_without_driving(office):
    s = session(office, 'Go to the cafeteria.', ['yes'], cls=Rehearsal)
    s.run('Go to the cafeteria.', 'Bharathwaj', 'en')
    moves = [r for r in s.records if r['kind'] == 'move']
    assert [m['place'] for m in moves] == ['cafeteria', 'home'] and all(m['simulated'] for m in moves)


def test_unreachable_reason_has_four_fixed_languages():
    from amr_language.office_speech import REASONS, say
    assert set(REASONS['unreachable']) == {'en', 'ta', 'ml', 'te'}
    for language in ('en', 'ta', 'ml', 'te'):
        assert REASONS['unreachable'][language] in say('job_failed', language, reason='unreachable')


def test_a_step_is_announced_in_the_requesters_language_not_the_next_persons(office):
    # Live check of 5 Nov: "Starting step 1" for Arun was said in Telugu to the English requester.
    next(p for p in office['people'] if p['name'] == 'Arun')['language'] = 'te'
    s = session(office, 'Ask Arun whether the encoder is ready.', ['yes', 'It is ready.', 'yes'])
    s.run('Ask Arun whether the encoder is ready.', 'Bharathwaj', 'en')
    spoken = {r['event']: r['language'] for r in s.records if r['kind'] == 'robot'}
    assert spoken['start_step'] == 'en'
    assert spoken['arrive_check'] == 'te'      # at Arun's desk the robot speaks Arun's language


def test_a_request_typed_in_tamil_keeps_tamil_for_the_requester(office):
    text = 'file-ஐ Divya-கிட்ட கொடுங்க'
    s = Robot(office, {'backend': MockOfficeBackend(['Give the file to Divya.'] * 4, [{}] * 4)},
              input_fn=lambda _prompt, _a=iter(['yes', 'taken']): next(_a), output_fn=lambda _line: None, source='SCRIPTED')
    s.run(text, 'Bharathwaj', None)
    spoken = [(r['event'], r['language']) for r in s.records if r['kind'] == 'robot']
    assert ('confirm_plan', 'ta') in spoken and ('start_step', 'ta') in spoken
    assert ('report', 'ta') in spoken
