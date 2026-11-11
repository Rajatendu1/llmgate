"""Redis-backed token-bucket rate limiter for multi-instance deployments.

Speaks the Upstash REST API (plain HTTPS, no redis client dependency), running
the bucket update as a single Lua EVAL so each take/charge is atomic across any
number of concurrent function instances. Buckets are keyed per (name, rate,
burst), same as the in-memory RateLimiter.

Failure mode is fail-closed: any store error raises RateLimiterUnavailable and
the request path answers 503, rather than silently degrading to per-process
enforcement that would over-admit under concurrency.
"""
from __future__ import annotations
import os
import httpx


class RateLimiterUnavailable(Exception):
    """The shared rate-limit store could not be reached or answered unexpectedly."""


# Atomic take: refill by elapsed time, debit `requested` when it fits.
# ARGV: capacity, rate_per_sec, requested, now_ms, ttl_ms -> {allowed(0/1), retry_after_ms}
TAKE_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now end
tokens = math.min(capacity, tokens + math.max(0, now - ts) / 1000.0 * rate)
local allowed = 0
local retry_ms = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
else
  retry_ms = math.ceil((requested - tokens) / rate * 1000.0)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return {allowed, retry_ms}
"""

# Atomic post-hoc settlement: refill, then debit without gating. A negative debit
# credits an admission over-estimate back; a credit never pushes past capacity
# (same semantics as TokenBucket.charge).
# ARGV: capacity, rate_per_sec, debit, now_ms, ttl_ms -> 1
CHARGE_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local debit = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now end
tokens = math.min(capacity, math.min(capacity, tokens + math.max(0, now - ts) / 1000.0 * rate) - debit)
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
"""

_ENV_CANDIDATES = (
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),          # Vercel marketplace integration
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("STORAGE_REST_API_URL", "STORAGE_REST_API_TOKEN"),  # custom-prefix variant
)


def store_credentials_from_env() -> "tuple[str, str] | None":
    """The first configured Upstash-compatible REST URL/token pair, or None.
    Shared by the rate limiter and the meter so both use the same store."""
    for url_var, token_var in _ENV_CANDIDATES:
        url, token = os.environ.get(url_var), os.environ.get(token_var)
        if url and token:
            return url, token
    return None


class RedisTokenBucket:
    def __init__(self, limiter: "RedisRateLimiter", name: str, rate: float, burst: float):
        self.limiter, self.rate, self.burst = limiter, rate, burst
        self.key = f"{limiter.prefix}{name}:{rate:g}:{burst:g}"
        # keep state while limiting stays relevant: two full refill windows + margin
        self.ttl_ms = max(60_000, int(burst / rate * 2 * 1000) + 60_000)

    async def take(self, n: float = 1.0):
        res = await self.limiter._eval(TAKE_LUA, self.key,
                                       [self.burst, self.rate, n, _now_ms(), self.ttl_ms])
        try:
            return bool(res[0]), int(res[1]) / 1000.0
        except (TypeError, ValueError, IndexError) as e:
            raise RateLimiterUnavailable(f"unexpected store reply: {res!r}") from e

    async def charge(self, n: float):
        await self.limiter._eval(CHARGE_LUA, self.key,
                                 [self.burst, self.rate, n, _now_ms(), self.ttl_ms])


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


class RedisRateLimiter:
    """Drop-in async equivalent of resilience.RateLimiter, backed by Redis."""

    def __init__(self, url: str, token: str, prefix: str = "llmgate:rl:", timeout: float = 4.0):
        self.prefix = prefix
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> "RedisRateLimiter | None":
        """Build from the environment when a REST URL/token pair is present, else None."""
        creds = store_credentials_from_env()
        return cls(*creds) if creds else None

    async def _eval(self, script: str, key: str, args: list):
        try:
            r = await self._client.post("/", json=["EVAL", script, 1, key, *[str(a) for a in args]])
        except httpx.HTTPError as e:
            raise RateLimiterUnavailable(f"store unreachable: {e}") from e
        if r.status_code != 200:
            raise RateLimiterUnavailable(f"store answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise RateLimiterUnavailable("store replied with non-JSON") from e
        if "error" in body:
            raise RateLimiterUnavailable(f"store error: {body['error']}")
        return body.get("result")

    def bucket(self, key: str, rate: float, burst: float) -> RedisTokenBucket:
        return RedisTokenBucket(self, key, rate, burst)

    async def check(self, key: str, rate: float, burst: float, n: float = 1.0):
        return await self.bucket(key, rate, burst).take(n)

    async def ping(self) -> bool:
        try:
            r = await self._client.post("/", json=["PING"])
            return r.status_code == 200 and r.json().get("result") == "PONG"
        except Exception:
            return False

    async def aclose(self):
        await self._client.aclose()
