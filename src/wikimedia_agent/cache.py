"""A small TTL cache for API responses (§2.1).

Cuts latency and load on donated infrastructure, and makes repeated retrievals
within a session free. The clock is injectable so expiry is testable without
sleeping (principle #14).
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Callable

DEFAULT_TTL = 900.0
"""Fifteen minutes. Articles change, so cached text is not kept indefinitely."""

DEFAULT_MAX_ENTRIES = 256


def cache_key(url: str, params: Mapping[str, Any]) -> str:
    """A stable key for an endpoint plus its parameters.

    Parameters are sorted so that two callers passing the same arguments in a
    different order share a cache entry rather than silently missing.
    """
    normalized = {str(key): str(value) for key, value in params.items()}
    return url + "?" + json.dumps(normalized, sort_keys=True, separators=(",", ":"))


class ResponseCache:
    """Bounded, TTL-expiring, least-recently-used cache."""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._monotonic = monotonic
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None

        stored_at, value = entry
        if self._monotonic() - stored_at >= self.ttl:
            del self._entries[key]
            self.misses += 1
            return None

        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: dict[str, Any]) -> None:
        if self.ttl <= 0:
            return
        self._entries[key] = (self._monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
