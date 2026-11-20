"""Unicode-preserving matching against the supplied approved roster only."""
from collections import defaultdict
import hashlib
import json
import re
import unicodedata

from .schema import valid_string

NONE = '<none>'


def normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().replace('_', ' ').split())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Names:
    def __init__(self, raw):
        if not isinstance(raw, dict) or raw.get('approval_status') != 'confirmed':
            raise ValueError('Approved names must be confirmed')
        self.raw = raw
        self.entries, self.aliases = {}, defaultdict(lambda: defaultdict(set))
        self.groups = {}
        for group in ('rooms', 'people', 'objects', 'seats'):
            rows = raw.get(group, [])
            if not isinstance(rows, list) or (group == 'rooms' and not rows):
                raise ValueError('Invalid roster group')
            self.groups[group] = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Invalid roster row')
                key = row.get('id' if group == 'seats' else 'name')
                if not valid_string(key, 100) or key == NONE or key in self.entries:
                    raise ValueError('Invalid or duplicate approved ID')
                self.entries[key] = row
                self.groups[group][key] = row
                aliases = row.get('aliases', [])
                localized = row.get('aliases_by_language', {})
                if (not isinstance(aliases, list) or not isinstance(localized, dict)
                        or any(lang not in ('en', 'ta', 'ml', 'te') for lang in localized)):
                    raise ValueError('Invalid aliases')
                all_aliases = [key, *aliases]
                for values in localized.values():
                    if not isinstance(values, list):
                        raise ValueError('Invalid language aliases')
                    all_aliases.extend(values)
                if group == 'people' and row.get('role'):
                    all_aliases.append(row['role'])
                for alias in all_aliases:
                    if not valid_string(alias, 100):
                        raise ValueError('Invalid approved alias')
                    self.aliases[group][normalize(alias)].add(key)
        for row in self.groups['seats'].values():
            if row.get('room') not in self.groups['rooms']:
                raise ValueError('Seat room does not exist')
        for row in self.groups['people'].values():
            if row.get('seat') not in self.groups['seats']:
                raise ValueError('Person seat does not exist')
        for row in self.groups['objects'].values():
            if row.get('room') not in self.groups['rooms']:
                raise ValueError('Object room does not exist')

    def match(self, slot, said):
        if said is None:
            return (NONE,)
        query = normalize(said)
        groups = {'place': ('rooms', 'seats'), 'person': ('people',), 'object': ('objects',)}[slot]
        matches = set()
        for group in groups:
            matches.update(self.aliases[group].get(query, ()))
        if matches:
            return tuple(sorted(matches))
        # Only shared full words can form a generic room ambiguity (e.g. office).
        # A distinctive unapproved phrase such as "server room" never fuzzy-matches.
        if slot == 'place':
            for alias, keys in self.aliases['rooms'].items():
                if set(query.split()) <= set(alias.split()):
                    matches.update(keys)
            if len(matches) > 1:
                return tuple(sorted(matches))
        return (NONE,)

    def room(self, slot, key):
        if slot == 'place' and key in self.groups['rooms']:
            return key
        if slot == 'person':
            return self.groups['seats'][self.entries[key]['seat']]['room']
        return self.entries[key].get('room')

    def prompt_roster(self):
        """No map paths, coordinates, annotation notes, or executable capabilities."""
        return {group: [{'id': key, 'aliases': row.get('aliases', []),
                         'aliases_by_language': row.get('aliases_by_language', {}),
                         **({'role': row['role']} if group == 'people' and row.get('role') else {})}
                        for key, row in rows.items()] for group, rows in self.groups.items()}
