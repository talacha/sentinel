"""A global sliding-window cap on reviews, to protect token and search credits on public demos."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class FailureThrottle:
    """Blocks a login after too many failures in a rolling window (brute-force protection).

    The block is global, which suits a rarely used admin login: an attacker can lock the admin
    out for a few minutes but cannot guess the password. It is not used for the visitor login,
    where a global lockout would let anyone on the internet lock legitimate visitors out."""

    def __init__(
        self,
        max_failures: int = 10,
        window: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window = window
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= self.window:
            self._stamps.popleft()

    def blocked_for(self) -> float | None:
        """Seconds until sign-ins are allowed again, or None if they are allowed now."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            if len(self._stamps) < self.max_failures:
                return None
            return max(self.window - (now - self._stamps[0]), 1.0)

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._stamps.append(now)

    def reset(self) -> None:
        with self._lock:
            self._stamps.clear()


class HourlyLimiter:
    """Allow at most `limit` acquisitions per rolling `window` seconds (0 disables the cap)."""

    def __init__(
        self,
        limit: int,
        window: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> float | None:
        """Take a slot and return None, or return the seconds until one frees up."""
        if self.limit <= 0:
            return None
        with self._lock:
            now = self._clock()
            while self._stamps and now - self._stamps[0] >= self.window:
                self._stamps.popleft()
            if len(self._stamps) >= self.limit:
                return max(self.window - (now - self._stamps[0]), 1.0)
            self._stamps.append(now)
            return None
