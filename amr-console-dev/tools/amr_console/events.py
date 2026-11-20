"""Bounded thread-safe event stream; no ROS or process dependencies."""
import json
import threading
import time
from collections import deque


def format_event(event, event_id=None):
    prefix = f'id: {event_id}\n' if event_id is not None else ''
    return prefix + 'data: ' + json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n\n'


def keepalive():
    return ': keepalive\n\n'


class EventStream:
    def __init__(self, limit=2000):
        self.condition = threading.Condition()
        self.items = deque(maxlen=limit)
        self.sequence = 0

    def publish(self, event):
        # Make a detached JSON-safe snapshot before a producer changes its record.
        value = json.loads(json.dumps(event, allow_nan=False))
        with self.condition:
            self.sequence += 1
            self.items.append((self.sequence, value))
            self.condition.notify_all()
            return self.sequence

    def read(self, cursor=0, timeout=15):
        with self.condition:
            ready = [(n, event) for n, event in self.items if n > cursor]
            if not ready:
                self.condition.wait(timeout)
                ready = [(n, event) for n, event in self.items if n > cursor]
            return ready


def stream_frames(events, cursor=0, timeout=15):
    """One heartbeat per idle wait; tests can use an injected event source."""
    while True:
        batch = events.read(cursor, timeout=timeout)
        if not batch:
            yield keepalive()
        else:
            for sequence, event in batch:
                cursor = sequence
                yield format_event(event, sequence)
