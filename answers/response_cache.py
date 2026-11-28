"""Small process-local cache tied to the actual database snapshot and model."""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import Lock
import time


class AnswerCache:
    # The key includes the database file's identity, so a data refresh (or a
    # model change) makes every old entry unreachable. Within one snapshot an
    # answer is kept for a day: a repeat question is answered instantly.
    def __init__(self, ttl=86400, capacity=256):
        self.ttl, self.capacity = ttl, capacity
        self.entries = OrderedDict()
        self.lock = Lock()

    def key(self, database, model, question):
        stat = Path(database).stat()
        return (str(Path(database).resolve()), stat.st_ino, stat.st_size,
                stat.st_mtime_ns, model, question.strip())

    def get(self, key):
        with self.lock:
            item = self.entries.get(key)
            if item is None:
                return None
            expires, payload = item
            if time.monotonic() >= expires:
                del self.entries[key]
                return None
            self.entries.move_to_end(key)
            return deepcopy(payload)

    def put(self, key, payload):
        # Never retain configuration errors, model failures or empty answers.
        if not payload.get('rows') or not payload.get('sql') or 'coverage' in payload:
            return
        with self.lock:
            self.entries[key] = (time.monotonic() + self.ttl, deepcopy(payload))
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
