"""Persistent spatial memory for task outcomes: one small JSON file per map. No ROS.

For each destination key (a person, seat, room or object) it keeps the last task outcome, when it
happened and in which task session. The runner reads it before asking for confirmation, so a person
recorded "not here" a few minutes ago is mentioned before the robot is sent again. The file survives
restarts, is plain JSON (readable from the console) and is written atomically.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import time

SCHEMA = 1
NOT_FOUND = ('RETURNED_NOT_HERE', 'RETURNED_TIMEOUT')
# Only delivery outcomes say something about the person; a plain 'go' or a failed trip does not.
REMEMBERED = ('DELIVERED', *NOT_FOUND)


def destination_key(destination):
    """'person:<name>' for a delivery to a person, else the tag key ('room:...', 'seat:...', ...)."""
    return f'person:{destination.person}' if destination.person else destination.key


class SpatialMemory:
    def __init__(self, path, map_id, clock=time.time):
        self.path, self.map_id, self.clock = Path(path), map_id, clock
        self.data = {'schema': SCHEMA, 'map_id': map_id, 'entries': {}}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or data.get('schema') != SCHEMA or data.get('map_id') != map_id
                    or not isinstance(data.get('entries'), dict)):
                raise ValueError(f'{self.path} is a memory file for another map or format')
            self.data = data

    @property
    def entries(self):
        return self.data['entries']

    def record(self, destination, outcome, run_id):
        key = destination_key(destination)
        now = self.clock()
        self.entries[key] = {'outcome': outcome, 'epoch_s': now, 'run_id': run_id,
                             'time_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='seconds'),
                             'room': destination.room, 'tag': destination.key}
        self._save()
        return key, self.entries[key]

    def recent(self, destination, within_s):
        entry = self.entries.get(destination_key(destination))
        # A malformed entry (hand-edited or truncated file) counts as no memory, never as an error.
        if (not isinstance(entry, dict) or not isinstance(entry.get('outcome'), str)
                or not isinstance(entry.get('epoch_s'), (int, float)) or not math.isfinite(entry['epoch_s'])
                or self.clock() - entry['epoch_s'] > within_s):
            return None
        return entry

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.' + self.path.name, suffix='.tmp')
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(self.data, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
