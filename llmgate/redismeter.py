"""Redis-backed usage metering for multi-instance deployments.

Same Upstash REST transport as redislimit.py. Usage counters (requests by
key/status, tokens by key, requests by provider/status) live in Redis hashes
updated by one atomic Lua EVAL per recorded request, so /admin/usage and
/metrics report fleet-wide totals instead of per-function-instance numbers.

Failure modes, disclosed:
- record() is fail-open: metering is observability, not enforcement, so a
  store error never fails the client's request. It raises MeterUnavailable;
  the app logs it, counts it in `errors` (surfaced on /healthz), and the
  event is lost. Counters therefore under-count during a store outage rather
  than block traffic.
- Reads (/admin/usage, /metrics) are fail-closed: a store error there raises
  MeterUnavailable and the endpoint answers 503 metering_unavailable, because
  serving partial usage as if it were complete would be worse than no answer.

Latency stays per process in both backends: the quantile window and the
count/sum accumulators describe the serving instance only (see README).
"""
from __future__ import annotations
import httpx
from .redislimit import store_credentials_from_env


class MeterUnavailable(Exception):
    """The shared metering store could not be reached or answered unexpectedly."""


# One atomic step per recorded event: add the key/provider names to the index
# sets and bump the counters in both hashes.
# KEYS: per-key hash, per-provider hash, key-name set, provider-name set
# ARGV: key name, provider name, status, prompt_tokens, completion_tokens, cost_usd
RECORD_LUA = """
redis.call('SADD', KEYS[3], ARGV[1])
redis.call('HINCRBY', KEYS[1], 'requests', 1)
redis.call('HINCRBY', KEYS[1], 'status_' .. ARGV[3], 1)
redis.call('HINCRBY', KEYS[1], 'prompt_tokens', tonumber(ARGV[4]))
redis.call('HINCRBY', KEYS[1], 'completion_tokens', tonumber(ARGV[5]))
local cost = tonumber(ARGV[6])
if cost and cost ~= 0 then
  redis.call('HINCRBYFLOAT', KEYS[1], 'cost_usd', cost)
end
redis.call('SADD', KEYS[4], ARGV[2])
redis.call('HINCRBY', KEYS[2], 'requests', 1)
redis.call('HINCRBY', KEYS[2], 'status_' .. ARGV[3], 1)
return 1
"""

# Whole-fleet read in one round trip: names interleaved with their HGETALLs,
# providers after the marker. Nested arrays survive the REST JSON encoding.
# KEYS: key-name set, provider-name set. ARGV: key-hash prefix, provider-hash prefix
SNAPSHOT_LUA = """
local res = {}
for _, k in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  res[#res+1] = k
  res[#res+1] = redis.call('HGETALL', ARGV[1] .. k)
end
res[#res+1] = '--providers--'
for _, p in ipairs(redis.call('SMEMBERS', KEYS[2])) do
  res[#res+1] = p
  res[#res+1] = redis.call('HGETALL', ARGV[2] .. p)
end
return res
"""


def _flat_to_counters(flat: list) -> dict:
    out = {}
    it = iter(flat or [])
    for field, val in zip(it, it):
        try:
            out[field] = float(val) if field == "cost_usd" else int(val)
        except (TypeError, ValueError):
            out[field] = 0
    return out


class RedisMeter:
    """Async equivalent of metering.Meter for the shared-counter parts.

    `usage`/`by_provider` are a cache of the last refresh() (read by the sync
    Prometheus renderer); latency attributes are live per-process state.
    """

    def __init__(self, url: str, token: str, prefix: str = "llmgate:meter:",
                 timeout: float = 4.0, client: httpx.AsyncClient | None = None):
        self.prefix = prefix
        self.errors = 0  # record() events lost to store errors since process start
        self.latencies: list[float] = []
        self.latency_count, self.latency_sum = 0, 0.0
        self.usage: dict[str, dict] = {}
        self.by_provider: dict[str, dict] = {}
        self._client = client or httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    @classmethod
    def from_env(cls, client: httpx.AsyncClient | None = None) -> "RedisMeter | None":
        creds = store_credentials_from_env()
        return cls(creds[0], creds[1], client=client) if creds else None

    async def _cmd(self, parts: list):
        try:
            r = await self._client.post("/", json=parts)
        except httpx.HTTPError as e:
            raise MeterUnavailable(f"store unreachable: {e}") from e
        if r.status_code != 200:
            raise MeterUnavailable(f"store answered HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise MeterUnavailable("store replied with non-JSON") from e
        if "error" in body:
            raise MeterUnavailable(f"store error: {body['error']}")
        return body.get("result")

    async def _eval(self, script: str, keys: list, args: list):
        return await self._cmd(["EVAL", script, len(keys), *keys, *[str(a) for a in args]])

    async def record(self, key, provider, model, usage, status="ok", latency=None, cost=None):
        # latency stays per process in both backends (disclosed in README)
        if latency is not None:
            self.latencies.append(latency)
            self.latency_count += 1
            self.latency_sum += latency
            if len(self.latencies) > 50000:
                del self.latencies[:25000]
        u = usage or {}
        await self._eval(RECORD_LUA,
                         [self.prefix + "k:" + key, self.prefix + "p:" + provider,
                          self.prefix + "keys", self.prefix + "provs"],
                         [key, provider, status,
                          u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                          cost or 0])

    async def refresh(self):
        """Pull fleet-wide counters into the local cache read by render()."""
        res = await self._eval(SNAPSHOT_LUA,
                               [self.prefix + "keys", self.prefix + "provs"],
                               [self.prefix + "k:", self.prefix + "p:"])
        try:
            cut = res.index("--providers--")
            names, provs = res[:cut], res[cut + 1:]
            self.usage = {names[i]: _flat_to_counters(names[i + 1])
                          for i in range(0, len(names), 2)}
            self.by_provider = {provs[i]: _flat_to_counters(provs[i + 1])
                                for i in range(0, len(provs), 2)}
        except (ValueError, TypeError, IndexError) as e:
            raise MeterUnavailable(f"unexpected store reply: {res!r}") from e

    async def snapshot(self, key=None):
        if key is not None:
            flat = await self._cmd(["HGETALL", self.prefix + "k:" + key])
            return _flat_to_counters(flat)
        await self.refresh()
        return {"keys": {k: dict(v) for k, v in self.usage.items()},
                "providers": {k: dict(v) for k, v in self.by_provider.items()}}

    async def ping(self) -> bool:
        try:
            r = await self._client.post("/", json=["PING"])
            return r.status_code == 200 and r.json().get("result") == "PONG"
        except Exception:
            return False
