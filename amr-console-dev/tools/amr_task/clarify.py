"""Per-slot probabilities, maximum-slot calibration and single-candidate confirmation.

The maximum error calibrates simultaneous inclusion of the scored truth slots.
No statistical guarantee is made for real input or physical execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from tools.grounding_calibration.grounding_calibration.registry import Entry
from tools.grounding_calibration.grounding_calibration.token_scorer import TokenScorer
from .grammar import Grammar, Frame, edit_distance, normalize
from .tags import Destination

NONE = '<none>'
EPS = 1e-12


def softmax(scores, temperature=.1):
    peak = max(scores.values())
    weights = {k: math.exp((v-peak)/temperature) for k, v in scores.items()}
    total = sum(weights.values())
    return {k: v/total for k, v in weights.items()}


def prediction_set(probabilities, q_hat):
    if not math.isfinite(q_hat) or not 0 <= q_hat <= 1:
        raise ValueError('q_hat must be finite and in [0, 1]')
    return tuple(k for k, p in probabilities.items() if 1-p <= q_hat+EPS)


def calibration_score(slots, truth, aggregation='max'):
    if not truth or not set(truth) <= set(slots):
        raise ValueError('Need truth for the scored slots')
    errors = [1-slots[slot].get(label, 0.) for slot, label in truth.items()]
    if aggregation not in ('min', 'max'):
        raise ValueError('Unknown slot aggregation')
    return (min if aggregation == 'min' else max)(errors)


def calibrate(rows, alpha=.1, aggregation='max'):
    if not rows or not 0 < alpha < 1:
        raise ValueError('Need calibration examples and alpha in (0, 1)')
    scores = sorted(calibration_score(slots, truth, aggregation) for slots, truth in rows)
    k = math.ceil((len(scores)+1)*(1-alpha))
    return 1. if k > len(scores) else scores[k-1]


@dataclass(frozen=True)
class Decision:
    kind: str  # ask | confirm | reject
    question: str
    slot: str | None = None
    options: tuple[str, ...] = ()
    destination: Destination | None = None
    slots: dict = field(default_factory=dict)


class Grounder:
    def __init__(self, tags):
        self.tags = tags
        self.grammar = Grammar(tags)
        entries = [Entry(k, k.replace('_', ' '), tuple(r['aliases'])) for k, r in tags.rooms.items()]
        self.room_scorer = TokenScorer(entries, temperature=.1)
        self.room_keys = {e.name: e.key for e in entries}

    def person_probabilities(self, query):
        query = normalize(query)
        scores = {}
        for name in self.tags.people:
            normalized = normalize(name)
            distance = edit_distance(query, normalized)
            if distance == 0 or (len(normalized) >= 4 and distance <= 1):
                scores[name] = 1-distance/max(len(query), len(normalized))
        # Hard admissibility is <=1 edit; softmax is normalized over admitted names.
        return softmax(scores) if scores else {NONE: 1.}

    def unknown_place(self, text):
        """True when a named place has no word near any tagged room name or alias.

        Out-of-roster gate, applied before the conformal step: conformal prediction covers
        in-distribution commands only, so a place that is not on the map is refused, never asked
        about. Words shared by several names or aliases ('office', 'room') and articles are not evidence
        either way; a phrase made only of them is left to the calibrated question. A distinctive
        word counts as known within 1 edit (2 for words of 8+ letters), as for people's names.
        """
        words = [w for w in normalize(text).split() if w]
        counts = {}  # in how many room names/aliases each word occurs
        for name, item in self.tags.rooms.items():
            for alias in {normalize(a) for a in [name, *item['aliases']]}:
                for word in set(alias.split()):
                    counts[word] = counts.get(word, 0) + 1
        generic = {w for w, n in counts.items() if n > 1} | {'the', 'a', 'an', 'my', 'our', 'this', 'that'}
        distinctive = [w for w in words if w not in generic]
        if not distinctive:
            return False

        def near(word, known):
            limit = 0 if len(word) < 4 else (1 if len(word) < 8 else 2)
            return edit_distance(word, known) <= limit
        if any(near(word, known) for word in distinctive for known in counts):
            return False
        # Spacing variants: "store room" is the alias "storeroom".
        joined = ''.join(w for w in words if w not in {'the', 'a', 'an', 'my', 'our', 'this', 'that'})
        aliases = {normalize(a).replace(' ', '') for name, item in self.tags.rooms.items()
                   for a in [name, *item['aliases']]}
        return not any(near(joined, alias) for alias in aliases)

    def score(self, frame, choices=None):
        choices = choices or {}
        if frame.error:
            return {'validity': {NONE: 1.}}
        slots = {}
        if frame.room:
            slots['room'] = {self.room_keys.get(k, k): v for k, v in self.room_scorer.probabilities(frame.room).items()}
        if frame.target_kind == 'person':
            slots['person'] = self.person_probabilities(frame.target)
        elif frame.target_kind == 'role':
            names = [k for k, p in self.tags.people.items() if p['role'] == frame.target]
            slots['person'] = {k: 1/len(names) for k in names} if names else {NONE: 1.}
        elif frame.target_kind == 'object':
            slots['object'] = {frame.target: 1.}
        # Unique person/object implies its room. No extra room nonconformity is
        # fabricated for an unspoken slot: calibration uses spoken slots only.
        if frame.target_kind == 'seat':
            room = choices.get('room') or self.grammar.rooms.get(normalize(frame.room or ''))
            seats = [s for s in self.tags.seats.values()
                     if (room is None or s['room'] == room)
                     and (frame.row is None or s['row'] == frame.row)
                     and (frame.col is None or s['col'] == frame.col)]
            slots['seat'] = {s['id']: 1/len(seats) for s in seats} if seats else {NONE: 1.}
        return slots or {'room': {NONE: 1.}}

    def decide(self, frame, q_hat, choices=None):
        choices = choices or {}
        slots = self.score(frame, choices)
        if frame.error:
            return Decision('reject', frame.error, slots=slots)
        if frame.room and 'room' not in choices and 'room' in slots and self.unknown_place(frame.room):
            # Only where the calibrated set would otherwise offer a real room: a place the scorer
            # already refuses keeps its existing answer (and the saved pilots stay reproducible).
            options = prediction_set(slots['room'], q_hat)
            if options and options != (NONE,):
                return Decision('reject', f"I don't know a place called {normalize(frame.room)}; no goal was sent.",
                                'room', slots=slots)
        # A spelling-corrected room constrains the seat catalogue too. Do not
        # accidentally offer row 1 seat 1 from every room for "open ofice".
        if frame.target_kind == 'seat' and 'room' in slots and 'room' not in choices:
            rooms = prediction_set(slots['room'], q_hat)
            if len(rooms) == 1 and rooms[0] != NONE:
                slots = self.score(frame, dict(choices, room=rooms[0]))
                seat_room = rooms[0]
            else:
                seat_room = None
        else:
            seat_room = None
        # Seat validity/missing-slot questions must not be disguised as empty CP sets.
        if frame.target_kind == 'seat':
            room = choices.get('room') or seat_room or self.grammar.rooms.get(normalize(frame.room or ''))
            if room is not None:
                room_seats = [s for s in self.tags.seats.values() if s['room'] == room]
                rows = sorted({s['row'] for s in room_seats})
                if frame.row is not None and frame.row not in rows:
                    unit = 'row' if len(rows) == 1 else 'rows'
                    return Decision('reject', f"The {room.replace('_', ' ')} has only {len(rows)} {unit}", slots=slots)
                if slots['seat'] == {NONE: 1.}:
                    return Decision('reject', 'That seat does not exist in this room.', slots=slots)
        resolved, uncertain = {}, []
        for slot, probabilities in slots.items():
            if slot in choices:
                if choices[slot] not in probabilities:
                    return Decision('reject', 'That answer is outside the offered values.', slots=slots)
                resolved[slot] = choices[slot]
                continue
            missing_seat = slot == 'seat' and (frame.row is None or frame.col is None)
            options = tuple(probabilities) if missing_seat else prediction_set(probabilities, q_hat)
            if not options or options == (NONE,):
                if slot == 'person' and probabilities == {NONE: 1.}:
                    msg = f"I don't know anyone called {frame.target.title()}"
                else:
                    msg = f"I don't know the {slot} reliably. Please give a tagged {slot}."
                return Decision('reject', msg, slot, slots=slots)
            if len(options) == 1 and not missing_seat:
                resolved[slot] = options[0]
            else:
                uncertain.append((max(probabilities.values()), slot, options))
        if uncertain:
            _, slot, options = min(uncertain, key=lambda item: (item[0], item[1]))
            if slot == 'seat':
                prefix = f'Which seat in row {frame.row}' if frame.row is not None else 'Which seat (row and column)'
                names = [self.seat_label(k) for k in options]
            else:
                prefix = f'Which {slot}'
                names = [k.replace('_', ' ') for k in options]
            return Decision('ask', prefix + ': ' + ', '.join(names) + '?', slot, options, slots=slots)
        if 'person' in resolved:
            dest = self.tags.destination('person', resolved['person'])
        elif 'object' in resolved:
            dest = self.tags.destination('object', resolved['object'])
        elif 'seat' in resolved:
            dest = self.tags.destination('seat', resolved['seat'])
            occupant = self.tags.occupant(resolved['seat'])
            if occupant:
                dest = Destination(dest.key, dest.room, dest.x, dest.y, dest.yaw, occupant)
        else:
            dest = self.tags.destination('room', resolved['room'])
        room = resolved.get('room')
        if room and room != dest.room:
            # A conflict answer can only retain the person's/object's registered room.
            if choices.get('conflict') != dest.room:
                return Decision('ask', f"{dest.person or dest.key} is tagged in {dest.room.replace('_', ' ')}, "
                                f"but you named {room.replace('_', ' ')}. Use {dest.room.replace('_', ' ')} or cancel?",
                                'conflict', (dest.room,), slots=slots)
        label = dest.room.replace('_', ' ').capitalize()
        if dest.key.startswith('seat:'):
            seat = self.tags.seats[dest.key.split(':', 1)[1]]
            label += f", row {seat['row']} seat {seat['col']}"
            if dest.person:
                label += f' — {dest.person}'
        elif dest.key.startswith('object:'):
            label += ', ' + dest.key.split(':', 1)[1].replace('_', ' ')
        suffix = ' Deliver here, wait for acknowledgement, then return home?' if frame.action == 'deliver' else ' Go here?'
        return Decision('confirm', label + '.' + suffix + ' (yes/no)', destination=dest, slots=slots)

    def seat_label(self, key):
        seat = self.tags.seats[key]
        return f"{seat['room'].replace('_', ' ')} row {seat['row']} seat {seat['col']}"


class Conversation:
    """Answers select only offered values; selection is followed by a separate yes."""
    def __init__(self, grounder, q_hat):
        prediction_set({}, q_hat)
        self.grounder, self.q_hat = grounder, q_hat
        self.frame = None
        self.decision = None
        self.choices = {}
        self.confirmed = None

    def begin(self, text):
        self.frame = self.grounder.grammar.parse(text)
        self.choices, self.confirmed = {}, None
        self.decision = self.grounder.decide(self.frame, self.q_hat)
        return self.decision

    def answer(self, text):
        d = self.decision
        if d is None or d.kind not in ('ask', 'confirm'):
            raise ValueError('No pending question; start a new command')
        value = normalize(text)
        if value in ('no', 'n', 'cancel'):
            self.decision, self.confirmed = None, None
            return None
        if d.kind == 'confirm':
            if value in ('yes', 'y'):
                self.confirmed = (self.frame.action, d.destination)
                self.decision = None
                return self.confirmed
            return d
        matches = []
        for i, key in enumerate(d.options, 1):
            variants = {normalize(key), str(i)}
            if d.slot == 'room' or d.slot == 'conflict':
                variants |= {normalize(a) for a in self.grounder.tags.rooms.get(key, {}).get('aliases', [])}
            if d.slot == 'seat' and key != NONE:
                seat = self.grounder.tags.seats[key]
                variants |= {normalize(self.grounder.seat_label(key)), f"row {seat['row']} seat {seat['col']}"}
                if self.frame.row == seat['row']:
                    variants.add(f"seat {seat['col']}")
            if value in variants:
                matches.append(key)
        if len(matches) != 1 or matches[0] == NONE:
            return d
        self.choices[d.slot] = matches[0]
        self.decision = self.grounder.decide(self.frame, self.q_hat, self.choices)
        return self.decision

    def consume_confirmation(self):
        result, self.confirmed = self.confirmed, None
        if result is None:
            raise ValueError('Explicit confirmation is required before motion')
        return result
