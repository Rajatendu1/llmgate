"""Structured request logging and aggregate metrics.

Every HTTP request passes through middleware that records method, path,
status and duration, assigns or echoes an X-Request-ID, and emits one JSON
line on the ``actas.requests`` logger so an operator can ship it wherever
their logs live.

GET /metrics exposes aggregate counters only: no per-client data, no token
material, no request bodies, no query strings. Counters are per-process; on
a multi-instance deployment each instance reports its own numbers.

Honest limits: this is operational telemetry, not the security audit log
(see audit.py). Counts reset on restart, and the path map is capped so
scanner noise on a public deployment cannot grow memory without bound.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque

logger = logging.getLogger("actas.requests")

# Latency histogram bucket boundaries in milliseconds.
BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500)

# Distinct paths kept before unknown ones fold into "other". The routes in
# this app are a fixed set, so anything past the cap is probing noise.
MAX_TRACKED_PATHS = 64


class Metrics:
    """In-process aggregate counters. Thread-safe, bounded, restart-reset."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._total = 0
        self._by_path: dict[str, dict[str, int]] = {}
        self._latency = [0] * (len(BUCKETS_MS) + 1)
        self._recent: deque[dict] = deque(maxlen=256)

    def record(self, method: str, path: str, status: int, duration_ms: float,
               request_id: str) -> dict:
        """Record one request and return its structured log entry."""
        entry = {
            "ts": round(time.time(), 3),
            "request_id": request_id,
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round(duration_ms, 2),
        }
        status_class = f"{status // 100}xx"
        with self._lock:
            self._total += 1
            if path not in self._by_path and len(self._by_path) >= MAX_TRACKED_PATHS:
                path = "other"
            bucket = self._by_path.setdefault(path, {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0})
            bucket[status_class] = bucket.get(status_class, 0) + 1
            idx = next((i for i, b in enumerate(BUCKETS_MS) if duration_ms < b), len(BUCKETS_MS))
            self._latency[idx] += 1
            self._recent.append(entry)
        return entry

    def snapshot(self) -> dict:
        with self._lock:
            by_path = {p: dict(c) for p, c in self._by_path.items()}
            latency = list(self._latency)
            total = self._total
        bounds = list(BUCKETS_MS) + [None]
        histogram = {
            (f"<{b}ms" if b is not None else f">={BUCKETS_MS[-1]}ms"): count
            for b, count in zip(bounds, latency)
        }
        return {
            "uptime_seconds": round(time.time() - self._started, 1),
            "requests_total": total,
            "by_path": by_path,
            "latency_ms_histogram": histogram,
            "note": "aggregate per-process counters; reset on restart",
        }

    def recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            return list(self._recent)[-n:]


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def clean_request_id(value: str | None) -> str:
    """Echo an inbound X-Request-ID if it is short and printable."""
    if value and 0 < len(value) <= 64 and all(32 < ord(c) < 127 for c in value):
        return value
    return new_request_id()


def log_line(entry: dict) -> None:
    logger.info(json.dumps(entry, sort_keys=True))
