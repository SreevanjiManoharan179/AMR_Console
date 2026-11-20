"""Tick-driven delivery state machine, with an injected navigator and wall clock.

No ROS imports. Every transition that matters is recorded before motion or
announcement. Return outcomes are final only after a checked home arrival.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Protocol

from dataclasses import replace

from .clarify import Conversation, Decision
from .memory import NOT_FOUND, REMEMBERED, destination_key
from .tags import Destination


@dataclass(frozen=True)
class NavigationResult:
    status: str
    x: float
    y: float
    pose_source: str = 'stand-in'
    # Simulation only: the simulator's own pose of the robot in the map frame at the same moment
    # (Gazebo truth). Absent on a real robot; acceptance never depends on it.
    truth_x: float | None = None
    truth_y: float | None = None
    truth_source: str | None = None


class Navigator(Protocol):
    def start(self, destination: Destination) -> None: ...
    def poll(self) -> NavigationResult | None: ...
    def cancel(self) -> None: ...


class EventLog:
    def __init__(self, session_dir, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.path = Path(session_dir) / 'events.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=False)
        self.stream = self.path.open('x', encoding='utf-8')
        self.sequence = 0

    def emit(self, type_, **fields):
        self.sequence += 1
        record = {'seq': self.sequence, 'timestamp': datetime.now(timezone.utc).isoformat(),
                  'elapsed_s': self.clock()-self.start, 'type': type_, **fields}
        self.stream.write(json.dumps(record, allow_nan=False, sort_keys=True) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return record

    def close(self):
        self.stream.close()


class Executor:
    TERMINAL = {'DONE', 'FAILED', 'CANCELLED'}

    def __init__(self, conversation: Conversation, navigator: Navigator, log: EventLog,
                 *, clock=time.monotonic, before_goal=lambda: None, announce=lambda text: None,
                 navigation_timeout=180., memory=None, run_id=None, memory_window_s=1800.):
        self.conversation, self.navigator, self.log = conversation, navigator, log
        self.memory, self.run_id, self.memory_window_s = memory, run_id, memory_window_s
        self.clock, self.before_goal, self.speak = clock, before_goal, announce
        self.navigation_timeout = navigation_timeout
        if not math.isfinite(navigation_timeout) or navigation_timeout <= 0:
            raise ValueError('Navigation timeout must be positive and finite')
        self.cancellation_uncertain = False
        self.state = 'GROUND'
        self.action = self.destination = self.pending_outcome = self.outcome = None
        self.active = False
        self.started = None
        self.announcements = 0
        self.wait_started = None

    def _question(self, decision):
        if decision.kind == 'confirm' and self.memory is not None and decision.destination is not None:
            try:
                entry = self.memory.recent(decision.destination, self.memory_window_s)
                if entry and entry['outcome'] in NOT_FOUND:
                    who = decision.destination.person or decision.destination.key.split(':', 1)[-1].replace('_', ' ')
                    minutes = max(0, round((self.memory.clock() - entry['epoch_s']) / 60))
                    why = 'not here' if entry['outcome'] == 'RETURNED_NOT_HERE' else 'no answer'
                    note = f'Last time ({minutes} min ago) {who}: {why}. Try again? '
                    self.log.emit('memory', key=destination_key(decision.destination),
                                  outcome=entry['outcome'], time_utc=entry.get('time_utc'),
                                  run_id=entry.get('run_id'), said=note)
                    decision = replace(decision, question=note + decision.question)
            except Exception as exc:  # memory is advisory; it never blocks or alters a task
                self.log.emit('memory_error', error=repr(exc)[:300])
        if decision.kind == 'reject':
            self.state = 'GROUND'
        else:
            self.state = 'CONFIRM' if decision.kind == 'confirm' else 'GROUND'
        self.log.emit('question', state=self.state, kind=decision.kind, text=decision.question,
                      slot=decision.slot, options=list(decision.options), probabilities=decision.slots)
        return decision.question

    def command(self, text):
        if self.cancellation_uncertain:
            raise RuntimeError('Cancellation is unconfirmed; no further task may start')
        if self.state not in ('GROUND', 'CONFIRM', *self.TERMINAL):
            raise ValueError('A task is active; acknowledge it or cancel first')
        self.log.emit('command', text=text, input='typed input, rule-based grammar', simulation=True)
        self.outcome = self.pending_outcome = None
        self.destination = self.action = None
        return self._question(self.conversation.begin(text))

    def answer(self, text):
        if self.state not in ('GROUND', 'CONFIRM'):
            raise ValueError('No grounding/confirmation question is pending')
        self.log.emit('answer', text=text, state=self.state)
        result = self.conversation.answer(text)
        if result is None:
            self.state = 'CANCELLED'
            self.outcome = 'CANCELLED'
            self.log.emit('outcome', outcome=self.outcome, returned_home=False)
            return 'Cancelled; no goal sent.'
        if isinstance(result, Decision):
            return self._question(result)
        self.action, self.destination = self.conversation.consume_confirmation()
        self.log.emit('confirm', action=self.action, destination=asdict(self.destination), answer=text)
        self._navigate(self.destination, returning=False)
        return self.state

    def _navigate(self, destination, *, returning):
        try:
            self.before_goal()
            self.current_goal = destination
            self.state = 'RETURNING' if returning else 'NAVIGATE'
            self.started = self.clock()
            self.log.emit('goal', state=self.state, destination=asdict(destination), returning=returning)
            self.active = True  # includes an uncertain dispatch; errors require cancellation
            self.navigator.start(destination)
        except Exception as exc:
            self._fail('NAVIGATION_ERROR', str(exc))

    def _fail(self, outcome, reason, remember=True):
        if self.active:
            try:
                self.navigator.cancel()
            except Exception as exc:
                self.cancellation_uncertain = True
                reason += f'; cancellation failed: {exc}'
        self.active = False
        self.state, self.outcome = 'FAILED', outcome
        self.log.emit('outcome', outcome=outcome, reason=reason, returned_home=False,
                      delivery_outcome=self.pending_outcome)
        if remember:
            self._remember(self.pending_outcome)

    def _remember(self, outcome):
        """Write a delivery outcome to the persistent memory; a memory fault never moves the robot."""
        if self.memory is None or self.destination is None or outcome not in REMEMBERED:
            return
        try:
            key, entry = self.memory.record(self.destination, outcome, self.run_id)
            self.log.emit('memory_write', key=key, entry=entry)
        except Exception as exc:
            self.log.emit('memory_error', error=str(exc))

    def _announce(self):
        person = self.destination.person
        text = f"I'm looking for {person}" if person else 'Delivery for ' + self.destination.key.replace('_', ' ')
        self.state = 'ANNOUNCE'
        self.announcements += 1
        self.log.emit('announce', text=text, number=self.announcements, state=self.state)
        self.speak(text)
        self.state = 'WAIT_ACK'

    def tick(self):
        if self.state in ('NAVIGATE', 'RETURNING'):
            returning = self.state == 'RETURNING'
            try:
                result = self.navigator.poll()
                if result is None:
                    if self.clock()-self.started >= self.navigation_timeout:
                        self._fail('RETURN_FAILED' if returning else 'NAVIGATION_FAILED', 'Navigation timeout')
                    return
                error = math.hypot(result.x-self.current_goal.x, result.y-self.current_goal.y)
                good = result.status == 'SUCCEEDED' and math.isfinite(error) and error <= .3
                truth_error = None
                if result.truth_x is not None and result.truth_y is not None:
                    truth_error = math.hypot(result.truth_x-self.current_goal.x, result.truth_y-self.current_goal.y)
                    truth_error = truth_error if math.isfinite(truth_error) else None
                self.log.emit('arrival', state='ARRIVED' if good else self.state,
                              result=asdict(result), error_m=error if math.isfinite(error) else None,
                              truth_error_m=truth_error, accepted=good, returning=returning)
                if not good:
                    self._fail('RETURN_FAILED' if returning else 'NAVIGATION_FAILED',
                               'Require Nav2 SUCCEEDED and finite map-pose error <= 0.3 m')
                    return
                self.active = False
                if returning:
                    self.state, self.outcome = 'DONE', self.pending_outcome
                    self.log.emit('return', phase='arrived', error_m=error, truth_error_m=truth_error)
                    self.log.emit('outcome', outcome=self.outcome, returned_home=True)
                    self._remember(self.outcome)
                elif self.action == 'go':
                    self.state, self.outcome = 'DONE', 'NAVIGATED'
                    self.log.emit('outcome', outcome=self.outcome, returned_home=False)
                    self._remember(self.outcome)
                else:
                    self.state = 'ARRIVED'
                    self.wait_started, self.announcements = self.clock(), 0
                    self._announce()
            except Exception as exc:
                self._fail('RETURN_FAILED' if returning else 'NAVIGATION_ERROR', str(exc))
        elif self.state == 'WAIT_ACK':
            elapsed = self.clock()-self.wait_started
            if elapsed >= 60:
                self.log.emit('ack', value='timeout', elapsed_s=elapsed)
                self._return('RETURNED_TIMEOUT')
            elif self.announcements < 3 and elapsed >= 15*self.announcements:
                self._announce()

    def acknowledge(self, value):
        if self.state != 'WAIT_ACK':
            raise ValueError('Acknowledgement is accepted only after checked arrival')
        value = value.strip().lower()
        if value not in ('taken', 'not here', 'wrong person'):
            raise ValueError('Type taken, not here, or wrong person')
        # At/after the deadline an acknowledgement cannot retroactively beat timeout.
        if self.clock()-self.wait_started >= 60:
            self.tick()
            return
        self.log.emit('ack', value=value)
        self._return('DELIVERED' if value == 'taken' else 'RETURNED_NOT_HERE')

    def _return(self, outcome):
        self.pending_outcome = outcome
        self.log.emit('return', phase='start', delivery_outcome=outcome)
        self._navigate(self.conversation.grounder.tags.destination('room', 'home'), returning=True)

    def cancel(self):
        # A cancel typed after a task has ended must not re-record (or refresh) its outcome.
        self._fail('CANCELLED', 'Operator cancelled; no automatic return goal',
                   remember=self.state not in self.TERMINAL)
        self.state = 'CANCELLED'
        self.conversation.confirmed = self.conversation.decision = None
