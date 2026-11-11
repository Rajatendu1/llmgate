"""Redis-backed shared metering: aggregation across instances, failure modes,
and the request-context middleware (request IDs, security headers).

The stub emulates the Upstash REST endpoint including the meter's Lua
semantics (one synchronous handler = one atomic step), so two app instances
sharing one stub behave like two function instances sharing one Redis.
"""
import json
import httpx, pytest
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.redislimit import CHARGE_LUA, TAKE_LUA, RedisRateLimiter
from llmgate.redismeter import RECORD_LUA, SNAPSHOT_LUA, RedisMeter
from llmgate.router import Price, Route, Router

H = {"Authorization": "Bearer k"}
ADMIN = {"Authorization": "Bearer a"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}


class FakeUpstash:
    """In-memory stand-in covering both the limiter and the meter scripts."""

    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.hashes: dict[str, dict] = {}
        self.sets: dict[str, set] = {}
        self.fail_with: int | None = None

    def _record(self, keys, args):
        khash, phash, setk, setp = keys
        name, provider, status, pt, ct = args[0], args[1], args[2], int(args[3]), int(args[4])
        cost = float(args[5]) if len(args) > 5 else 0.0
        self.sets.setdefault(setk, set()).add(name)
        h = self.hashes.setdefault(khash, {})
        h["requests"] = h.get("requests", 0) + 1
        h["status_" + status] = h.get("status_" + status, 0) + 1
        h["prompt_tokens"] = h.get("prompt_tokens", 0) + pt
        h["completion_tokens"] = h.get("completion_tokens", 0) + ct
        if cost:
            h["cost_usd"] = h.get("cost_usd", 0.0) + cost
        self.sets.setdefault(setp, set()).add(provider)
        p = self.hashes.setdefault(phash, {})
        p["requests"] = p.get("requests", 0) + 1
        p["status_" + status] = p.get("status_" + status, 0) + 1
        return 1

    def _snapshot(self, keys, args):
        setk, setp = keys
        kpre, ppre = args
        res = []
        for k in sorted(self.sets.get(setk, ())):
            flat = [x for kv in sorted(self.hashes.get(kpre + k, {}).items()) for x in kv]
            res += [k, flat]
        res.append("--providers--")
        for p in sorted(self.sets.get(setp, ())):
            flat = [x for kv in sorted(self.hashes.get(ppre + p, {}).items()) for x in kv]
            res += [p, flat]
        return res

    def _bucket(self, script, key, args):
        capacity, rate, amount, _now, ttl = (float(x) for x in args[:5])
        h = self.buckets.get(key, {})
        tokens = min(capacity, float(h.get("tokens", capacity)))
        if script == TAKE_LUA:
            if tokens >= amount:
                tokens -= amount
                result = [1, 0]
            else:
                result = [0, int((amount - tokens) / rate * 1000 + 0.999)]
        else:  # CHARGE_LUA: a negative amount credits back, clamped at capacity
            tokens = min(capacity, tokens - amount)
            result = 1
        self.buckets[key] = {"tokens": tokens, "ts": 0}
        return result

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"error": "simulated outage"})
        cmd = json.loads(request.content)
        if cmd[0] == "PING":
            return httpx.Response(200, json={"result": "PONG"})
        if cmd[0] == "HGETALL":
            flat = [x for kv in sorted(self.hashes.get(cmd[1], {}).items()) for x in kv]
            return httpx.Response(200, json={"result": flat})
        assert cmd[0] == "EVAL"
        script, nkeys = cmd[1], cmd[2]
        keys, args = cmd[3:3 + nkeys], cmd[3 + nkeys:]
        if script in (TAKE_LUA, CHARGE_LUA):
            result = self._bucket(script, keys[0], args)
        elif script == RECORD_LUA:
            result = self._record(keys, args)
        elif script == SNAPSHOT_LUA:
            result = self._snapshot(keys, args)
        else:  # pragma: no cover
            return httpx.Response(400, json={"error": "unknown script"})
        return httpx.Response(200, json={"result": result})


def wired(store: FakeUpstash, price=None, **keykw):
    """An app whose limiter AND meter share one fake store, like production."""
    transport = httpx.MockTransport(store.handler)
    rl = RedisRateLimiter("http://fake-upstash", "token")
    rl._client = httpx.AsyncClient(transport=transport, base_url="http://fake-upstash")
    m = RedisMeter("http://fake-upstash", "token")
    m._client = httpx.AsyncClient(transport=transport, base_url="http://fake-upstash")
    app = create_app(Router([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1),
                                   price=price)]),
                     {"k": ApiKey("tenant", 50, 50, **keykw), "a": ApiKey("admin", admin=True)},
                     limiter=rl, meter=m)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), m


async def test_usage_aggregates_across_app_instances():
    store = FakeUpstash()
    _, c1, _ = wired(store)
    _, c2, _ = wired(store)
    for _ in range(3):
        assert (await c1.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    for _ in range(2):
        assert (await c2.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    # either instance reads the fleet-wide totals
    r = await c2.get("/admin/usage", headers=ADMIN)
    assert r.status_code == 200
    usage = r.json()
    assert usage["keys"]["tenant"]["requests"] == 5
    assert usage["keys"]["tenant"]["prompt_tokens"] == 15
    assert usage["keys"]["tenant"]["completion_tokens"] == 5 * 5  # "[a] echo: hello there world"
    assert usage["providers"]["a"]["requests"] == 5
    assert usage["providers"]["a"]["status_ok"] == 5


async def test_cost_aggregates_across_app_instances():
    """Estimated cost rides the same shared counters: two instances, one fleet total."""
    store = FakeUpstash()
    _, c1, _ = wired(store, price=Price(0.40, 1.20))
    _, c2, _ = wired(store, price=Price(0.40, 1.20))
    assert (await c1.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    assert (await c2.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    r = await c2.get("/admin/usage", headers=ADMIN)
    cost = r.json()["keys"]["tenant"]["cost_usd"]
    assert abs(cost - 2 * (3 * 0.40 + 5 * 1.20) / 1e6) < 1e-12


async def test_snapshot_shape_matches_in_memory():
    store = FakeUpstash()
    _, c, m = wired(store)
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    per_key = await m.snapshot("tenant")
    assert per_key["requests"] == 1 and per_key["prompt_tokens"] == 3
    assert await m.snapshot("nobody") == {}


async def test_record_fails_open_and_counts_errors():
    """A metering-store outage never fails the client's request: the event is
    lost, logged, counted, and surfaced on /healthz. (In-memory limiter here
    so admission does not fail closed first; with a shared store the limiter's
    own fail-closed 503 precedes metering, which is also documented.)"""
    store = FakeUpstash()
    m = RedisMeter("http://fake-upstash", "token")
    m._client = httpx.AsyncClient(transport=httpx.MockTransport(store.handler),
                                  base_url="http://fake-upstash")
    app = create_app(Router([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1))]),
                     {"k": ApiKey("tenant", 50, 50)}, meter=m)  # in-memory limiter
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    store.fail_with = 500
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200  # request unaffected by the metering outage
    assert m.errors == 1
    r = await c.get("/healthz")
    body = r.json()
    assert body["metering"]["reachable"] is False and body["metering"]["errors"] == 1


async def test_admin_usage_fails_closed_when_store_down():
    store = FakeUpstash()
    _, c, _ = wired(store)
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    store.fail_with = 500
    r = await c.get("/admin/usage", headers=ADMIN)
    assert r.status_code == 503 and r.json()["error"]["type"] == "metering_unavailable"
    r = await c.get("/metrics", headers=ADMIN)
    assert r.status_code == 503


async def test_metrics_reports_fleet_counters_from_either_instance():
    store = FakeUpstash()
    _, c1, _ = wired(store)
    _, c2, _ = wired(store)
    assert (await c1.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    r = await c2.get("/metrics", headers=ADMIN)
    assert 'llmgate_requests_total{key="tenant",status="ok"} 1' in r.text
    assert 'llmgate_provider_requests_total{provider="a",status="ok"} 1' in r.text


async def test_healthz_reports_metering_backend():
    store = FakeUpstash()
    _, c, _ = wired(store)
    r = await c.get("/healthz")
    assert r.json()["metering"] == {"backend": "redis", "shared": True, "reachable": True, "errors": 0}
    store.fail_with = 500
    r = await c.get("/healthz")
    assert r.json()["metering"]["reachable"] is False


async def test_request_id_and_security_headers():
    store = FakeUpstash()
    _, c, _ = wired(store)
    r = await c.get("/healthz", headers={"X-Request-ID": "test-rid-123"})
    assert r.headers["x-request-id"] == "test-rid-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    r = await c.get("/v1/models")  # 401 still gets a request id and headers
    assert r.status_code == 401 and r.headers["x-request-id"]
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_landing_page_csp_allows_inline_assets(monkeypatch):
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setenv("LLMGATE_KEYS", json.dumps({"k": {"name": "t"}}))
    for var in ("KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                "UPSTASH_REDIS_REST_TOKEN", "STORAGE_REST_API_URL", "STORAGE_REST_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    import importlib
    import index as index_module
    importlib.reload(index_module)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=index_module.app), base_url="http://t")
    r = await c.get("/")
    assert r.status_code == 200 and "unsafe-inline" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    await c.aclose()
