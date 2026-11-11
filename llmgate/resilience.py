"""Circuit breaker and token-bucket rate limiter. Both take an injectable clock for tests."""
from __future__ import annotations
import time


class CircuitBreaker:
    """closed -> open after `threshold` consecutive failures; after `cooldown` s one
    probe is allowed (half-open); success closes it, failure re-opens it."""

    def __init__(self, threshold=3, cooldown=10.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures, self.opened_at, self.probing = 0, None, False

    @property
    def state(self):
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and not self.probing:
            self.probing = True
            return True
        return False

    def success(self):
        self.failures, self.opened_at, self.probing = 0, None, False

    def failure(self):
        self.failures += 1
        self.probing = False
        if self.failures >= self.threshold or self.opened_at is not None:
            self.opened_at = self.clock()


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate_per_sec, burst, clock
        self.tokens, self.last = burst, clock()

    def take(self, n=1.0):
        """Return (allowed, retry_after_seconds)."""
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True, 0.0
        return False, (n - self.tokens) / self.rate

    def charge(self, n):
        """Debit n tokens without gating (post-hoc settlement of actual usage).
        A negative n credits an admission over-estimate back. The balance may go
        negative after a debit; a credit never pushes it past burst capacity."""
        now = self.clock()
        self.tokens = min(self.burst, min(self.burst, self.tokens + (now - self.last) * self.rate) - n)
        self.last = now


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self.buckets: dict[str, TokenBucket] = {}
        self.clock = clock

    def bucket(self, key: str, rate: float, burst: float) -> TokenBucket:
        b = self.buckets.get((key, rate, burst))
        if b is None:
            b = self.buckets[(key, rate, burst)] = TokenBucket(rate, burst, self.clock)
        return b

    def check(self, key: str, rate: float, burst: float, n=1.0):
        return self.bucket(key, rate, burst).take(n)
