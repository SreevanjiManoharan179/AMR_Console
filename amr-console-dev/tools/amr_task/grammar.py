"""Small, whole-command TASK grammar. No language model or arbitrary coordinates."""
from __future__ import annotations

from dataclasses import dataclass
import re

ORDINALS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5}
NUMBER = r'(?:first|second|third|fourth|fifth|[1-9][0-9]*(?:st|nd|rd|th)?)'


def normalize(text):
    return re.sub(r'\s+', ' ', text.lower().replace('_', ' ').strip(' .?!')).strip()


def number(value):
    return ORDINALS[value] if value in ORDINALS else int(re.match(r'\d+', value)[0])


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        nxt = [i]
        for j, cb in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (ca != cb)))
        row = nxt
    return row[-1]


@dataclass(frozen=True)
class Frame:
    action: str = 'go'
    room: str | None = None
    target_kind: str | None = None
    target: str | None = None
    row: int | None = None
    col: int | None = None
    error: str | None = None


class Grammar:
    def __init__(self, tags):
        self.tags = tags
        self.rooms = {normalize(alias): name for name, item in tags.rooms.items()
                      for alias in [name, *item['aliases']]}
        self.objects = {normalize(alias): name for name, item in tags.objects.items()
                        for alias in [name, *item['aliases']]}
        self.roles = {p['role'] for p in tags.people.values()}

    def parse(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            return Frame(error='Give one short typed task.')
        value = normalize(text)
        if re.search(r'\b(near|beside|behind|next to)\b', value):
            return Frame(error='I can deliver to rooms, seats, people or objects')
        if re.search(r"\b(not|no|never|dont|don.t|unless|if|maybe|or|then|before|after|and|except|without)\b", value):
            return Frame(error='Give one unconditional positive task; no goal was sent.')
        value = re.sub(r'^(?:please|kindly|could you|can you|would you|i want you to) ', '', value)
        value = re.sub(r' (?:please|now|for me|thanks|thank you)$', '', value)
        m = re.fullmatch(r'(go|navigate|head|move|return|deliver|take|bring|give)\s+(.*)', value)
        if not m:
            return Frame(error='Use “go to <place>” or “deliver to <person, seat or object>”.')
        verb, body = m.groups()
        action = 'go' if verb in ('go', 'navigate', 'head', 'move', 'return') else 'deliver'
        room = None
        combo = re.fullmatch(r'to (.+),\s*(?:give|deliver)(?: (?:this|it))? to (.+)', body)
        if combo:
            room, body = combo.groups()
            action = 'deliver'
        else:
            body = re.sub(r'^(?:this|it|the parcel|this parcel|the package|a package) ', '', body)
            if not body.startswith('to '):
                return Frame(error='Use “to” before the destination.')
            body = body[3:]
        if ' in ' in body:
            target, explicit_room = body.rsplit(' in ', 1)
            if room is not None:
                return Frame(error='Name the room once.')
            body, room = target, explicit_room
        # "deliver to the manager cabin, third row".
        if ',' in body:
            parts = body.split(',')
            if len(parts) != 2 or not re.search(r'\b(row|seat)\b', parts[1]):
                return Frame(error='Only one destination is supported.')
            if room is not None:
                return Frame(error='Name the room once.')
            room, body = parts[0], parts[1]
        body = re.sub(r'^the ', '', body.strip())
        room = re.sub(r'^the ', '', room.strip()) if room else None
        if room and not re.fullmatch(r'[a-z ]+', room):
            return Frame(error='Use a tagged room name.')
        if re.search(r'\b(row|seat|person)\b', body):
            # Tokenize slots and require every remaining word to be grammar filler.
            fields = {}
            rest = body
            for key, label in [('row', 'row'), ('col', '(?:seat|person)')]:
                pattern = rf'\b(?:({NUMBER}) {label}|{label} ({NUMBER}))\b'
                matches = list(re.finditer(pattern, rest))
                if len(matches) > 1:
                    return Frame(error='Give one row and one seat.')
                if matches:
                    match = matches[0]
                    fields[key] = number(next(v for v in match.groups() if v))
                    rest = rest[:match.start()] + ' ' + rest[match.end():]
            if re.sub(r'\b(?:the|at|in|to)\b|\s+', '', rest) or not fields:
                return Frame(error='Use “row 2 seat 3”, or “second row second person”.')
            return Frame(action, room, 'seat', row=fields.get('row'), col=fields.get('col'))
        if action == 'go':
            if room:
                return Frame(error='Name just one navigation destination.')
            # Explicit object navigation is allowed too.
            if body in self.objects:
                return Frame(action, None, 'object', self.objects[body])
            return Frame(action, body)
        if body in self.roles:
            return Frame(action, room, 'role', body)
        if body in self.objects:
            return Frame(action, room, 'object', self.objects[body])
        if body in self.rooms or (len(body.split()) > 1 and any(
                token in body.split() for token in ('office', 'hall', 'cabin', 'room', 'area'))):
            if room:
                return Frame(error='Name one delivery destination.')
            return Frame(action, body)
        if re.fullmatch(r'[a-z]+', body):
            return Frame(action, room, 'person', body)
        return Frame(error='I can deliver to rooms, seats, people or objects')
