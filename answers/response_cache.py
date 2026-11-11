"""Small process-local cache tied to the actual database snapshot and model."""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import Lock
import time


class AnswerCache:
    def __init__(self, ttl=300, capacity=128):
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
