"""Visitor fault injection (X-Simulate-Failure) and own-key usage: minted
self-serve keys only, simulated fleet only, consume-once so retry/failover
legs run the gateway's genuine policy, and strict per-visitor isolation."""
import httpx

from llmgate.app import ApiKey, create_app
from llmgate.demo_keys import digest_of, install_demo_keys
from llmgate.demo_try import install_demo_try
from llmgate.providers import MockProvider
from llmgate.resilience import CircuitBreaker
from llmgate.router import Route, Router


def _app(with_real=True):
    sim_a = MockProvider("sim-a", ["gpt-sim", "gpt-sim-pro"], latency=0.01)
    sim_b = MockProvider("sim-b", ["gpt-sim", "gpt-sim-pro"], latency=0.01)
    routes = [Route(sim_a, priority=0, breaker=CircuitBreaker(3, 5)),
              Route(sim_b, priority=1, breaker=CircuitBreaker(3, 5))]
    if with_real:
        real = MockProvider("groq", ["gpt-real"], latency=0.01)
        real.mode = "real"
        routes.insert(0, Route(real, priority=0, breaker=CircuitBreaker(3, 5)))
    router = Router(routes)
    router.failover_ambiguous = True
    app = create_app(router, {"demo-key": ApiKey("demo")})
    install_demo_try(app, model="gpt-sim")
    install_demo_keys(app)
    return app


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mint(cl):
    r = await cl.post("/demo/keys")
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _chat(key, fault=None, stream=False):
    h = {"Authorization": f"Bearer {key}"}
    if fault:
        h["X-Simulate-Failure"] = fault
    return {"headers": h,
            "json": {"model": "gpt-sim", "stream": stream,
                     "messages": [{"role": "user", "content": "hi"}]}}


async def test_injected_429_retries_in_place_and_is_served():
    app = _app()
    async with await _client(app) as cl:
        key = await _mint(cl)
        r = await cl.post("/v1/chat/completions", **_chat(key, fault="429"))
        assert r.status_code == 200, r.text
        # consume-once: the retry leg ran clean on the primary (the attempts
        # header counts providers engaged; the provider call count proves the
        # in-place retry happened)
        assert r.headers["x-gateway-provider"] == "sim-a"
        sim_a = app.state.router.routes[1].provider
        assert sim_a.calls == 2


async def test_injected_500_fails_over_to_the_backup():
    app = _app()
    async with await _client(app) as cl:
        key = await _mint(cl)
        r = await cl.post("/v1/chat/completions", **_chat(key, fault="500"))
        assert r.status_code == 200, r.text
        assert r.headers["x-gateway-provider"] == "sim-b"
        assert r.headers["x-gateway-attempts"] == "2"


async def test_injected_slow_succeeds_late():
    app = _app()
    async with await _client(app) as cl:
        key = await _mint(cl)
        import time
        t0 = time.perf_counter()
        r = await cl.post("/v1/chat/completions", **_chat(key, fault="slow"))
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200, r.text
        assert elapsed >= 3.0
        assert r.headers["x-gateway-provider"] == "sim-a"


async def test_injected_truncate_cuts_the_stream_mid_way():
    app = _app()
    async with await _client(app) as cl:
        key = await _mint(cl)
        chunks = []
        async with cl.stream("POST", "/v1/chat/completions",
                             **_chat(key, fault="truncate", stream=True)) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    chunks.append(line[6:])
        assert any('"stream_error"' in c for c in chunks), chunks
        assert not any("[DONE]" == c for c in chunks)


async def test_fault_header_rejected_for_static_keys_and_bad_values():
    app = _app()
    async with await _client(app) as cl:
        r = await cl.post("/v1/chat/completions", **_chat("demo-key", fault="429"))
        assert r.status_code == 400
        key = await _mint(cl)
        r = await cl.post("/v1/chat/completions", **_chat(key, fault="explode"))
        assert r.status_code == 400
        r = await cl.post("/v1/chat/completions", **_chat(key, fault="truncate"))
        assert r.status_code == 400  # truncate is stream-only


async def test_own_usage_is_isolated_per_minted_key():
    app = _app()
    async with await _client(app) as cl:
        k1, k2 = await _mint(cl), await _mint(cl)
        await cl.post("/v1/chat/completions", **_chat(k1))
        await cl.post("/v1/chat/completions", **_chat(k1))
        await cl.post("/v1/chat/completions", **_chat(k2))
        r = await cl.get("/demo/usage", headers={"Authorization": f"Bearer {k1}"})
        assert r.status_code == 200, r.text
        u1 = r.json()["usage"]
        assert u1["requests"] == 2 and u1["status_ok"] == 2
        r = await cl.get("/demo/usage", headers={"Authorization": f"Bearer {k2}"})
        assert r.json()["usage"]["requests"] == 1
        # static keys and unknown keys get nothing
        r = await cl.get("/demo/usage", headers={"Authorization": "Bearer demo-key"})
        assert r.status_code == 401
        r = await cl.get("/demo/usage", headers={"Authorization": "Bearer vg_nope"})
        assert r.status_code == 401


async def test_per_key_budgets_are_independent():
    """One visitor burning their budget must not throttle another visitor."""
    app = _app()
    async with await _client(app) as cl:
        k1, k2 = await _mint(cl), await _mint(cl)
        last = None
        for _ in range(12):  # k1 burst is 6, sustained 0.5/s: burns out fast
            last = await cl.post("/v1/chat/completions", **_chat(k1))
        assert last.status_code == 429
        r = await cl.post("/v1/chat/completions", **_chat(k2))
        assert r.status_code == 200, r.text


async def test_pre_meter_id_records_derive_the_same_identity():
    """Compatibility: a record minted before per-visitor identities shipped
    (no meter_id field) resolves to the same derived identity the mint path
    would compute - usage reads and budgets work, nothing resets or widens."""
    app = _app()
    async with await _client(app) as cl:
        key = await _mint(cl)
        # rewrite the stored record into the old shape, and drop any cached key
        store = app.state.dynamic_keys
        digest = digest_of(key)
        rec = dict(store._recs[digest])
        del rec["meter_id"]
        store._recs[digest] = rec
        app.state.keys.pop(key, None)
        r = await cl.post("/v1/chat/completions", **_chat(key))
        assert r.status_code == 200, r.text
        r = await cl.get("/demo/usage", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200, r.text
        assert r.json()["usage"]["requests"] == 1
