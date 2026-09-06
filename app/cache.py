import asyncio
from copy import deepcopy
from time import monotonic


class SnapshotCache:
    """Coalesce concurrent dashboard reads; never used to authorize a trade."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._value = None
        self._expires = 0.0
        self._generation = 0
        self._lock = asyncio.Lock()

    def invalidate(self):
        self._expires = 0.0
        self._generation += 1

    async def get(self, build):
        async with self._lock:
            if self._value is not None and monotonic() < self._expires:
                return deepcopy(self._value)
            generation = self._generation
            value = await build()
            if generation == self._generation:
                self._value = deepcopy(value)
                self._expires = monotonic() + self.ttl
            return value
