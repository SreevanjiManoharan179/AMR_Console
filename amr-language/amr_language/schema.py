"""The fixed task-form contract. No third-party validation dependency."""
import json
from pathlib import Path
import unicodedata

ACTIONS = ('go', 'check', 'deliver', 'wait_ack', 'retry', 'return')
LANGUAGES = ('en', 'ta', 'ml', 'te', 'mixed')
SLOTS = ('place', 'person', 'object', 'item')
SCHEMA = json.loads(Path(__file__).with_name('task_form.schema.json').read_text())


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=reject_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def valid_string(value, limit=300):
    return (isinstance(value, str) and 1 <= len(value) <= limit and bool(value.strip())
            and not any(unicodedata.category(c) in ('Cc', 'Cs') for c in value))


def validate_task_form(value):
    """Validate structure plus conservative action requirements; return original form."""
    if not isinstance(value, dict) or set(value) != {'language', 'steps', 'out_of_scope', 'reason'}:
        raise ValueError('Task form fields differ from schema')
    if value['language'] not in LANGUAGES or type(value['out_of_scope']) is not bool:
        raise ValueError('Invalid language or scope flag')
    if value['reason'] is not None and not valid_string(value['reason']):
        raise ValueError('Invalid reason')
    if value['out_of_scope'] != (value['reason'] is not None):
        raise ValueError('Reason must describe an out-of-scope form only')
    if not isinstance(value['steps'], list) or not 1 <= len(value['steps']) <= 4:
        raise ValueError('Need one to four steps')
    for step in value['steps']:
        if not isinstance(step, dict) or set(step) != {'action', *SLOTS}:
            raise ValueError('Invalid step fields')
        if step['action'] not in ACTIONS:
            raise ValueError('Invalid action')
        for field in SLOTS:
            if step[field] is not None and not valid_string(step[field]):
                raise ValueError('Invalid slot string')
        if value['out_of_scope']:
            continue
        if step['action'] in ('go', 'check', 'deliver') and not any(step[s] for s in SLOTS[:3]):
            raise ValueError('Missing destination')
        if step['action'] == 'deliver' and step['item'] is None:
            raise ValueError('Missing carried item')
        if step['action'] in ('wait_ack', 'retry', 'return') and any(step[s] for s in SLOTS):
            raise ValueError('Context-only actions cannot contain invented slots')
        if step['action'] != 'deliver' and step['item'] is not None:
            raise ValueError('Carried item requires delivery')
    return value
