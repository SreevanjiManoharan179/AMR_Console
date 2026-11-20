"""0.6.0: chained requests (then / if / otherwise), one trip for up to four items, calling a person,
and the closing question to the last listed recipient."""
import pytest

from amr_language.office import understand
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_grammar import parse_english
from amr_language.rehearsal import Rehearsal


class Echo(MockOfficeBackend):
    """English in, the same English out: every request of a session is its own text."""
    def __init__(self):
        super().__init__(['echo'], [{}])

    def rewrite(self, text, roster, language, index, timeout, settings):
        return text


class Robot(Rehearsal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.moves = []

    def move_to(self, place, purpose, target=None):
        self.moves.append((place, purpose))
        return True


@pytest.fixture
def office(names):
    names['requester_place'] = 'reception'
    return names


def plan(text):
    result = parse_english(text, 'Bharathwaj')
    assert result and result['issue'] is None, result
    return [(s['action'], s.get('from') or s.get('at') or s.get('to'), s.get('item') or s.get('message') or s.get('question'))
            for s in result['steps']]


def session(office, replies, **settings):
    answers = iter(replies)
    return Robot(office, {'backend': Echo(), **settings}, input_fn=lambda _prompt: next(answers),
                 output_fn=lambda _line: None, source='SCRIPTED')


def spoken(s):
    return [r['event'] for r in s.records if r['kind'] == 'robot']


# -- the words --------------------------------------------------------------------------------

def test_then_carries_the_verb_to_the_next_stop():
    assert plan('Go to reception, then the cafeteria, then the demo area') == [
        ('go', 'reception', None), ('go', 'the cafeteria', None), ('go', 'the demo area', None)]
    assert plan('Give the file to Divya, then the charger to Ravi, then the notebook to Meera, then the parcel to Arun') == [
        ('deliver', 'Divya', 'file'), ('deliver', 'Ravi', 'charger'), ('deliver', 'Meera', 'notebook'), ('deliver', 'Arun', 'parcel')]
    assert plan('Collect the report from Ravi and the charger from Meera') == [
        ('retrieve', 'Ravi', 'report'), ('retrieve', 'Meera', 'charger')]


def test_different_jobs_chain_in_one_request():
    assert plan('Give the file to Divya, then ask Arun whether the encoder is ready, then call Meera, then come back') == [
        ('deliver', 'Divya', 'file'), ('check', 'Arun', 'whether the encoder is ready'),
        ('tell', 'Meera', 'Please come'), ('go', 'me', None)]
    assert plan('Give the file to Divya and tell Ravi the meeting moved to 3') == [
        ('deliver', 'Divya', 'file'), ('tell', 'Ravi', 'the meeting moved to 3')]


def test_and_inside_a_message_stays_in_the_message():
    assert plan('Tell Ravi the meeting moved and the room changed') == [('tell', 'Ravi', 'the meeting moved and the room changed')]


def test_seven_stops_are_too_many_for_one_request(office):
    text = ('Go to reception, then the cafeteria, then the demo area, then storage, then the conference hall, '
            'then the team office, then the open office.')
    assert parse_english(text, 'Bharathwaj') == {'steps': [], 'issue': 'too_many_steps'}
    result = understand(text, office, 'Bharathwaj', 'en', {'backend': Echo()})
    assert result['decision'] == 'ask' and result['ask']['event'] == 'split_job'


def test_calling_a_person_is_a_message(office):
    assert plan('Call Ravi') == [('tell', 'Ravi', 'Please come')]
    assert plan('Call Ravi to the conference hall') == [('tell', 'Ravi', 'Please come to the conference hall')]
    assert plan('Ask Ravi to come to the conference hall') == [('tell', 'Ravi', 'come to the conference hall')]
    for text in ('Call Ravi.', 'Call Ravi to the conference hall.', 'Ask Ravi to come here.'):
        result = understand(text, office, 'Bharathwaj', 'en', {'backend': Echo()})
        assert result['decision'] == 'act', (text, result.get('ask'), result.get('refuse'))
        assert [(s['action'], s['to']) for s in result['steps']] == [('tell', 'Ravi')]


def test_if_yes_and_if_no_may_go_ask_or_call():
    result = parse_english('Ask Arun whether the encoder is ready. If yes, go to Divya and tell her the encoder is ready. '
                           'If no, call Ravi to the conference hall.', 'Bharathwaj')
    answer = result['steps'][0]['on_answer']
    assert [(s['action'], s['to'], s['message']) for s in answer['yes']] == [('tell', 'Divya', 'the encoder is ready')]
    assert [(s['action'], s['to'], s['message']) for s in answer['no']] == [('tell', 'Ravi', 'Please come to the conference hall')]


def test_if_not_there_takes_other_steps(office):
    text = 'Give the file to Divya. If she is not there, go to Priya and ask her where she is now.'
    result = understand(text, office, 'Bharathwaj', 'en', {'backend': Echo()})
    assert result['decision'] == 'act', (result.get('ask'), result.get('refuse'))
    step = result['steps'][0]
    assert step['if_not_there'] == 'otherwise'
    assert [(s['action'], s['at'], s['question']) for s in step['otherwise']] == [('check', 'Priya', 'where she is now')]
    assert "if they are not there, I'll go to Priya and ask your question" in result['readback']
    # The fixed rules are still the fixed rules.
    assert parse_english('Give the file to Divya. If she is not there, leave it with Ravi.', 'Bharathwaj')['steps'][0]['if_not_there'] == 'leave_with:Ravi'
    # Live check of 10 Nov: "then" before the fixed rule was refused.
    for text in ('Give the file to Divya. If she is not there, then give it to Priya.',
                 'Give the file to Divya. If she is not there then leave it with Priya.',
                 'Give the file to Divya, if she is not there, please give it to Priya.'):
        assert parse_english(text, 'Bharathwaj')['steps'][0]['if_not_there'] == 'leave_with:Priya', text
    # A condition about someone else is not an absence rule.
    assert parse_english('Give the file to Divya. If Ravi is not there, tell Priya I am late.', 'Bharathwaj') == {'steps': [], 'issue': 'unsupported_condition'}


# -- the tray ---------------------------------------------------------------------------------

def test_a_stack_for_one_person_is_one_stop(office):
    text = 'Give the file, the charger and the folder to Divya.'
    result = understand(text, office, 'Bharathwaj', 'en', {'backend': Echo()})
    assert result['decision'] == 'act', (result.get('ask'), result.get('refuse'))
    assert len(result['steps']) == 1 and 'take the file, the charger and the folder to Divya' in result['readback']
    many = understand('Give the file, the charger, the folder, the letter and the parcel to Divya.', office, 'Bharathwaj', 'en',
                      {'backend': Echo()})
    assert many['decision'] == 'refuse' and many['refuse']['reason'] == 'too_many_items'
    assert 'more items than I can carry at once' in many['readback']


def test_four_deliveries_are_loaded_in_one_visit(office):
    text = 'Give the file to Divya, then the charger to Ravi, then the folder to Meera, then the letter to Arun.'
    s = session(office, ['yes', 'taken', 'taken', 'taken', 'taken'])
    result = s.run(text, 'Bharathwaj', 'en')
    assert [o['status'] for o in result['jobs'][0]['outcomes']] == ['delivered'] * 4
    assert [m for m in s.moves if m[1] == 'pickup'] == [('reception', 'pickup')]
    load = next(r for r in s.records if r['kind'] == 'load')
    assert load['items'] == ['file', 'charger', 'folder', 'letter'] and load['count'] == 4


def test_a_fifth_item_waits_for_a_second_trip(office):
    text = 'Give the file, the charger and the folder to Divya, then the letter to Ravi, then the parcel to Meera.'
    # The parcel is loaded on the second visit, where its size is asked about ("yes").
    s = session(office, ['yes', 'taken', 'taken', 'yes', 'taken'])
    result = s.run(text, 'Bharathwaj', 'en')
    assert [o['status'] for o in result['jobs'][0]['outcomes']] == ['delivered'] * 3
    assert [r['count'] for r in s.records if r['kind'] == 'load'] == [4, 1]
    assert len([m for m in s.moves if m[1] == 'pickup']) == 2


# -- not there, otherwise ---------------------------------------------------------------------

def test_otherwise_steps_run_only_when_the_person_is_away(office):
    text = 'Give the file to Divya. If she is not there, tell Ravi that the file is with me.'
    there = session(office, ['yes', 'taken'])
    assert [o['status'] for o in there.run(text, 'Bharathwaj', 'en')['jobs'][0]['outcomes']] == ['delivered']
    assert 'arrive_tell' not in spoken(there)
    away = session(office, ['yes', 'not here', '', 'done'])
    outcomes = away.run(text, 'Bharathwaj', 'en')['jobs'][0]['outcomes']
    assert [(o['status'], o['target']) for o in outcomes] == [('failed', 'Divya'), ('told', 'Ravi')]
    assert outcomes[0]['otherwise_used'] and outcomes[0]['returned_to_requester'] is True
    # Divya's desk, Ravi's desk, and only then back to the requester with the file.
    places = [m[0] for m in away.moves]
    assert places[0] == 'reception' and places[-2:] == ['reception', 'home'] and len(places) == 5


# -- the closing question ---------------------------------------------------------------------

def test_no_closing_question_unless_switched_on(office):
    s = session(office, ['yes', 'taken'])
    s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert 'anything_else' not in spoken(s)


def test_the_last_recipient_can_decline_and_the_robot_goes_home(office):
    for reply in ('no', 'No thanks', 'go back to the dock', ''):
        s = session(office, ['yes', 'taken', reply], closing_question=True)
        result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
        assert len(result['jobs']) == 1 and 'anything_else' in spoken(s)
        assert s.moves[-1] == ('home', 'home')


def test_the_last_recipient_gives_a_new_job_with_its_own_read_back(office):
    s = session(office, ['yes', 'taken', 'Tell Ravi the file has arrived.', 'yes', 'done', 'no'], closing_question=True)
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert [j['result']['requester'] for j in result['jobs']] == ['Bharathwaj', 'Divya']
    assert [o['status'] for o in result['jobs'][1]['outcomes']] == ['told']
    assert spoken(s).count('confirm_plan') == 2 and spoken(s).count('anything_else') == 2
    request = next(r for r in s.records if r['kind'] == 'follow_up_request')
    assert request['requester'] == 'Divya' and request['depth'] == 1
    # Divya's own delivery would be loaded where she is, not at the first requester's place.
    assert request['place'] == 'manager_cabin_r1c1'


def test_yes_alone_is_asked_what_and_a_refused_read_back_ends_it(office):
    s = session(office, ['yes', 'taken', 'yes', 'Call Arun.', 'no'], closing_question=True)
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert 'anything_else_what' in spoken(s)
    assert [j['decision'] for j in result['jobs']] == ['rehearsed', 'cancelled']


def test_follow_up_jobs_stop_at_the_limit(office):
    replies = ['yes', 'done', 'Call Ravi.', 'yes', 'done', 'Call Meera.', 'yes', 'done']
    s = session(office, replies, closing_question=True, follow_up_limit=2)
    result = s.run('Call Divya.', 'Bharathwaj', 'en')
    assert len(result['jobs']) == 3 and spoken(s).count('anything_else') == 2


def test_only_a_listed_person_at_the_last_stop_is_asked(office):
    for text, replies in (('Go to the cafeteria.', ['yes']), ('Collect the charger from Ravi.', ['yes', 'done', 'taken'])):
        s = session(office, replies, closing_question=True)
        s.run(text, 'Bharathwaj', 'en')
        assert 'anything_else' not in spoken(s), text
