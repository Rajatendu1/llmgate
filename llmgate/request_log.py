"""Recent-request log: the last N served requests as secret-free events.

Powers the public "live request log" on the demo dashboard (GET /demo/events).
Every event is recorded from the same funnel as usage metering, so the log and
the counters always describe the same traffic.

Redaction contract - an event NEVER contains:
- prompt or completion content (only token counts),
- API key secrets (the key's public label only, e.g. "visitor"),
- client IPs or request headers,
- upstream error bodies beyond the already-capped status category.

Backends: a per-process ring buffer (single-process dev) or a capped Redis
list (multi-instance deployments, same Upstash REST transport as the limiter
and meter). Recording is fail-open like metering (observability, not
enforcement): a store error loses one log event and is counted in `errors`,
never fails a request. Reads degrade to a 503 at the endpoint, matching the
stats endpoint's "no partial answer presented as complete" rule.
"""
from __future__ import annotations
import json
import time
from collections import deque

import httpx

from .redislimit import store_credentials_from_env

MAX_EVENTS = 50  # ring capacity; the public endpoint serves the newest 20


class LogUnavailable(Exception):
    """The shared request-log store could not be reached or answered unexpectedly."""


def make_event(key: str, provider: str, model: str, usage: dict | None,
               status: str, latency_s: float | None, cost: float | None,
               mode: str, attempts: int, rid: str) -> dict:
    """One secret-free log event. `mode` is "simulated" or "real" - whether the
    serving provider is a mock or a live upstream - so the dashboard can label
    rows honestly instead of implying everything is a live model call."""
    u = usage or {}
    return {
        "ts": round(time.time(), 3),
        "rid": rid[:16],
        "key": key,
        "provider": provider,
        "model": model,
        "status": status,
        "latency_ms": round(latency_s * 1000, 1) if latency_s is not None else None,
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        # estimated USD from the config's list-price table; 0.0 on a free tier
        "cost_usd": round(cost, 8) if cost is not None else None,
        "mode": mode,
        "attempts": attempts,
    }


class MemoryRequestLog:
    """Per-process ring buffer. Events are lost on restart and each instance
    keeps its own - the same disclosed split as the in-memory meter."""

    def __init__(self, capacity: int = MAX_EVENTS):
        self.events: deque[dict] = deque(maxlen=capacity)
        self.errors = 0

    def record(self, event: dict) -> None:
        self.events.appendleft(event)

    def latest(self, n: int) -> list[dict]:
        return list(self.events)[:n]

    async def ping(self) -> bool:
        return True


# One atomic step per recorded event: push and cap the list.
# KEYS[1]: the list. ARGV: serialized event, capacity.
RECORD_LUA = """
redis.call('LPUSH', KEYS[1], ARGV[1])
redis.call('LTRIM', KEYS[1], 0, tonumber(ARGV[2]) - 1)
return 1
"""


class RedisRequestLog:
    """Shared capped list, one EVAL per recorded event and one LRANGE per read
    (reads are cached at the endpoint, so concurrent viewers share one read)."""

    def __init__(self, url: str, token: str, prefix: str = "llmgate:reqlog",
                 capacity: int = MAX_EVENTS, timeout: float = 4.0,
                 client: httpx.AsyncClient | None = None):
        self.key = prefix
        self.capacity = capacity
        self.errors = 0
        self._client = client or httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    @classmethod
    def from_env(cls, client: httpx.AsyncClient | None = None) -> "RedisRequestLog | None":
        creds = store_credentials_from_env()
        return cls(creds[0], creds[1], client=client) if creds else None

    async def _cmd(self, parts: list):
        try:
            r = await self._client.post("/", json=parts)
        except httpx.HTTPError as e:
            raise LogUnavailable(f"store unreachable: {e}") from e
        if r.status_code != 200:
            raise LogUnavailable(f"store answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise LogUnavailable("store replied with non-JSON") from e
        if "error" in body:
            raise LogUnavailable(f"store error: {body['error']}")
        return body.get("result")

    async def record(self, event: dict) -> None:
        await self._cmd(["EVAL", RECORD_LUA, 1, self.key,
                         json.dumps(event, separators=(",", ":")), str(self.capacity)])

    async def latest(self, n: int) -> list[dict]:
        res = await self._cmd(["LRANGE", self.key, "0", str(n - 1)])
        out = []
        for raw in res or []:
            try:
                out.append(json.loads(raw))
            except ValueError:
                self.errors += 1  # a corrupt entry is skipped, never served raw
        return out

    async def ping(self) -> bool:
        try:
            r = await self._client.post("/", json=["PING"])
            return r.status_code == 200 and r.json().get("result") == "PONG"
        except Exception:
            return False
