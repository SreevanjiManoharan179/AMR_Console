"""Faults found with the console's request battery and the Speech Lab on 10 Nov 2026 (0.5.2)."""
import pytest

from amr_language.office import (_agreement_key, ask_whether, detect_language, romanised_tamil, substitute_aliases, understand)
from amr_language.office_backend import MockOfficeBackend
from amr_language.office_names import OfficeNames


class Seen(MockOfficeBackend):
    """Records the text the translation step was given."""
    def rewrite(self, text, roster, language, index, timeout, settings):
        self.given = text
        return super().rewrite(text, roster, language, index, timeout, settings)


@pytest.fixture
def office(names):
    people = {p['name']: p for p in names['people']}
    people['Meera'].setdefault('aliases_by_language', {})['ta'] = ['மீரா']
    people['Divya'].setdefault('aliases_by_language', {}).update(ta=['திவ்யா'], ml=['ദിവ്യ'])
    people['Arun'].setdefault('aliases_by_language', {})['ta'] = ['அருண்', 'அருணி']
    next(r for r in names['rooms'] if r['name'] == 'reception').setdefault('aliases_by_language', {})['ta'] = ['வரவேற்பு']
    return names


def run(office, text, english, language=None, requester='Bharathwaj'):
    backend = Seen([english] * 4, [{}] * 4)
    return understand(text, office, requester, language, {'backend': backend}), backend


def test_names_and_places_reach_the_model_in_english_letters(office):
    roster = OfficeNames(office, 'Bharathwaj')
    assert substitute_aliases('மீராவிடம் கூட்டம் மாறியது என்று சொல்லுங்க', roster).startswith('Meera-விடம் ')
    assert substitute_aliases('ഈ ഫയൽ ദിവ്യയ്ക്ക് കൊടുക്കൂ', roster) == 'ഈ ഫയൽ Divya-യ്ക്ക് കൊടുക്കൂ'
    assert substitute_aliases('வரவேற்புக்கு போ', roster) == 'reception-க்கு போ'
    assert substitute_aliases('Give the file to Divya.', roster) == 'Give the file to Divya.'
    result, backend = run(office, 'மீராவிடம் கூட்டம் மூன்று மணிக்கு மாறியது என்று சொல்லுங்க',
                          "Tell Meera that the meeting has moved to three o'clock.")
    assert backend.given.startswith('Meera-விடம்')
    assert result['decision'] == 'act' and result['steps'][0]['to'] == 'Meera'


def test_a_request_written_in_tamil_is_answered_in_tamil_whatever_the_profile_says(office):
    office['requester'] = {'id': 'Bharathwaj', 'language': 'en'} if isinstance(office.get('requester'), dict) else office.get('requester')
    result, _ = run(office, 'இந்த கோப்பை திவ்யாவிடம் கொடுங்க', 'Give this file to Divya.',
                    requester={'id': 'Bharathwaj', 'name': 'Bharathwaj', 'language': 'en'})
    assert result['language'] == 'ta' and result['decision'] == 'act'
    assert any('஀' <= c <= '௿' for c in result['readback'])
    english, _ = run(office, 'Give the file to Divya.', 'Give the file to Divya.',
                     requester={'id': 'Bharathwaj', 'name': 'Bharathwaj', 'language': 'en'})
    assert english['language'] == 'en'


@pytest.mark.parametrize('english', ['Ask Arun if the encoder is ready.', 'ask Arun if the encoder is ready'])
def test_ask_someone_if_is_a_question(office, english):
    assert ask_whether(english).lower().startswith('ask arun whether the encoder')
    result, _ = run(office, 'அருணிடம் என்கோடர் தயாரா என்று கேளுங்க', english)
    assert result['decision'] == 'act', result.get('refuse')
    assert result['steps'][0]['action'] == 'check' and 'encoder is ready' in result['steps'][0]['question']


def test_a_real_condition_keeps_its_if():
    text = 'Ask Arun whether he is in. If he is not there, bring it back.'
    assert ask_whether(text) == text
    assert ask_whether('Give the file to Divya. If she is not there, leave it with Priya.').count('whether') == 0


@pytest.mark.parametrize('text,expected', [
    ('file-a Divya kitta kudunga', True), ('Arun kitta encoder ready-aa nu kelunga', True),
    ('Ravi kitta irunthu charger eduthuttu vaanga', True), ('manager cabin-ku file kudunga', True),
    ('Give the file to Divya.', False), ('Go to the cafeteria and come back.', False),
    ('Tell Meera the meeting moved to 3 pm.', False), ('Ask Arun whether the encoder is ready.', False)])
def test_tamil_typed_in_english_letters_is_recognised(text, expected):
    assert romanised_tamil(text) is expected
    assert detect_language(text) == ('ta' if expected else 'en')


def test_romanised_tamil_is_translated_and_done(office):
    result, _ = run(office, 'file-a Divya kitta kudunga', 'Give the file to Divya.')
    assert result['language'] == 'ta' and result['decision'] == 'act'
    assert [(s['action'], s['to']) for s in result['steps']] == [('deliver', 'Divya')]


def test_number_words_and_digits_agree():
    a = {'steps': [{'message': "the meeting has moved to three o'clock"}]}
    b = {'steps': [{'message': "The meeting has moved to 3 o'clock."}]}
    assert _agreement_key(a) == _agreement_key(b)
