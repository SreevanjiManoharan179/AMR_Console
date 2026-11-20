
import pytest

from tools.amr_task.clarify import Grounder, calibrate, calibration_score, prediction_set
from tools.amr_task.grammar import Frame, edit_distance, number


@pytest.mark.parametrize('text,kind,expected', [
    ('go to the open office, give to the second row second person', 'confirm', 'seat:open_office_r2c2'),
    ('deliver this to Priya in the conference hall', 'ask', 'conflict'),
    ('take this to the manager', 'confirm', 'seat:manager_cabin_r1c1'),
    ('bring it to Priy', 'confirm', 'seat:open_office_r1c1'),
    ('deliver to the 2nd row in the open office', 'ask', 'seat'),
    ('go to the confrence hall', 'confirm', 'room:conference_hall'),
    ('take this to Karthik', 'reject', "I don't know anyone called Karthik"),
    ('give this to the person near the printer', 'reject', 'I can deliver to rooms, seats, people or objects'),
    ('deliver to the manager cabin, third row', 'reject', 'The manager cabin has only 1 row'),
    ('go to the open office', 'confirm', 'room:open_office'),
])
def test_packet_ten_commands(conversation, text, kind, expected):
    d = conversation.begin(text)
    assert d.kind == kind
    assert (d.destination.key if kind == 'confirm' else d.slot if kind == 'ask' else d.question) == expected
    assert conversation.confirmed is None
    if expected == 'seat:open_office_r2c2':
        assert '— Ravi. Deliver here,' in d.question


@pytest.mark.parametrize('word,n', [('first', 1), ('second', 2), ('third', 3), ('fourth', 4), ('fifth', 5),
                                    ('1st', 1), ('2nd', 2), ('3rd', 3), ('4th', 4), ('5th', 5)])
def test_ordinals(tags, word, n):
    assert number(word) == n
    frame = Grounder(tags).grammar.parse(f'deliver to {word} row {word} person in the open office')
    assert frame.target_kind == 'seat' and frame.row == frame.col == n


def test_fuzzy_name_admission_and_probabilities(tags):
    g = Grounder(tags)
    assert edit_distance('priy', 'priya') == 1
    assert g.person_probabilities('Priy') == {'Priya': 1.}
    assert g.person_probabilities('Pry') == {'<none>': 1.}
    p = g.person_probabilities('Rai')
    assert p == {'Ravi': .5, 'Rani': .5}
    assert sum(g.person_probabilities('Ravi').values()) == pytest.approx(1.)


def test_name_implies_room_and_conflict_needs_selection_then_yes(conversation):
    d = conversation.begin('deliver to Priya in the conference hall')
    assert d.kind == 'ask' and d.slot == 'conflict'
    assert conversation.answer('yes') == d
    assert conversation.answer('open office').kind == 'confirm'
    with pytest.raises(ValueError):
        conversation.consume_confirmation()
    action, dest = conversation.answer('yes')
    assert action == 'deliver' and dest.person == 'Priya' and dest.room == 'open_office'
    assert conversation.consume_confirmation() == (action, dest)
    with pytest.raises(ValueError):
        conversation.consume_confirmation()


def test_missing_seat_then_explicit_confirmation(conversation):
    d = conversation.begin('deliver to the 2nd row in the open office')
    assert d.question.startswith('Which seat in row 2:')
    assert conversation.answer('seat 9') == d
    d = conversation.answer('seat 2')
    assert d.kind == 'confirm' and d.destination.person == 'Ravi'
    assert conversation.answer('no') is None
    with pytest.raises(ValueError):
        conversation.consume_confirmation()


def test_least_confident_slot_only(tags):
    g = Grounder(tags)
    frame = g.grammar.parse('deliver to Rai in the office')
    slots = g.score(frame)
    d = g.decide(frame, .6)
    assert d.kind == 'ask' and d.slot == min(slots, key=lambda s: max(slots[s].values()))
    assert d.destination is None


def test_typo_room_filters_seat_candidates(conversation):
    d = conversation.begin('deliver to row 1 seat 1 in the open ofice')
    assert d.kind == 'confirm' and d.destination.key == 'seat:open_office_r1c1'
    d = conversation.begin('deliver to row 3 in the manager cabn')
    assert d.kind == 'reject' and 'only 1 row' in d.question


@pytest.mark.parametrize('text', ['do not go to home', 'go to home then reception', 'if ready take this to Priya',
                                  'go to Priya and Ravi', 'where is the manager', 'deliver to row 1 row 2 seat 1',
                                  '', 'x'*501])
def test_invalid_or_multiple_intents_never_confirm(conversation, text):
    assert conversation.begin(text).kind == 'reject'
    with pytest.raises(ValueError):
        conversation.consume_confirmation()


def test_conformal_order_statistic_ties_and_weakest_slot():
    rows = [({'room': {'r': 1.}, 'person': {'p': .1}}, {'room': 'r', 'person': 'p'})]*20
    assert calibration_score(*rows[0], aggregation='min') == 0.
    assert calibration_score(*rows[0], aggregation='max') == .9
    assert calibrate(rows, .1) == .9
    assert calibration_score(*rows[0]) == .9
    assert calibrate(rows, .1, 'min') == 0.
    assert calibrate(rows, .1, 'max') == .9
    assert prediction_set({'a': .5, 'b': .5}, .5) == ('a', 'b')
    assert calibrate(rows[:1], .1) == 1.
    for invalid in [float('nan'), float('inf'), -.1, 1.1]:
        with pytest.raises(ValueError):
            prediction_set({}, invalid)


def test_empty_set_rejects_without_motion(tags):
    g = Grounder(tags)
    assert g.decide(Frame(room='office'), 0).kind == 'reject'
