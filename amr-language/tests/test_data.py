from collections import Counter
import csv
import json
from pathlib import Path

import yaml

from amr_language.core import source_span
from amr_language.grounding import Names
from amr_language.schema import validate_task_form, valid_string


def test_all_synthetic_rows_have_valid_forms_and_expected_approved_ids():
    rows=[json.loads(line) for line in Path('data/dev_synthetic.jsonl').read_text().splitlines()]
    assert Counter(row['language'] for row in rows)=={'en':120,'ta':80,'ml':50,'te':50}
    assert len({row['text'] for row in rows})==300
    assert len({row['id'] for row in rows})==300
    names=Names(yaml.safe_load(Path('data/dev_names.yaml').read_text()))
    assert names.raw['fixture_source']=='SYNTHETIC'
    for row in rows:
        assert row['source']=='SYNTHETIC' and valid_string(row['text'])
        form=validate_task_form(row['expected_task_form'])
        assert len(form['steps'])==len(row['expected_ids'])
        for step,truth in zip(form['steps'],row['expected_ids']):
            for slot,keys in truth.items():
                assert set(names.match(slot,step[slot]))==set(keys),(row['id'],slot)
                assert source_span(step[slot],row['text']),(row['id'],step[slot],row['text'])
            if step['item']:
                assert source_span(step['item'],row['text'])
    for lang in ('en','ta','ml','te'):
        assert {row['category'] for row in rows if row['language']==lang}=={
            'go','check','deliver','multi','retry','wait_ack','return','ambiguous','unknown',
            'out_of_scope','negation','conditional'}
    tamil=[row['text'] for row in rows if row['language']=='ta']
    assert any(all(ord(char)<128 for char in text) for text in tamil)
    assert any(any('\u0b80'<=char<='\u0bff' for char in text) and any(char.isascii() and char.isalpha() for char in text) for text in tamil)


def test_real_sheet_is_header_only_until_real_people_provide_commands():
    with Path('data/real_commands_sheet.csv').open(newline='') as stream:
        reader=csv.DictReader(stream)
        assert reader.fieldnames==['id','speaker','language','typed_text','intended_action','intended_targets','notes']
        assert list(reader)==[]
