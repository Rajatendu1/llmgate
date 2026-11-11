"""Redis-backed rate limiter: wiring, semantics, fail-closed behavior.

The stub transport runs the actual Lua scripts under Lua 5.1, with a small
Redis-command adapter (one synchronous handler = one atomic step). This checks
script syntax and arithmetic, not Redis networking or server concurrency.
Two app instances share one adapter for local multi-instance admission tests.
"""
import asyncio, json
import httpx, pytest
from lupa.lua51 import LuaRuntime
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.redislimit import (CHARGE_LUA, TAKE_LUA, RateLimiterUnavailable,
                                RedisRateLimiter)
from llmgate.router import Route, Router

H = {"Authorization": "Bearer k"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}


class FakeUpstash:
    """Run the production scripts, not a Python reimplementation of them.
    Only HMGET/HMSET/PEXPIRE are adapted; now_ms is injectable for refill tests.
    """

    def __init__(self, now_ms=lambda: 0):
        self.store: dict[str, dict] = {}
        self.now_ms = now_ms
        self.fail_with: int | None = None  # set an HTTP status to simulate an outage

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"error": "simulated outage"})
        cmd = json.loads(request.content)
        if cmd[0] == "PING":
            return httpx.Response(200, json={"result": "PONG"})
        assert cmd[0] == "EVAL" and cmd[2] == 1
        script, key = cmd[1], cmd[3]
        runtime = LuaRuntime(unpack_returned_tuples=True)

        def redis_call(command, key, *args):
            if command == "HMGET":
                h = self.store.get(key, {})
                return runtime.table_from([h.get(field) for field in args])
            if command == "HMSET":
                h = self.store.setdefault(key, {})
                h.update(zip(args[::2], args[1::2]))
                return "OK"
            if command == "PEXPIRE":
                return 1
            raise AssertionError(f"unexpected Redis command: {command}")

        runtime.globals().redis = runtime.table_from({"call": redis_call})
        runtime.globals().KEYS = runtime.table_from([key])
        args = list(cmd[4:9])
        args[3] = str(self.now_ms())
        runtime.globals().ARGV = runtime.table_from(args)
        result = runtime.execute(script)
        if script == TAKE_LUA:
            result = [result[1], result[2]]
        return httpx.Response(200, json={"result": result})


def redis_limiter(store: FakeUpstash, **kw) -> RedisRateLimiter:
    rl = RedisRateLimiter("http://fake-upstash", "token", **kw)
    rl._client = httpx.AsyncClient(transport=httpx.MockTransport(store.handler),
                                   base_url="http://fake-upstash")
    return rl


async def test_take_and_charge_semantics():
    store = FakeUpstash()
    rl = redis_limiter(store)
    tb = rl.bucket("tenant", rate=1.0, burst=2.0)
    assert await tb.take() == (True, 0.0)
    assert await tb.take() == (True, 0.0)
    ok, retry = await tb.take()
    assert not ok and retry == pytest.approx(1.0, abs=0.01)
    # settlement drives the balance negative; next take waits it out
    await tb.charge(2)
    ok, retry = await tb.take()
    assert not ok and retry == pytest.approx(3.0, abs=0.01)


async def test_refill_with_time():
    t = [0]
    store = FakeUpstash(now_ms=lambda: t[0])
    tb = redis_limiter(store).bucket("tenant", rate=2.0, burst=2.0)
    assert (await tb.take(2))[0]
    assert not (await tb.take())[0]
    t[0] = 600  # 0.6 s -> 1.2 tokens refilled
    assert (await tb.take())[0]


async def test_shared_across_instances_under_concurrency():
    """Two limiter instances (two 'function instances') over one store never
    over-admit: 50 concurrent takes against burst 10 admit exactly 10."""
    store = FakeUpstash()
    rl_a, rl_b = redis_limiter(store), redis_limiter(store)
    buckets = [rl_a.bucket("tenant", 1.0, 10.0), rl_b.bucket("tenant", 1.0, 10.0)]

    async def attempt(i):
        return (await buckets[i % 2].take())[0]

    allowed = await asyncio.gather(*(attempt(i) for i in range(50)))
    assert sum(allowed) == 10


async def test_fail_closed_on_store_error():
    store = FakeUpstash()
    store.fail_with = 500
    tb = redis_limiter(store).bucket("tenant", 1.0, 1.0)
    with pytest.raises(RateLimiterUnavailable):
        await tb.take()
    with pytest.raises(RateLimiterUnavailable):
        await tb.charge(1)


def _client(store, **keykw):
    rl = redis_limiter(store)
    app = create_app(Router([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1))]),
                     {"k": ApiKey("tenant", 2, 4, **keykw)}, limiter=rl)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_app_request_limit_is_global_across_app_instances():
    """The real end-to-end property: two app instances behind one store admit
    only the configured burst between them."""
    store = FakeUpstash()
    _, c1 = _client(store)
    _, c2 = _client(store)

    async def call(c):
        return (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code

    codes = await asyncio.gather(*([call(c1) for _ in range(6)] + [call(c2) for _ in range(6)]))
    assert codes.count(200) == 4 and codes.count(429) == 8


async def test_app_token_admission_and_settlement_shared():
    store = FakeUpstash()
    _, c = _client(store, token_rate=5, token_burst=10)
    big = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 20)}]}
    r = await c.post("/v1/chat/completions", json=big, headers=H)
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200
    # est 3 + settled 5 = 8 of 10 spent, next estimated 3 does not fit
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"


async def test_app_503_when_store_down():
    store = FakeUpstash()
    store.fail_with = 503
    _, c = _client(store)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 503 and r.json()["error"]["type"] == "rate_limiter_unavailable"
    assert r.headers["retry-after"] == "2"


async def test_healthz_reports_shared_backend():
    store = FakeUpstash()
    app, c = _client(store)
    r = await c.get("/healthz")
    assert r.json()["rate_limit"] == {"backend": "redis", "shared": True, "reachable": True}
    store.fail_with = 500
    r = await c.get("/healthz")
    assert r.json()["rate_limit"]["reachable"] is False


def test_from_env_candidates(monkeypatch):
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("STORAGE_REST_API_URL", raising=False)
    monkeypatch.delenv("STORAGE_REST_API_TOKEN", raising=False)
    assert RedisRateLimiter.from_env() is None
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert RedisRateLimiter.from_env() is not None


async def test_actual_lua_credit_is_capped_at_burst():
    store = FakeUpstash()
    tb = redis_limiter(store).bucket("credit", rate=1.0, burst=20.0)
    assert (await tb.take(17))[0]
    await tb.charge(-100)  # a large refund cannot mint excess capacity
    assert store.store[tb.key]["tokens"] == 20
    assert (await tb.take(20))[0]
    assert not (await tb.take())[0]


async def test_actual_lua_refills_before_settlement_and_caps_credit():
    now = [0]
    store = FakeUpstash(now_ms=lambda: now[0])
    tb = redis_limiter(store).bucket("refill", rate=2.0, burst=10.0)
    assert (await tb.take(10))[0]
    now[0] = 2000  # refill four tokens, then debit three
    await tb.charge(3)
    assert store.store[tb.key]["tokens"] == 1
    await tb.charge(-20)
    assert store.store[tb.key]["tokens"] == 10


async def test_actual_lua_unused_output_reservation_is_refunded():
    store = FakeUpstash()
    _, c = _client(store, token_rate=1, token_burst=20)
    async with c:
        r = await c.post("/v1/chat/completions", json={**BODY, "max_tokens": 14}, headers=H)
        assert r.status_code == 200
        # 17 reserved, eight used, nine refunded: 12 tokens remain.
        key = next(k for k in store.store if k.endswith(":1:20"))
        assert store.store[key]["tokens"] == 12
        follow = {**BODY, "messages": [{"role": "user", "content": " ".join(["w"] * 10)}]}
        assert (await c.post("/v1/chat/completions", json=follow, headers=H)).status_code == 200


async def test_settlement_failure_is_logged_without_claiming_refund(caplog):
    store = FakeUpstash()
    original = store.handler

    def fail_settlement(request):
        cmd = json.loads(request.content)
        if cmd[0] == "EVAL" and cmd[1] == CHARGE_LUA:
            return httpx.Response(503, json={"error": "settlement unavailable"})
        return original(request)

    store.handler = fail_settlement
    _, c = _client(store, token_rate=1, token_burst=20)
    async with c:
        r = await c.post("/v1/chat/completions", json={**BODY, "max_tokens": 14}, headers=H)
        assert r.status_code == 200  # upstream response already produced
        key = next(k for k in store.store if k.endswith(":1:20"))
        assert store.store[key]["tokens"] == 3  # reservation retained once
        assert "rate limiter settlement failed" in caplog.text
        assert "balance kept the estimate once" in caplog.text
