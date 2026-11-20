from collections import Counter
import json

from tools.amr_task.pilot import run
from tools.amr_task.tags import ROOT


def test_pilot_is_deterministic_and_uses_disjoint_calibration_test_ids(tags):
    first = run(tags, repeats=3)
    second = run(tags, repeats=3)
    assert first == second
    result, rows, split, decisions, repeats = first
    assert len(rows) >= 150 and len(repeats) == 3
    assert len(split['calibration']) == int(.6*len(rows))
    assert not set(split['calibration']) & set(split['test'])
    assert len(set(split['calibration']) | set(split['test'])) == len(rows)
    assert {'typo_person', 'typo_room', 'ambiguous_room', 'ambiguous_person', 'out_of_roster', 'paraphrase_template'} <= set(Counter(r['category'] for r in rows))
    assert result['headline']['baseline']['question_rate'] == 0
    assert result['headline']['per_slot']['wrong_destinations'] == 0
    assert 'oracle' in '\n'.join(result['limitations']) or 'truthful' in '\n'.join(result['limitations'])


def test_shipped_calibration_is_what_the_pilot_regenerates(tags):
    result, rows, split, decisions, repeats = run(tags)
    directory = ROOT/'config/calibration/proposed_system/tasks'
    # The shipped calibration must be exactly what the pilot regenerates (the two larger
    # per-decision files are optional and checked only when present).
    for name, value in [('results', result), ('dataset', rows), ('split', split),
                        ('decisions_headline', decisions), ('repeated_splits', repeats)]:
        path = directory/(name+'.json')
        if name in ('results', 'dataset', 'split') or path.is_file():
            assert value == json.loads(path.read_text()), name
    assert result['meta']['aggregation'] == 'max'
