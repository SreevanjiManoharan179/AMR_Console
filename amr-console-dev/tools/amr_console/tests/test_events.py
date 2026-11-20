"""SSE messages preserve one JSON data line and idle heartbeats."""
import json
import unittest

from events import EventStream, format_event, keepalive, stream_frames
from test_support import assert_guard_active


def setUpModule():
    assert_guard_active()


class FakeEventSource:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.waits = []

    def read(self, cursor=0, timeout=15):
        self.waits.append((cursor, timeout))
        return next(self.batches)


class EventsTests(unittest.TestCase):
    def test_json_is_one_data_line_even_when_text_has_newlines(self):
        event = {'type': 'log', 'text': 'line one\nline two', 'level': 'warn', 'ts': 1}
        frame = format_event(event, 12)
        self.assertTrue(frame.startswith('id: 12\ndata: '))
        self.assertTrue(frame.endswith('\n\n'))
        self.assertEqual(1, sum(line.startswith('data: ') for line in frame.splitlines()))
        line = next(line for line in frame.splitlines() if line.startswith('data: '))
        self.assertEqual(event, json.loads(line[len('data: '):]))

    def test_idle_uses_exact_15_second_keepalive_wait(self):
        source = FakeEventSource([[], [(4, {'type': 'state', 'status': {'state': 'idle'}})], []])
        frames = stream_frames(source)
        self.assertEqual(': keepalive\n\n', next(frames))
        self.assertIn('id: 4\n', next(frames))
        self.assertEqual(keepalive(), next(frames))
        self.assertEqual([(0, 15), (0, 15), (4, 15)], source.waits)

    def test_stream_detaches_producer_mutations_and_uses_cursor(self):
        events = EventStream()
        event = {'type': 'state', 'status': {'state': 'starting'}}
        sequence = events.publish(event)
        event['status']['state'] = 'changed'
        self.assertEqual([(1, {'type': 'state', 'status': {'state': 'starting'}})],
                         events.read(0, timeout=0))
        self.assertEqual([], events.read(sequence, timeout=0))

    def test_bounded_stream_keeps_newest_events(self):
        events = EventStream(limit=2)
        for index in range(3):
            events.publish({'type': 'log', 'text': str(index)})
        self.assertEqual([2, 3], [sequence for sequence, _ in events.read(timeout=0)])

    def test_nonfinite_payload_is_refused(self):
        with self.assertRaises(ValueError):
            format_event({'time_s': float('nan')})
        with self.assertRaises(ValueError):
            EventStream().publish({'time_s': float('inf')})
