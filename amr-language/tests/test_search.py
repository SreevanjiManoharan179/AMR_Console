"""0.7.0: looking for a person who does not answer, spoken-style replies, plainer English, and
reading back an uncertain plan instead of asking for the whole request again."""
import pytest

from amr_language.office import understand
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_replies import short_reply
from amr_language.rehearsal import Rehearsal


class Echo(MockOfficeBackend):
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


def session(office, replies, **settings):
    answers = iter(replies)
    return Robot(office, {'backend': Echo(), 'search_for_person': True, **settings}, input_fn=lambda _prompt: next(answers),
                 output_fn=lambda _line: None, source='SCRIPTED')


def spoken(s):
    return [r['event'] for r in s.records if r['kind'] == 'robot']


def searched(s):
    return [m[0] for m in s.moves if m[1] == 'search']


# -- looking for a person ---------------------------------------------------------------------

def test_three_unanswered_calls_then_the_person_is_found_in_a_nearby_room(office):
    s = session(office, ['yes', '', '', '', 'yes', 'taken'])
    outcome = s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'delivered' and outcome['found_by_search'] is True
    events = spoken(s)
    assert events.count('call_person') == 2 and 'searching' in events and 'search_ask' in events
    # Nobody was at the desk, so nobody there was asked where she is.
    assert 'ask_people_where' not in events
    assert len(searched(s)) == 1 and outcome['at'] == searched(s)[0] != 'manager_cabin_r1c1'


def test_someone_at_the_desk_says_where_the_person_is(office):
    s = session(office, ['yes', 'not here', 'She is in the cafeteria.', 'taken'])
    outcome = s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'delivered' and outcome['at'] == 'cafeteria'
    assert searched(s) == ['cafeteria'] and 'ask_people_where' in spoken(s)


def test_nearby_rooms_then_the_open_office_then_the_item_comes_back(office):
    s = session(office, ['yes', '', '', '', '', '', ''])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    outcome = result['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'failed' and outcome['returned_to_requester'] is True
    stops = searched(s)
    assert len(stops) == 3 and stops[-1] == 'open_office' and 'home' not in stops
    assert 'search_failed' in spoken(s)
    assert [m[0] for m in s.moves][-2:] == ['reception', 'home']


def test_a_message_and_a_question_also_look_for_the_person(office):
    told = session(office, ['yes', '', '', '', 'yes', 'okay'])
    assert told.run('Tell Divya the meeting moved to 3.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]['status'] == 'told'
    asked = session(office, ['yes', '', '', '', 'no', 'yes', 'At three.', 'yes'])
    outcome = asked.run('Ask Divya what time the meeting is.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'checked' and len(searched(asked)) == 2


def test_the_otherwise_steps_run_after_an_unsuccessful_search(office):
    s = session(office, ['yes', '', '', '', '', '', '', 'okay'])
    outcomes = s.run('Give the file to Divya. If she is not there, tell Ravi that she is away.', 'Bharathwaj', 'en')['jobs'][0]['outcomes']
    assert [(o['status'], o['target']) for o in outcomes] == [('failed', 'Divya'), ('told', 'Ravi')]


def test_without_the_switch_nothing_is_searched(office):
    s = session(office, ['yes', '', '', ''], search_for_person=False)
    outcome = s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert outcome['status'] == 'failed' and not searched(s) and 'call_person' not in spoken(s)


# -- replies as people say them ---------------------------------------------------------------

@pytest.mark.parametrize('text,expect,kind', [
    ('Yeah, go ahead.', {'yes', 'no'}, 'yes'), ('Sure!', {'yes', 'no'}, 'yes'), ("That's right.", {'yes', 'no'}, 'yes'),
    ('No, thanks.', {'yes', 'no', 'later'}, 'no'), ('Taken, thanks!', {'taken', 'not_here', 'later'}, 'taken'),
    ('Thank you.', {'taken', 'not_here', 'later'}, 'taken'), ('Okay.', {'done', 'yes', 'not_here', 'later'}, 'done'),
    ("She's not here.", {'taken', 'not_here', 'later'}, 'not_here'), ('I am busy', {'taken', 'not_here', 'later'}, 'later'),
    ('Um, yes please', {'yes', 'no'}, 'yes')])
def test_spoken_replies_are_understood(text, expect, kind):
    assert short_reply(text, expect, 'en')['kind'] == kind


def test_a_reply_that_is_not_an_answer_stays_unclear():
    assert short_reply('The weather is nice.', {'yes', 'no'}, 'en')['kind'] == 'unclear'
    assert short_reply('thanks', {'yes', 'no'}, 'en')['kind'] == 'unclear'
    assert short_reply('Please stop.', {'yes', 'no'}, 'en')['kind'] == 'stop'


# -- how the robot says it ---------------------------------------------------------------------

def test_the_read_back_is_a_sentence(office):
    one = understand('Give the file to Divya.', office, 'Bharathwaj', 'en', {'backend': Echo()})
    assert one['readback'] == "Sure. I'll take the file to Divya. Shall I go ahead?"
    three = understand('Give the file to Divya, then ask Arun whether the encoder is ready, then come back.', office, 'Bharathwaj', 'en',
                       {'backend': Echo()})
    assert three['readback'] == ("Sure. First I'll take the file to Divya. Then I'll go to Arun and ask your question. "
                                 "And last, I'll go to Bharathwaj. Shall I go ahead?")


def test_an_uncertain_reading_is_read_back_for_checking(office):
    text = 'Give the file to Divya.'
    rewrites = [text, '{"not": "english"}', text, text]
    asked = understand(text, office, 'Bharathwaj', 'en', {'backend': MockOfficeBackend(rewrites, [{}] * 4)})
    assert asked['decision'] == 'ask' and asked['ask']['event'] == 'clarify_request'
    checked = understand(text, office, 'Bharathwaj', 'en', {'backend': MockOfficeBackend(rewrites, [{}] * 4), 'tentative_plans': True})
    assert checked['decision'] == 'act' and checked['tentative'] is True
    assert checked['readback'] == "I'm not fully sure I understood, so please check this. I'll take the file to Divya. Shall I go ahead?"
