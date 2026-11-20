"""Long requests split into their own steps (0.5.0): ", and <action>" and "come back"."""
import pytest

from amr_language.office_grammar import parse_english


def steps(text):
    plan = parse_english(text, 'Bharathwaj')
    assert plan is not None and plan['issue'] is None, plan
    return [(s['action'], s.get('to') or s.get('at'), s.get('question') or s.get('message') or s.get('item'),
             s['if_not_there']) for s in plan['steps']]


def test_a_closing_come_back_is_its_own_step_not_part_of_the_question():
    assert steps('Take this file to Divya, then ask Arun whether the encoder is ready, and come back.') == [
        ('deliver', 'Divya', 'file', 'bring_back'),
        ('check', 'Arun', 'whether the encoder is ready', 'bring_back'),
        ('go', 'me', None, 'bring_back')]


@pytest.mark.parametrize('text', ['Give the file to Divya and come back', 'Give the file to Divya, and come back to me.',
                                  'Give the file to Divya, then come back to me.'])
def test_come_back_means_back_to_the_person_who_asked(text):
    assert steps(text) == [('deliver', 'Divya', 'file', 'bring_back'), ('go', 'me', None, 'bring_back')]


def test_and_with_a_new_action_starts_a_new_step():
    assert steps('Ask Arun whether the encoder is ready, and tell Meera the meeting moved.') == [
        ('check', 'Arun', 'whether the encoder is ready', 'bring_back'), ('tell', 'Meera', 'the meeting moved', 'bring_back')]
    assert steps('Tell Meera the meeting moved, and bring the file to Divya.') == [
        ('tell', 'Meera', 'the meeting moved', 'bring_back'), ('deliver', 'Divya', 'file', 'bring_back')]


def test_and_inside_a_question_or_an_item_list_is_not_split():
    assert steps('Ask Arun whether the encoder and the motor are ready.') == [
        ('check', 'Arun', 'whether the encoder and the motor are ready', 'bring_back')]
    # Two items for one person are one stop with a stack of two (0.6.0).
    assert steps('Give the file, and the charger to Divya.') == [('deliver', 'Divya', 'file, and the charger', 'bring_back')]


def test_the_absence_rule_stays_with_its_delivery():
    assert steps('Give the file to Divya. If she is not there, leave it with Priya, and come back.') == [
        ('deliver', 'Divya', 'file', 'leave_with:Priya'), ('go', 'me', None, 'bring_back')]


def test_the_whole_pipeline_accepts_a_closing_come_back(names):
    # Live check of 5 Nov: the grammar split was right, but the slot check wanted "me" in the text.
    from amr_language.office import understand
    from amr_language.office_backend import MockOfficeBackend
    text = 'Take this file to Divya, then ask Arun whether the encoder is ready, and come back.'
    result = understand(text, names, 'Bharathwaj', 'en', {'backend': MockOfficeBackend([text] * 4, [{}] * 4)})
    assert result['decision'] == 'act', result.get('refuse')
    assert [(s['action'], s.get('to') or s.get('at')) for s in result['steps']] == [
        ('deliver', 'Divya'), ('check', 'Arun'), ('go', 'Bharathwaj')]


@pytest.mark.parametrize('text', [
    'Give the file to Divya. If she is not there, wait until she is back.',
    'Give the file to divya. if she is not there. go to priya and ask her" where she is now?".'])
def test_an_unsupported_fallback_is_refused_with_the_rules_it_can_follow(names, text):
    # Live check of 6 Nov: this was refused with only "I cannot do that task."
    from amr_language.office import understand
    from amr_language.office_backend import MockOfficeBackend
    result = understand(text, names, 'Bharathwaj', 'en', {'backend': MockOfficeBackend([text] * 4, [{}] * 4)})
    assert result['decision'] == 'refuse'
    assert result['refuse']['reason'] == 'unsupported_condition'
    assert "if someone is not there, leave it with another person, bring it back, or try again" in result['readback']


def test_the_supported_fallback_still_works(names):
    from amr_language.office import understand
    from amr_language.office_backend import MockOfficeBackend
    text = 'Give the file to Divya. If she is not there, leave it with Priya.'
    result = understand(text, names, 'Bharathwaj', 'en', {'backend': MockOfficeBackend([text] * 4, [{}] * 4)})
    assert result['decision'] == 'act'
    assert result['steps'][0]['if_not_there'] == 'leave_with:Priya'
