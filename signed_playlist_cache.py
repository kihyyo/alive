"""Refresh signed HLS masters without blocking a still-valid live stream.

Only the master and its signed source URL are retained here. Media playlists
and segments continue to be fetched through ALive's normal playback path.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Condition, Thread
from time import monotonic


@dataclass
class RefreshValue:
    value: dict
    valid_for: float
    refresh_after: float = 15.0


@dataclass
class _Entry:
    value: dict | None = None
    expires: float = 0.0
    refresh_at: float = 0.0
    busy: bool = False
    error: Exception | None = None


class SignedPlaylistCache:
    def __init__(self, loader, *, capacity=32, retry_delay=3.0,
                 wait_timeout=10.0, clock=monotonic, on_error=None):
        self.loader = loader
        self.capacity = capacity
        self.retry_delay = retry_delay
        self.wait_timeout = wait_timeout
        self.clock = clock
        self.on_error = on_error
        self._entries = OrderedDict()
        self._condition = Condition()

    def get(self, key):
        # The waiting deadline is real elapsed time, independent of the token
        # clock (which can also be controlled in expiry regression tests).
        deadline = monotonic() + self.wait_timeout
        with self._condition:
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.capacity:
                    victim = next((k for k, v in self._entries.items() if not v.busy), None)
                    if victim is None:
                        raise RuntimeError("Live playlist refresh capacity exhausted")
                    del self._entries[victim]
                entry = self._entries[key] = _Entry()
            self._entries.move_to_end(key)
            while True:
                now = self.clock()
                valid = entry.value is not None and now < entry.expires
                if not entry.busy and now >= entry.refresh_at:
                    entry.busy = True
                    worker = Thread(target=self._refresh, args=(key, entry),
                                    name="alive-signed-playlist", daemon=True)
                    try:
                        worker.start()
                    except Exception:
                        entry.busy = False
                        self._condition.notify_all()
                        raise
                if valid:
                    return entry.value
                if not entry.busy and entry.error is not None:
                    raise entry.error
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Live playlist refresh exceeded its wait deadline")
                self._condition.wait(remaining)

    def _refresh(self, key, entry):
        try:
            loaded = self.loader(key, entry.value)
            if loaded.valid_for <= 0:
                raise ValueError("Refreshed live playlist URL is already expired")
        except Exception as exc:
            with self._condition:
                entry.error = exc
                entry.refresh_at = self.clock() + self.retry_delay
                entry.busy = False
                self._condition.notify_all()
            if self.on_error is not None:
                self.on_error(key, exc)
        else:
            with self._condition:
                now = self.clock()
                entry.value = loaded.value
                entry.expires = now + loaded.valid_for
                entry.refresh_at = now + min(loaded.refresh_after, loaded.valid_for / 2)
                entry.error = None
                entry.busy = False
                self._condition.notify_all()
