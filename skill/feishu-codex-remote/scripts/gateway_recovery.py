"""Durable recovery intent, separate from message processing ownership."""
from __future__ import annotations

import threading
from pathlib import Path
if __package__:
    from .gateway_primitives import atomic_write_json, load_json
else:
    from gateway_primitives import atomic_write_json, load_json


class CatchupRecovery:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.state = load_json(path, {"revision": 0, "pending": {}})
        self.group_locks: dict[str, threading.RLock] = {}

    def group_lock(self, key):
        with self.lock:
            return self.group_locks.setdefault(key, threading.RLock())

    def _commit(self, state):
        # Callers hold self.lock; publish before exposing the new memory state.
        atomic_write_json(self.path, state)
        self.state = state

    def request(self, keys):
        with self.lock:
            state = dict(self.state)
            revision = int(state.get("revision", 0)) + 1
            pending = dict(state.get("pending", {}))
            for key in keys:
                pending[key] = revision
            state.update(revision=revision, pending=pending)
            # Retain desired work in memory if publication fails. The caller
            # still receives the error; this new intent is not yet durable.
            self.state = state
            atomic_write_json(self.path, state)

    def pending(self):
        with self.lock:
            return dict(self.state.get("pending", {}))

    def complete(self, key, revision):
        # A new reconnect during an in-flight scan must retain its own intent.
        with self.lock:
            pending = self.state.get("pending", {})
            if key in pending and pending[key] == revision:
                state = dict(self.state)
                pending = dict(pending)
                del pending[key]
                state["pending"] = pending
                self._commit(state)

    def failed(self, key):
        with self.lock:
            if key not in self.state.get("pending", {}):
                self.request([key])
