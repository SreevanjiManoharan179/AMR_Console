"""0.8.0: the robot as an office assistant. A visit waits for what the person needs, a "no" at the
read-back is asked what to change, an unwanted item goes back, small talk gets an answer, another
language is asked for again in English, and the model may read a free reply."""
import pytest

from amr_language.office import understand
from amr_language.office_backend import MockOfficeBackend
from amr_language.rehearsal import Rehearsal


class Echo(MockOfficeBackend):
    def __init__(self, reply_kinds=None):
        super().__init__(['echo'], [{}])
        self.reply_kinds = reply_kinds or {}

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


def session(office, replies, backend=None, **settings):
    answers = iter(replies)
    return Robot(office, {'backend': backend or Echo(), 'conversational': True, **settings},
                 input_fn=lambda _prompt: next(answers), output_fn=lambda _line: None, source='SCRIPTED')


def spoken(s):
    return [r['event'] for r in s.records if r['kind'] == 'robot']


def ask(office, text, **settings):
    return understand(text, office, 'Bharathwaj', None, {'backend': Echo(), 'conversational': True, **settings})


# -- "go to Divya" ----------------------------------------------------------------------------

def test_a_visit_greets_the_person_and_takes_their_request(office):
    s = session(office, ['yes', 'Tell Ravi the file is ready.', 'yes', 'okay'])
    result = s.run('Go to Divya.', 'Bharathwaj', 'en')
    assert 'arrive_visit' in spoken(s)
    assert [j['result']['requester'] for j in result['jobs']] == ['Bharathwaj', 'Divya']
    assert result['jobs'][0]['outcomes'][0] == {'status': 'went', 'target': 'Divya', 'asked_for_more': True}
    assert result['jobs'][1]['outcomes'][0]['status'] == 'told'


def test_a_visit_where_nothing_is_needed_just_ends(office):
    s = session(office, ['yes', 'Nothing, thanks.'])
    result = s.run('Go to Divya.', 'Bharathwaj', 'en')
    assert len(result['jobs']) == 1 and result['jobs'][0]['outcomes'][0]['asked_for_more'] is False
    assert s.moves[-1] == ('home', 'home')


def test_a_silent_visit_calls_three_times_and_then_looks_for_the_person(office):
    s = session(office, ['yes', '', '', '', 'yes', 'no thanks'], search_for_person=True)
    outcome = s.run('Go to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert spoken(s).count('call_person') == 2 and 'searching' in spoken(s)
    assert outcome['status'] == 'went' and outcome['found_by_search'] is True


def test_going_to_a_place_or_back_to_the_requester_asks_nobody(office):
    s = session(office, ['yes'])
    s.run('Go to the cafeteria, then come back.', 'Bharathwaj', 'en')
    assert 'arrive_visit' not in spoken(s)


# -- the yes before moving --------------------------------------------------------------------

def test_no_at_the_read_back_is_asked_what_to_change(office):
    s = session(office, ['no', 'Give the file to Ravi.', 'yes', 'taken'])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert 'what_change' in spoken(s) and spoken(s).count('confirm_plan') == 2
    assert [j['decision'] for j in result['jobs']] == ['cancelled', 'rehearsed']
    assert result['jobs'][1]['outcomes'][0]['target'] == 'Ravi'


def test_a_new_sentence_at_the_read_back_is_the_correction(office):
    s = session(office, ['No, give it to Ravi instead of Divya please', 'no', 'no'])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    correction = next(r for r in s.records if r['kind'] == 'correction')
    assert correction['text'].startswith('give it to Ravi') and len(result['jobs']) == 2


def test_no_and_no_again_drops_the_job(office):
    s = session(office, ['no', 'no'])
    result = s.run('Give the file to Divya.', 'Bharathwaj', 'en')
    assert [j['decision'] for j in result['jobs']] == ['cancelled'] and 'dropped' in spoken(s)
    assert not [m for m in s.moves if m[1] != 'home']


# -- at the stop ------------------------------------------------------------------------------

def test_an_item_the_person_does_not_want_goes_back(office):
    s = session(office, ['yes', "No, it's not mine."])
    outcome = s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]
    assert outcome['reason'] == 'declined' and outcome['returned_to_requester'] is True
    report = next(r['text'] for r in s.records if r.get('event') == 'report')
    assert 'Divya did not take the file' in report


def test_the_model_may_read_a_free_reply_but_never_a_yes_out_of_a_no(office):
    backend = Echo({'she has it now': 'taken', "no wait don't go": 'yes'})
    s = session(office, ['yes', 'she has it now'], backend=backend, reply_model=True)
    assert s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]['status'] == 'delivered'
    assert any(r['kind'] == 'reply_read_by_model' for r in s.records)
    s = session(office, ["no wait don't go", 'no', 'no'], backend=backend, reply_model=True)
    assert s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['decision'] == 'cancelled'


def test_an_unclear_reply_is_asked_again_and_then_slowly(office):
    s = session(office, ['yes', 'blue seven', 'umm what', 'taken'])
    assert s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]['status'] == 'delivered'


# -- small talk -------------------------------------------------------------------------------

@pytest.mark.parametrize('text,event', [('Hello', 'greeting'), ('Hi robot!', 'greeting'), ('Thank you', 'welcome'),
                                        ('What can you do?', 'capabilities'), ('Who are you?', 'introduce'),
                                        ('Where is Divya?', 'person_place'), ('Where does Ravi sit?', 'person_place')])
def test_small_talk_gets_an_answer_and_no_plan(office, text, event):
    result = ask(office, text)
    assert result['decision'] == 'refuse' and result['refuse'] == {'event': event, 'reason': 'conversation'}
    assert result['steps'] == [] and result['readback']


def test_a_real_request_is_not_small_talk(office):
    assert ask(office, 'Hello, give the file to Divya.')['decision'] in ('act', 'ask', 'refuse')
    assert ask(office, 'Tell Ravi that I said thank you.')['decision'] == 'act'
    assert 'usual place is' in ask(office, 'Where is Divya?')['readback']


def test_nonsense_is_answered_humbly(office):
    result = understand('blorp the zzz', office, 'Bharathwaj', None,
                        {'backend': MockOfficeBackend(['blorp the zzz'] * 4, [ValueError('bad')] * 4), 'conversational': True})
    assert result['decision'] == 'refuse' and result['refuse']['event'] == 'not_understood'
    assert result['readback'].startswith("Sorry, I didn't understand that.")


# -- English only -----------------------------------------------------------------------------

@pytest.mark.parametrize('text', ['இந்த கோப்பை திவ்யாவிடம் கொடுங்க', 'file-a Divya kitta kudunga', 'ഈ ഫയൽ ദിവ്യയ്ക്ക് കൊടുക്കൂ'])
def test_another_language_is_asked_for_again_in_english(office, text):
    result = ask(office, text, english_only=True)
    assert result['decision'] == 'ask' and result['ask']['event'] == 'english_only' and result['language'] == 'en'
    assert result['readback'].startswith('Sorry, I can only work in English for now.')


def test_english_still_works_and_everyone_is_answered_in_english(office):
    result = understand('Give the file to Divya.', office, 'Priya', None, {'backend': Echo(), 'english_only': True})
    assert result['decision'] == 'act' and result['language'] == 'en' and result['readback'].startswith('Sure.')


def test_a_reply_in_another_language_is_asked_again_in_english(office):
    s = session(office, ['yes', 'எடுத்துக்கிட்டேன்', 'taken'], english_only=True)
    assert s.run('Give the file to Divya.', 'Bharathwaj', 'en')['jobs'][0]['outcomes'][0]['status'] == 'delivered'
    assert 'english_only' in spoken(s)
