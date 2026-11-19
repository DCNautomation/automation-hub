"""A small in-memory request limiter (sliding window per key).

Used for POST /ingest and for the dashboard API, keyed by client address. Login has its own
stronger, database-backed lockout (auth.py) and is not handled here.

Known limit: the counters live in one API process. Run several API replicas and each keeps its
own count, so the real ceiling is the limit times the number of replicas. That is fine for a
single small service; a shared limiter (Redis, or the proxy in front) is the next step if it grows.
"""
import threading
import time
from collections import deque

from app import config

WINDOW_SECONDS = 60
MAX_KEYS = 50_000  # past this, idle keys are dropped so a flood of addresses can't grow memory forever


class RateLimiter:
    def __init__(self, limit_per_minute, clock=time.monotonic):
        self._limit = limit_per_minute  # a number, or a callable so tests and config changes are picked up
        self._clock = clock
        self._hits = {}
        self._lock = threading.Lock()

    def limit(self):
        return self._limit() if callable(self._limit) else self._limit

    def check(self, key):
        """(allowed, retry_after_seconds). A limit of 0 or less turns the limiter off."""
        limit = self.limit()
        if limit <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(WINDOW_SECONDS - (now - hits[0])) + 1)
            hits.append(now)
            if len(self._hits) > MAX_KEYS:
                self._drop_idle(now)
            return True, 0

    def _drop_idle(self, now):
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= WINDOW_SECONDS]:
            del self._hits[key]

    def reset(self):
        with self._lock:
            self._hits.clear()


ingest = RateLimiter(lambda: config.INGEST_RATE_PER_MINUTE)
reads = RateLimiter(lambda: config.READ_RATE_PER_MINUTE)


def reset_all():
    ingest.reset()
    reads.reset()
