"""In-process token-bucket rate limiter (single node; see docs for scaling notes)."""
import threading
import time
from collections import OrderedDict
from typing import Callable


class TokenBucketLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ):
        self._capacity = capacity
        self._refill = refill_per_sec
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (float(self._capacity), now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:  # bound memory: evict least-recently used
                self._buckets.popitem(last=False)
            return allowed
