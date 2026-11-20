"""Office tasks: the approved map context for the language worker, and stop poses by place key.

Pure Python, no ROS. Everything the robot may drive to comes from the owner-approved files
of one map: its named places, its people/desk tags and the robot's tray and weight limits.
The language worker only names place keys; poses always come from here.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import yaml

import registry

ROOT = Path(__file__).resolve().parents[2]
CONFIG = 'config/office_tasks.yaml'
LANGUAGES = ('en', 'ta', 'ml', 'te')
LANGUAGE_NAMES = {'en': 'English', 'ta': 'Tamil', 'ml': 'Malayalam', 'te': 'Telugu'}


class OfficeConfigError(ValueError):
    """The office-task configuration or an approved file is missing or invalid."""


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _inside(root, relative, label):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise OfficeConfigError(f'{label} is missing: {relative}')
    return path


def load_config(root=ROOT):
    """Read config/office_tasks.yaml; every path in it stays relative to the repository."""
    path = Path(root) / CONFIG
    try:
        value = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise OfficeConfigError(f'Office tasks are not configured: {CONFIG} could not be read ({exc}).') from exc
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise OfficeConfigError(f'{CONFIG} must have schema_version: 1.')
    maps = value.get('maps')
    if not isinstance(maps, dict) or not maps:
        raise OfficeConfigError(f'{CONFIG} lists no office maps.')
    for map_id, entry in maps.items():
        if map_id not in registry.COMMAND_MAPS or 'tags' not in registry.COMMAND_MAPS[map_id]:
            raise OfficeConfigError(f'{map_id} has no named places and people tags in config/worlds.yaml.')
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ('language', 'capability')):
            raise OfficeConfigError(f'{CONFIG}: {map_id} needs language and capability files.')
    language = value.get('language_module', {})
    if not isinstance(language, dict) or not isinstance(language.get('path'), str):
        raise OfficeConfigError(f'{CONFIG} needs language_module.path.')
    timeout = language.get('timeout_s', 20)
    if type(timeout) not in (int, float) or not 5 <= timeout <= 120:
        raise OfficeConfigError('language_module.timeout_s must be 5–120 seconds.')
    if language.get('model_runner', 'contained') not in ('contained', 'uncontained'):
        raise OfficeConfigError('language_module.model_runner must be contained or uncontained.')
    operator = value.get('operator', {})
    if not isinstance(operator, dict) or not isinstance(operator.get('name'), str) \
            or not re.fullmatch(r'[A-Za-z][A-Za-z .\'-]{0,59}', operator['name']):
        raise OfficeConfigError(f'{CONFIG} needs operator.name (letters only).')
    if operator.get('language', 'en') not in LANGUAGES:
        raise OfficeConfigError('operator.language must be en, ta, ml or te.')
    return value


def language_root(root=ROOT, config=None):
    config = config or load_config(root)
    path = (Path(root) / config['language_module']['path']).resolve()
    if not (path / 'amr_language/__init__.py').is_file():
        raise OfficeConfigError(f"The language module was not found at {path}. "
                                f"Set language_module.path in {CONFIG}.")
    return path


def capability_status(value):
    """Return a reason the capability profile cannot drive the robot, or None."""
    if not isinstance(value, dict):
        return 'The robot capability file is not a mapping.'
    if value.get('approval_status') != 'confirmed':
        return ('Confirm the robot tray and weight limits: set approval_status: confirmed in the '
                'capability file after reviewing its values.')
    for key in ('maximum_payload_kg', 'maximum_steps_per_job', 'maximum_wait_at_stop_s'):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            return f'The capability file needs a positive number for {key}.'
    tray = value.get('tray_inner_cm')
    if not isinstance(tray, dict) or not all(type(tray.get(k)) in (int, float) and tray[k] > 0
                                           for k in ('length', 'width', 'height')):
        return 'The capability file needs tray_inner_cm length, width and height.'
    return None


class OfficeContext:
    """One office map's approved places, desks, people, objects and robot limits."""

    def __init__(self, root=ROOT, map_id='proposed_system', config=None):
        self.root = Path(root).resolve()
        self.config = config or load_config(self.root)
        assistance = self.config.get('assistance') or {}
        forced = 'en' if assistance.get('english_only') else assistance.get('speak_to_everyone_in')
        self.everyone_language = forced if forced in LANGUAGES else None
        if map_id not in self.config['maps']:
            raise OfficeConfigError(f'Office tasks are not configured for the map {map_id}.')
        self.map_id = map_id
        profile = registry.COMMAND_MAPS[map_id]
        entry = self.config['maps'][map_id]
        self.places_path = _inside(self.root, profile['locations'], 'Named places file')
        self.tags_path = _inside(self.root, profile['tags'], 'People and desk tags file')
        self.language_path = _inside(self.root, entry['language'], 'Language names file')
        self.capability_path = _inside(self.root, entry['capability'], 'Robot capability file')
        self.approval_key = profile['approval']
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        from tools.amr_task.tags import Tags
        try:
            self.tags = Tags(self.tags_path)
            self.static_check = self.tags.validate()
        except (ValueError, KeyError, OSError) as exc:
            raise OfficeConfigError(f'The people and desk tags failed the map check: {exc}') from exc
        try:
            self.language = yaml.safe_load(self.language_path.read_text(encoding='utf-8')) or {}
            self.capability = yaml.safe_load(self.capability_path.read_text(encoding='utf-8')) or {}
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise OfficeConfigError(f'An office-task file could not be read: {exc}') from exc
        if not isinstance(self.language, dict) or not isinstance(self.capability, dict):
            raise OfficeConfigError('The language names and capability files must be mappings.')
        self.fingerprint = self._fingerprint()

    # -- approvals ---------------------------------------------------------------------------
    def _fingerprint(self):
        files = [self.places_path, self.tags_path, self.language_path, self.capability_path,
                 self.root / 'tools/amr_console/approvals.yaml', *self.tags.fingerprints().values()]
        h = hashlib.sha256()
        for item in files:
            h.update(str(item).encode() if not isinstance(item, Path) else _digest(item).encode())
        return h.hexdigest()

    def changed(self):
        """True when an approved file changed since this context was built."""
        try:
            return self._fingerprint() != self.fingerprint
        except OSError:
            return True

    def motion_blocks(self):
        """Owner approvals that real robot motion needs. Empty means approved."""
        reasons = []
        approvals = registry.read_approvals(self.root / 'tools/amr_console/approvals.yaml')
        if approvals.get('error'):
            reasons.append(approvals['error'])
        elif not approvals.get(self.approval_key):
            reasons.append(f'Set {self.approval_key}: true in tools/amr_console/approvals.yaml.')
        if not registry.places_confirmed(self.root, self.map_id):
            reasons.append('Confirm the named places (approval_status: confirmed in the places file).')
        if self.tags.data.get('approval_status') != 'confirmed':
            reasons.append('Confirm the people and desk tags (approval_status: confirmed).')
        problem = capability_status(self.capability)
        if problem:
            reasons.append(problem + f' File: {self.capability_path.relative_to(self.root)}.')
        return reasons

    # -- names for the language module -----------------------------------------------------
    def _overlay(self, group, key):
        entry = (self.language.get(group) or {}).get(key) or {}
        return entry if isinstance(entry, dict) else {}

    def names(self, requester_place=None):
        """The approved roster in the language module's names format."""
        data = copy.deepcopy(self.tags.data)
        names = {key: data[key] for key in ('schema_version', 'frame_id', 'map_id', 'robot',
                                            'minimum_clearance_m') if key in data}
        names['approval_status'] = 'confirmed' if data.get('approval_status') == 'confirmed' else 'proposed'
        names['map_id'] = self.map_id
        reachable = {key.split(':', 1)[1] for key, value in self.static_check['poses'].items()
                     if value.get('reachable')}
        for group, key_field in (('rooms', 'name'), ('seats', 'id'), ('objects', 'name')):
            rows = []
            for row in data.get(group, []):
                row = dict(row)
                overlay = self._overlay(group, row[key_field])
                if overlay.get('aliases_by_language'):
                    row['aliases_by_language'] = copy.deepcopy(overlay['aliases_by_language'])
                row['on_map'] = True
                row['reachable'] = row[key_field] in reachable
                rows.append(row)
            names[group] = rows
        people = []
        for row in data.get('people', []):
            row = dict(row)
            overlay = self._overlay('people', row['name'])
            if overlay.get('aliases_by_language'):
                row['aliases_by_language'] = copy.deepcopy(overlay['aliases_by_language'])
            if overlay.get('language') in LANGUAGES:
                row['language'] = overlay['language']
            # One language for everyone (config: assistance.speak_to_everyone_in), whatever their own is.
            if self.everyone_language:
                row['language'] = self.everyone_language
            row['usual_place'] = row['seat']
            people.append(row)
        names['people'] = people
        if requester_place is not None:
            self.place(requester_place)
            names['requester_place'] = requester_place
        return names

    # -- places and people -----------------------------------------------------------------
    def place(self, key):
        """The approved stop pose for a place key (room name, desk id or object name)."""
        if not isinstance(key, str):
            raise OfficeConfigError('A stop must be named by its place key.')
        for kind, items in (('room', self.tags.rooms), ('seat', self.tags.seats), ('object', self.tags.objects)):
            if key in items:
                destination = self.tags.destination(kind, key)
                return {'key': key, 'kind': kind, 'label': self.label(key),
                        'x': destination.x, 'y': destination.y, 'yaw': destination.yaw,
                        'room': destination.room}
        raise OfficeConfigError(f'{key} is not an approved stop on the {self.map_id} map.')

    def label(self, key):
        if key in self.tags.seats:
            person = self.tags.occupant(key)
            return (f"{person}'s desk" if person else key.replace('_', ' '))
        return key.replace('_', ' ')

    def places(self):
        """Places a requester can say they are at: rooms first, then each person's desk."""
        result = [{'key': key, 'label': self.label(key), 'kind': 'room'} for key in self.tags.rooms]
        result += [{'key': row['seat'], 'label': self.label(row['seat']), 'kind': 'desk'}
                   for row in self.tags.people.values()]
        return result

    def requesters(self):
        operator = self.config['operator']
        result = [{'id': operator['name'], 'name': operator['name'], 'kind': 'operator',
                   'language': operator.get('language', 'en'), 'place': operator.get('place', 'home')}]
        for name, row in self.tags.people.items():
            overlay = self._overlay('people', name)
            result.append({'id': name, 'name': name, 'kind': 'person',
                           'language': self.everyone_language or (overlay.get('language') if overlay.get('language') in LANGUAGES else 'en'),
                           'place': row['seat']})
        return result

    def requester(self, requester_id):
        for row in self.requesters():
            if row['id'] == requester_id:
                return row
        raise OfficeConfigError('Choose who is asking from the list.')

    def summary(self):
        return {'map_id': self.map_id, 'places': self.places(), 'requesters': self.requesters(),
                'languages': [{'code': code, 'name': LANGUAGE_NAMES[code]} for code in LANGUAGES],
                'capability_file': str(self.capability_path.relative_to(self.root)),
                'language_file': str(self.language_path.relative_to(self.root)),
                'native_review': self.language.get('review_status', 'needs native-speaker check')}

    def worker_capability(self, rehearsal=False):
        """The capability profile the language worker uses.

        A real robot needs the owner-confirmed file. A text rehearsal (no motion) may use the
        proposed values; they are marked so that they can never authorise motion.
        """
        value = copy.deepcopy(self.capability)
        if rehearsal and value.get('approval_status') != 'confirmed':
            value['approval_status'] = 'confirmed'
            value['rehearsal_only_unconfirmed'] = True
        value.pop('notes', None)
        return value


def describe(context):
    return json.dumps(context.summary(), indent=2, ensure_ascii=False)


def main(argv=None):
    """Show the office context, or write the approved roster for the owner's learning review."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument('--map', default=None, help='office map id (default: the first in config/office_tasks.yaml)')
    parser.add_argument('--write-names', type=Path, metavar='FILE',
                        help='write the names file the language module review script expects')
    args = parser.parse_args(argv)
    config = load_config(ROOT)
    context = OfficeContext(ROOT, args.map or next(iter(config['maps'])), config=config)
    if args.write_names:
        if args.write_names.exists():
            parser.error(f'{args.write_names} exists; choose a new file name.')
        args.write_names.write_text(yaml.safe_dump(context.names(), allow_unicode=True, sort_keys=False),
                                    encoding='utf-8')
        print(f'Wrote {args.write_names} from the approved {context.map_id} files.')
    else:
        print(describe(context))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
